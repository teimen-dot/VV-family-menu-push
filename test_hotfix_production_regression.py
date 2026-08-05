import os
import unittest
from unittest.mock import patch

import app
import runtime_config


TEST_ENV = {
    "APP_ENV": "development",
    "OWNER_AUTH_USERNAME": "vivian",
    "WORKER_AUTH_USERNAME": "kitchen",
    "SESSION_SECRET": "test-session-secret-0123456789abcdef",
    "PUSH_ENABLED": "false",
    "PUSH_ON_CONFIRM": "false",
}


class SessionTests(unittest.TestCase):
    def test_signed_session_is_30_days_and_restart_safe(self):
        with patch.dict(os.environ, TEST_ENV, clear=False):
            token = app.create_session("vivian", "owner")
            refreshed, session = app.session_from_cookie(f"{app.SESSION_COOKIE_NAME}={token}")
            self.assertEqual(session["role"], "owner")
            self.assertNotEqual(refreshed, token)
            self.assertEqual(app.SESSION_TTL_SECONDS, 30 * 24 * 60 * 60)
            self.assertIsNone(app.session_from_cookie(f"{app.SESSION_COOKIE_NAME}={token}x")[1])

    def test_production_requires_fixed_session_secret(self):
        with patch.dict(os.environ, {"APP_ENV": "production", "H5_BASE_URL": "https://menu.ourmenu.site", "SESSION_SECRET": ""}, clear=False):
            with self.assertRaises(ValueError):
                runtime_config.validate_app_startup()


class MarkupTests(unittest.TestCase):
    def test_breakpoints_and_owner_controls(self):
        self.assertIn("@media(min-width:1024px){.dishes-page .dish-grid{grid-template-columns:repeat(3", app.CSS)
        self.assertIn("@media(min-width:1400px){.dishes-page .dish-grid{grid-template-columns:repeat(4", app.CSS)
        self.assertIn("@media(min-width:961px){.desktop-confirm{display:grid}.tomorrow-actions{display:none}", app.CSS)

    def test_kitchen_write_permissions(self):
        for path in (
            "/api/tomorrow/add", "/api/tomorrow/ai-fill", "/api/tomorrow/repair",
            "/api/tomorrow/confirm", "/api/tomorrow/diners", "/api/tomorrow/meal-mode",
        ):
            self.assertFalse(app.post_path_allowed("worker", path), path)

    def test_do_post_uses_module_database_function(self):
        self.assertNotIn("get_db", app.AppHandler.do_POST.__code__.co_varnames)

    def test_empty_required_meals_share_validation_and_render_all_gaps(self):
        menu = {
            "exists": True, "menu_id": 1, "date": "2026-08-06", "status": "draft",
            "confirmed_at": None, "pushed_at": None, "push_status": "not_sent",
            "location": "shenzhen", "shortages": {}, "review_issues": "",
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
             patch.object(app, "get_menu_meal_mode", return_value={"meal_mode": "daily", "banquet_total_diners": None}), \
             patch.object(app, "validate_menu_meals", return_value={"meal_slots": slots, "warnings": []}), \
             patch.object(app, "get_menu_purchase_requests", return_value=[]):
            owner_html = app.render_tomorrow("owner", "shenzhen")
            worker_html = app.render_tomorrow("worker", "shenzhen")
        self.assertIn("早餐缺少：粥1份", owner_html)
        self.assertIn("午餐缺少：快手汤1份", owner_html)
        self.assertIn("晚餐缺少：蛋白质2份", owner_html)
        self.assertNotIn("下午茶缺少", owner_html)
        self.assertIn('class="desktop-confirm"', owner_html)
        self.assertNotIn('class="desktop-confirm"', worker_html)

    def test_production_preview_branch_uses_shared_gaps_and_desktop_actions(self):
        menu = {
            "exists": True, "menu_id": 1, "date": "2026-08-06", "status": "draft",
            "push_status": "not_sent", "meals": {
                "breakfast": [], "lunch": [], "afternoon_snack": [], "dinner": [],
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
             patch.object(app, "get_menu_meal_mode", return_value={"meal_mode": "daily", "banquet_total_diners": None}), \
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

if __name__ == "__main__":
    unittest.main()
