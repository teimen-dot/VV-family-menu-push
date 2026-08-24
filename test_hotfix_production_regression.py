import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app
import db
import menu_service
import runtime_config


TEST_ENV = {
    "APP_ENV": "development",
    "OWNER_AUTH_USERNAME": "vivian",
    "WORKER_AUTH_USERNAME": "kitchen",
    "SESSION_SECRET": "test-session-secret-0123456789abcdef",
    "PUSH_ENABLED": "false",
    "PUSH_ON_CONFIRM": "false",
}


class HeaderRecorder:
    def __init__(self):
        self.headers = []
        self._session_refresh = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers.append((name, value))

    def end_headers(self):
        pass

    def cookie_header(self):
        return next(value for name, value in self.headers if name == "Set-Cookie")


class LegacyDinersRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", os.path.join(self.tempdir.name, "test.db"))
        self.db_patch.start()
        db.init_db()
        conn = db.get_db()
        conn.execute(
            "INSERT INTO menus "
            "(id,date,location,status,diners,diners_count,meal_mode,banquet_total_diners) "
            "VALUES (1,'2099-01-01','shenzhen','draft','[]',4,'banquet',12)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_legacy_member_updates_preserve_explicit_diners_count(self):
        for diners in ([], ["vivian", "sir", "grandma"]):
            with self.subTest(diners=diners):
                self.assertTrue(app.update_menu_diners(1, diners))
                conn = db.get_db()
                row = conn.execute(
                    "SELECT diners,diners_count FROM menus WHERE id=1"
                ).fetchone()
                conn.close()
                self.assertEqual(json.loads(row["diners"]), diners)
                self.assertEqual(row["diners_count"], 4)

    def test_historical_banquet_fields_do_not_override_diners_count(self):
        self.assertEqual(menu_service._get_effective_diners_count(menu_id=1), 4)
        conn = db.get_db()
        conn.execute(
            "UPDATE menus SET diners='[\"vivian\"]', banquet_total_diners=20 WHERE id=1"
        )
        conn.commit()
        conn.close()
        self.assertEqual(menu_service._get_effective_diners_count(menu_id=1), 4)

    def test_same_dish_is_allowed_in_different_meals_but_not_twice_in_one_meal(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO dishes (id,name_cn,name_en,is_active) "
            "VALUES ('dish_shared','金蒜牛肉粒','Golden Garlic Diced Beef',1)"
        )
        conn.execute(
            "INSERT INTO menu_items (menu_id,dish_id,meal_type,sort_order) "
            "VALUES (1,'dish_shared','dinner',1)"
        )
        conn.commit()
        conn.close()

        self.assertTrue(menu_service.add_dish_to_menu(1, "dish_shared", "lunch"))
        self.assertFalse(menu_service.add_dish_to_menu(1, "dish_shared", "lunch"))
        conn = db.get_db()
        rows = conn.execute(
            "SELECT meal_type FROM menu_items WHERE menu_id=1 AND dish_id='dish_shared' "
            "ORDER BY meal_type"
        ).fetchall()
        conn.close()
        self.assertEqual([row["meal_type"] for row in rows], ["dinner", "lunch"])


class SessionTests(unittest.TestCase):
    def test_signed_session_is_30_days_and_restart_safe(self):
        with patch.dict(os.environ, TEST_ENV, clear=False):
            token = app.create_session("vivian", "owner")
            refreshed, session = app.session_from_cookie(f"{app.session_cookie_name()}={token}")
            self.assertEqual(session["role"], "owner")
            self.assertNotEqual(refreshed, token)
            self.assertEqual(app.SESSION_TTL_SECONDS, 30 * 24 * 60 * 60)
            self.assertIsNone(app.session_from_cookie(f"{app.session_cookie_name()}={token}x")[1])

    def test_preview_login_logout_and_refresh_cookies_work_over_http(self):
        with patch.dict(os.environ, TEST_ENV, clear=False):
            login = HeaderRecorder()
            app.AppHandler.send_redirect(login, "/tomorrow", session_id="login-token")

            logout = HeaderRecorder()
            app.AppHandler.send_redirect(logout, "/login", clear_session=True)

            refresh = HeaderRecorder()
            refresh._session_refresh = "refresh-token"
            app.AppHandler.send_session_refresh_header(refresh)

            token = app.create_session("vivian", "owner")
            self.assertEqual(app.session_cookie_name(), "family_session")
            self.assertIsNotNone(app.session_from_cookie(f"family_session={token}")[1])
            self.assertIsNone(app.session_from_cookie(f"__Host-family_session={token}")[1])
            self.assertIn(f"Max-Age={app.SESSION_TTL_SECONDS}", login.cookie_header())
            self.assertIn("Max-Age=0", logout.cookie_header())
            self.assertIn(f"Max-Age={app.SESSION_TTL_SECONDS}", refresh.cookie_header())
            self.assertTrue(login.cookie_header().startswith("family_session=login-token;"))
            self.assertTrue(logout.cookie_header().startswith("family_session=;"))
            self.assertTrue(refresh.cookie_header().startswith("family_session=refresh-token;"))
            for header in (login.cookie_header(), logout.cookie_header(), refresh.cookie_header()):
                self.assertNotIn("Secure", header)
                self.assertIn("HttpOnly; SameSite=Lax", header)

    def test_production_login_logout_and_refresh_cookies_keep_host_and_secure(self):
        production_env = {
            **TEST_ENV,
            "APP_ENV": "production",
            "LOCAL_PREVIEW_UI": "true",
            "LAN_PREVIEW_HTTP": "false",
        }
        with patch.dict(os.environ, production_env, clear=False):
            login = HeaderRecorder()
            app.AppHandler.send_redirect(login, "/tomorrow", session_id="login-token")

            logout = HeaderRecorder()
            app.AppHandler.send_redirect(logout, "/login", clear_session=True)

            refresh = HeaderRecorder()
            refresh._session_refresh = "refresh-token"
            app.AppHandler.send_session_refresh_header(refresh)

            token = app.create_session("vivian", "owner")
            self.assertEqual(app.session_cookie_name(), "__Host-family_session")
            self.assertIsNotNone(app.session_from_cookie(f"__Host-family_session={token}")[1])
            self.assertIsNone(app.session_from_cookie(f"family_session={token}")[1])
            for header in (login.cookie_header(), logout.cookie_header(), refresh.cookie_header()):
                self.assertTrue(header.startswith("__Host-family_session="))
                self.assertIn("; Secure; HttpOnly; SameSite=Lax", header)

    def test_production_lan_http_preview_uses_nonsecure_preview_cookie(self):
        preview_env = {
            **TEST_ENV,
            "APP_ENV": "production",
            "LOCAL_PREVIEW_UI": "true",
            "LAN_PREVIEW_HTTP": "true",
        }
        with patch.dict(os.environ, preview_env, clear=False):
            login = HeaderRecorder()
            app.AppHandler.send_redirect(login, "/tomorrow", session_id="login-token")

            refresh = HeaderRecorder()
            refresh._session_refresh = "refresh-token"
            app.AppHandler.send_session_refresh_header(refresh)

            token = app.create_session("vivian", "owner")
            self.assertEqual(app.session_cookie_name(), "family_session")
            self.assertIsNotNone(app.session_from_cookie(f"family_session={token}")[1])
            self.assertIsNone(app.session_from_cookie(f"__Host-family_session={token}")[1])
            for header in (login.cookie_header(), refresh.cookie_header()):
                self.assertTrue(header.startswith("family_session="))
                self.assertNotIn("; Secure", header)
                self.assertIn("; HttpOnly; SameSite=Lax", header)

    def test_production_requires_fixed_session_secret(self):
        with patch.dict(os.environ, {"APP_ENV": "production", "H5_BASE_URL": "https://menu.ourmenu.site", "SESSION_SECRET": ""}, clear=False):
            with self.assertRaises(ValueError):
                runtime_config.validate_app_startup()

    def test_lan_http_preview_requires_local_preview_ui(self):
        invalid_env = {
            **TEST_ENV,
            "APP_ENV": "production",
            "H5_BASE_URL": "https://menu.ourmenu.site",
            "LOCAL_PREVIEW_UI": "false",
            "LAN_PREVIEW_HTTP": "true",
        }
        with patch.dict(os.environ, invalid_env, clear=False):
            with self.assertRaisesRegex(ValueError, "LOCAL_PREVIEW_UI=true"):
                runtime_config.validate_app_startup()

    def test_lan_http_preview_startup_is_valid_with_explicit_pair(self):
        preview_env = {
            **TEST_ENV,
            "APP_ENV": "production",
            "H5_BASE_URL": "https://menu.ourmenu.site",
            "LOCAL_PREVIEW_UI": "true",
            "LAN_PREVIEW_HTTP": "true",
        }
        with patch.dict(os.environ, preview_env, clear=False):
            runtime_config.validate_app_startup()
            self.assertTrue(runtime_config.lan_http_preview_enabled())


class MarkupTests(unittest.TestCase):
    def test_family_ui_has_account_switch_logout(self):
        index_path = os.path.join(os.path.dirname(__file__), "public", "family-menu", "index.html")
        with open(index_path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('method="post" action="/logout"', html)
        self.assertIn("切换账号", html)

    def test_family_ui_reports_existing_pantry_item_instead_of_added(self):
        index_path = os.path.join(os.path.dirname(__file__), "public", "family-menu", "index.html")
        with open(index_path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("if (result.already_in_pantry)", html)
        self.assertIn("该食材已在当前库存", html)

    def test_worker_existing_pantry_name_path_and_new_ingredient_guard(self):
        self.assertTrue(app.post_path_allowed("worker", "/api/pantry/add-by-name"))
        self.assertFalse(app.post_path_allowed("worker", "/api/tomorrow/confirm"))
        self.assertIn("/api/ingredients/pending/merge", app.OWNER_ONLY_POST_PATHS)
        self.assertIn("/api/ingredients/pending/complete", app.OWNER_ONLY_POST_PATHS)

    def test_breakpoints_and_owner_controls(self):
        self.assertIn("@media(min-width:1024px){.dishes-page .dish-grid{grid-template-columns:repeat(3", app.CSS)
        self.assertIn("@media(min-width:1400px){.dishes-page .dish-grid{grid-template-columns:repeat(4", app.CSS)
        self.assertIn("@media(min-width:961px){.desktop-confirm{display:grid}.tomorrow-actions{display:none}", app.CSS)

    def test_kitchen_write_permissions(self):
        for path in (
            "/api/tomorrow/add", "/api/tomorrow/ai-fill", "/api/tomorrow/repair",
            "/api/tomorrow/confirm", "/api/tomorrow/diners",
            "/api/menu/diners",
        ):
            self.assertFalse(app.post_path_allowed("worker", path), path)

    def test_do_post_uses_module_database_function(self):
        self.assertNotIn("get_db", app.AppHandler.do_POST.__code__.co_varnames)

    def test_menu_banquet_mode_surface_is_removed(self):
        self.assertFalse(hasattr(app, "get_menu_meal_mode"))
        self.assertFalse(hasattr(app, "update_menu_meal_mode"))
        self.assertNotIn("/api/tomorrow/meal-mode", app.OWNER_ONLY_POST_PATHS)
        self.assertNotIn("/api/tomorrow/meal-mode", app.MENU_DRAFT_WRITE_PATHS)

    def test_empty_required_meals_share_validation_and_render_all_gaps(self):
        menu = {
            "exists": True, "menu_id": 1, "date": "2026-08-06", "status": "draft",
            "confirmed_at": None, "pushed_at": None, "push_status": "not_sent",
            "location": "shenzhen", "shortages": {}, "review_issues": "", "diners_count": 1,
            "meals": {"breakfast": [], "lunch": [], "afternoon_snack": [], "dinner": []},
        }
        diners = [{"id": "vv", "name_cn": "VV", "name_en": "VV", "default_attends": 1}]
        slots = {
            "breakfast": {"porridge": {"current": 0, "target_min": 1, "missing_min": 1}},
            "lunch": {"quick_soup": {"current": 0, "target_min": 1, "missing_min": 1}},
            "dinner": {"protein_main": {"current": 0, "target_min": 2, "missing_min": 2}},
        }
        with patch.object(app, "ensure_tomorrow_menu"), \
             patch.object(app, "get_menu_with_dishes", return_value=menu), \
             patch.object(app, "get_all_diners", return_value=diners), \
             patch.object(app, "get_menu_diners", return_value=["vv"]), \
             patch.object(app, "validate_menu_meals", return_value={"meal_slots": slots, "warnings": []}):
            owner_html = app.render_tomorrow("owner", "shenzhen")
            worker_html = app.render_tomorrow("worker", "shenzhen")
        self.assertIn("早餐缺少：粥1份", owner_html)
        self.assertIn("午餐缺少：快手汤1份", owner_html)
        self.assertIn("晚餐缺少：蛋白质2份", owner_html)
        self.assertNotIn("下午茶缺少", owner_html)
        self.assertIn('class="desktop-confirm"', owner_html)
        self.assertNotIn('class="desktop-confirm"', worker_html)
        self.assertNotIn("用餐模式", owner_html)
        self.assertNotIn("/api/tomorrow/meal-mode", owner_html)

    def test_production_preview_branch_uses_shared_gaps_and_desktop_actions(self):
        menu = {
            "exists": True, "menu_id": 1, "date": "2026-08-06", "status": "draft",
            "push_status": "not_sent", "diners_count": 1, "meals": {
                "breakfast": [], "lunch": [{
                    "menu_item_id": 9, "dish_id": "dish_1", "name_cn": "测试菜",
                    "name_en": "Test Dish", "category_id": "vegetable", "image": None,
                }], "afternoon_snack": [], "dinner": [{
                    "menu_item_id": 10, "dish_id": "dish_2", "name_cn": "测试晚餐",
                    "name_en": "Test Dinner", "category_id": "protein", "image": None,
                }],
            },
        }
        diners = [{"id": "vv", "name_cn": "VV", "name_en": "VV", "default_attends": 1}]
        slots = {
            "breakfast": {"porridge": {"current": 0, "target_min": 1, "missing_min": 1}},
            "lunch": {"quick_soup": {"current": 0, "target_min": 1, "missing_min": 1}},
            "dinner": {"protein_main": {"current": 0, "target_min": 2, "missing_min": 2}},
        }
        validation = {"meal_slots": slots, "missing_by_meal": slots, "warnings": []}
        with patch.dict(os.environ, {"LOCAL_PREVIEW_UI": "true"}, clear=False), \
             patch.object(app, "ensure_tomorrow_menu"), \
             patch.object(app, "get_menu_with_dishes", return_value=menu), \
             patch.object(app, "get_all_diners", return_value=diners), \
             patch.object(app, "get_menu_diners", return_value=["vv"]), \
             patch.object(app, "validate_menu_meals", return_value=validation):
            owner_html = app.render_tomorrow("owner", "shenzhen")
            worker_html = app.render_tomorrow("worker", "shenzhen")
        self.assertIn("早餐缺少 粥 1 份", owner_html)
        self.assertIn("午餐缺少 快手汤 1 份", owner_html)
        self.assertIn("晚餐缺少 蛋白质 2 份", owner_html)
        self.assertNotIn("下午茶缺少", owner_html)
        self.assertIn('class="desktop-owner-actions"', owner_html)
        self.assertNotIn('class="desktop-owner-actions"', worker_html)
        self.assertIn("Available now", owner_html)
        self.assertNotIn("智能补充", worker_html)
        self.assertNotIn("用餐模式", owner_html)
        self.assertNotIn("/api/tomorrow/meal-mode", owner_html)
        note_index = owner_html.index("添加备注")
        add_index = owner_html.index("添加菜品", note_index)
        fill_index = owner_html.index("智能补充", add_index)
        delete_index = owner_html.index("删除整餐", fill_index)
        self.assertLess(note_index, add_index)
        self.assertLess(add_index, fill_index)
        self.assertLess(fill_index, delete_index)
        self.assertIn("cycleDish(this", owner_html)
        self.assertIn("搜索更换", owner_html)
        meal_order = [
            owner_html.index("下一顿"), owner_html.index("明天早餐"),
            owner_html.index("明天午餐"), owner_html.index("明天下午茶"),
            owner_html.index("明天晚餐"),
        ]
        self.assertEqual(meal_order, sorted(meal_order))
        self.assertIn("Next Meal", owner_html)
        self.assertNotIn("今日晚餐", owner_html)
        self.assertIn("<title>菜单 · Menu</title>", owner_html)
        self.assertIn('<span class="lang-zh">菜单</span><span class="lang-en">Menu</span>', owner_html)
        self.assertGreaterEqual(owner_html.count("meal-diners-button"), 5)
        self.assertGreaterEqual(owner_html.count('class="meal-delete-x"'), 2)
        self.assertIn("重新添加该餐", owner_html)
        self.assertIn("/api/menu/diners", owner_html)
        self.assertIn("meal-note-button", owner_html)
        self.assertIn("data-meal-diner", owner_html)
        self.assertNotIn('onclick="toggleMealDiner(\'\'', owner_html)
        next_meal = owner_html[owner_html.index('data-meal="today_dinner"'):owner_html.index('data-meal="breakfast"')]
        for label in ("修改人数", "添加备注", "添加菜品", "智能补充", "删除整餐", "搜索更换"):
            self.assertIn(label, next_meal)
        self.assertIn("cycleDish(this,1,10)", next_meal)
        self.assertIn("removeDish(1,10)", next_meal)

if __name__ == "__main__":
    unittest.main()
