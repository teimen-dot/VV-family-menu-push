#!/usr/bin/env python3
"""Task A regression tests. Uses only a disposable SQLite database."""

import importlib
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def send(self, title, content):
        self.calls.append((title, content))
        if self.fail:
            raise RuntimeError("mock delivery failed")
        return {"code": 200, "data": "mock-id"}


class TaskATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "task_a.db")
        self.photos_dir = os.path.join(self.tmp.name, "photos")
        os.environ["FAMILY_MENU_DB_PATH"] = self.db_path
        os.environ["FAMILY_MENU_PHOTOS_DIR"] = self.photos_dir
        os.environ["H5_BASE_URL"] = "https://menu.example.test"
        os.environ["APP_ENV"] = "production"
        os.environ["PUSH_ENABLED"] = "true"

        import db
        import push_service
        import menu_service
        import photo_manager
        import app
        importlib.reload(db)
        importlib.reload(push_service)
        importlib.reload(menu_service)
        importlib.reload(photo_manager)
        importlib.reload(app)
        self.db = db
        self.push_service = push_service
        self.menu_service = menu_service
        self.photo_manager = photo_manager
        self.app = app
        db.init_db()
        self._seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        os.makedirs(self.photos_dir, exist_ok=True)
        with open(os.path.join(self.photos_dir, "breakfast.jpg"), "wb") as image_file:
            image_file.write(b"historical-image")
        conn = self.db.get_db()
        try:
            conn.execute("INSERT INTO categories(id,label_cn,label_en) VALUES('protein_main','主菜','Main')")
            for did, cn, en, active, image in (
                ("dish_0001", "SQLite 早餐", "SQLite Breakfast", 1, "breakfast.jpg"),
                ("dish_0002", "SQLite 午餐", "SQLite Lunch", 1, "lunch.jpg"),
                ("dish_0003", "SQLite 晚餐", "SQLite Dinner", 1, "dinner.jpg"),
                ("dish_9999", "已下架菜", "Inactive Dish", 0, "inactive.jpg"),
            ):
                conn.execute(
                    "INSERT INTO dishes(id,name_cn,name_en,category_id,is_active,image,protein_types,vegetables,meal_tags,cooking_methods,custom_tags,meal_roles) "
                    "VALUES(?,?,?,?,?,?,'[]','[]','[]','[]','[]','[]')",
                    (did, cn, en, "protein_main", active, image),
                )
            conn.execute(
                "INSERT INTO menus(id,date,location,status,diners,diners_count) "
                "VALUES(1,'2030-01-02','shenzhen','draft','[]',4)"
            )
            for order, (did, meal) in enumerate((
                ("dish_0001", "breakfast"), ("dish_0002", "lunch"), ("dish_0003", "dinner")
            )):
                conn.execute(
                    "INSERT INTO menu_items(menu_id,dish_id,meal_type,sort_order,source) VALUES(1,?,?,?,'owner')",
                    (did, meal, order),
                )
            conn.commit()
        finally:
            conn.close()

    def _set_confirmed(self):
        conn = self.db.get_db()
        conn.execute("UPDATE menus SET status='confirmed',confirmed_at=datetime('now') WHERE id=1")
        conn.commit()
        conn.close()

    def test_01_draft_cannot_push(self):
        client = FakeClient()
        ok, _ = self.push_service.push_confirmed_menu(1, client=client)
        self.assertFalse(ok)
        self.assertEqual(client.calls, [])

    def test_02_confirmed_push_reads_sqlite_not_legacy_json(self):
        self._set_confirmed()
        for legacy in ("menu_data.json", "dish_pool.json", "photo_manifest.json"):
            with open(os.path.join(self.tmp.name, legacy), "w", encoding="utf-8") as f:
                f.write('{"bad":"LEGACY MUST NOT APPEAR"}')
        client = FakeClient()
        ok, _ = self.push_service.push_confirmed_menu(1, client=client)
        self.assertTrue(ok)
        body = client.calls[0][1]
        self.assertIn("SQLite 早餐", body)
        self.assertNotIn("LEGACY MUST NOT APPEAR", body)

    def test_03_success_log_and_duplicate_guard(self):
        self._set_confirmed()
        client = FakeClient()
        self.assertTrue(self.push_service.push_confirmed_menu(1, client=client)[0])
        self.assertTrue(self.push_service.push_confirmed_menu(1, client=client)[0])
        self.assertEqual(len(client.calls), 1)
        conn = self.db.get_db()
        row = conn.execute("SELECT status,pushed_at FROM push_logs").fetchone()
        menu = conn.execute("SELECT push_status,pushed_at FROM menus WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(row["status"], "success")
        self.assertTrue(row["pushed_at"])
        self.assertEqual(menu["push_status"], "success")
        self.assertTrue(menu["pushed_at"])

    def test_04_failure_never_marks_success(self):
        self._set_confirmed()
        ok, _ = self.push_service.push_confirmed_menu(1, client=FakeClient(fail=True))
        self.assertFalse(ok)
        conn = self.db.get_db()
        log = conn.execute("SELECT status,error FROM push_logs").fetchone()
        menu = conn.execute("SELECT push_status,pushed_at FROM menus WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(log["status"], "failed")
        self.assertIn("mock delivery failed", log["error"])
        self.assertEqual(menu["push_status"], "failed")
        self.assertIsNone(menu["pushed_at"])

    def test_05_changed_reconfirmed_menu_gets_new_revision(self):
        self._set_confirmed()
        first = FakeClient()
        self.assertTrue(self.push_service.push_confirmed_menu(1, client=first)[0])
        conn = self.db.get_db()
        conn.execute("UPDATE menus SET status='draft',confirmed_revision=NULL WHERE id=1")
        conn.execute("DELETE FROM menu_items WHERE menu_id=1 AND dish_id='dish_0003'")
        conn.execute("UPDATE menus SET status='confirmed',confirmed_at=datetime('now') WHERE id=1")
        conn.commit()
        conn.close()
        second = FakeClient()
        self.assertTrue(self.push_service.push_confirmed_menu(1, client=second)[0])
        conn = self.db.get_db()
        count = conn.execute("SELECT COUNT(*) FROM push_logs WHERE menu_id=1 AND status='success'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)
        self.assertEqual(len(second.calls), 1)

    def test_06_legacy_json_has_zero_formal_runtime_references(self):
        root = os.path.dirname(__file__)
        for filename in ("app.py", "photo_manager.py", "push_menu.py", "push_service.py"):
            with open(os.path.join(root, filename), encoding="utf-8") as source_file:
                source = source_file.read()
            for legacy in ("menu_data.json", "dish_pool.json", "photo_manifest.json"):
                self.assertNotIn(legacy, source, f"{filename} still references {legacy}")

    def test_07_photo_upload_updates_file_and_sqlite(self):
        os.makedirs(self.photos_dir, exist_ok=True)
        filename = self.photo_manager.store_photo_for_dish(
            "SQLite 早餐", "new_breakfast", b"\xff\xd8\xff\xe0test-jpeg"
        )
        self.assertTrue(os.path.exists(os.path.join(self.photos_dir, filename)))
        conn = self.db.get_db()
        image = conn.execute("SELECT image FROM dishes WHERE id='dish_0001'").fetchone()[0]
        conn.close()
        self.assertEqual(image, "new_breakfast.jpg")

    def test_08_inactive_dish_excluded_from_runtime_candidates(self):
        ids = {d["id"] for d in self.app.get_all_dishes()}
        self.assertNotIn("dish_9999", ids)
        self.menu_service.invalidate_catalog_cache()
        pool_ids = {d["id"] for d in self.menu_service._load_pool()["dishes"]}
        self.assertNotIn("dish_9999", pool_ids)
        self.assertFalse(self.menu_service.add_dish_to_menu(1, "dish_9999", "dinner"))
        item_id = self.db.get_db().execute("SELECT id FROM menu_items WHERE menu_id=1 LIMIT 1").fetchone()[0]
        self.assertFalse(self.menu_service.replace_dish_in_menu(1, item_id, "dish_9999")[0])

    def test_09_push_images_use_h5_public_url(self):
        self._set_confirmed()
        menu = self.push_service.load_menu_for_push(1)
        body = self.push_service.format_menu(menu)
        self.assertIn("https://menu.example.test/photos/breakfast.jpg", body)
        self.assertNotIn("raw.githubusercontent.com", body)

    def test_10_confirm_freezes_revision_then_can_deliver(self):
        result = self.menu_service.confirm_menu(1)
        self.assertTrue(result[0])
        conn = self.db.get_db()
        menu = conn.execute(
            "SELECT status,confirmed_at,confirmed_revision,push_status FROM menus WHERE id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(menu["status"], "confirmed")
        self.assertTrue(menu["confirmed_at"])
        self.assertTrue(menu["confirmed_revision"])
        self.assertEqual(menu["push_status"], "not_sent")
        self.assertTrue(self.push_service.push_confirmed_menu(1, client=FakeClient())[0])

    def test_11_soft_delete_preserves_image_and_history(self):
        image_path = os.path.join(self.photos_dir, "breakfast.jpg")
        ok, _ = self.photo_manager.soft_delete_dish("dish_0001")
        self.assertTrue(ok)
        conn = self.db.get_db()
        row = conn.execute("SELECT is_active,image FROM dishes WHERE id='dish_0001'").fetchone()
        conn.execute("UPDATE menus SET date=? WHERE id=1", ((date.today() - timedelta(days=1)).isoformat(),))
        conn.commit()
        item_id = conn.execute("SELECT id FROM menu_items WHERE menu_id=1 AND dish_id='dish_0002'").fetchone()[0]
        conn.close()
        self.assertEqual(row["is_active"], 0)
        self.assertEqual(row["image"], "breakfast.jpg")
        self.assertTrue(os.path.exists(image_path))
        self.assertNotIn("dish_0001", {d["id"] for d in self.app.get_all_dishes(search="SQLite")})
        self.assertFalse(self.menu_service.add_dish_to_menu(1, "dish_0001", "dinner"))
        self.assertFalse(self.menu_service.replace_dish_in_menu(1, item_id, "dish_0001")[0])
        self.menu_service.invalidate_catalog_cache()
        self.assertNotIn("dish_0001", {d["id"] for d in self.menu_service._load_pool()["dishes"]})
        history = self.app.get_history_menus(30)
        archived = [d for menu in history for d in menu["meals"]["breakfast"] if d["name_cn"] == "SQLite 早餐"]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["image"], "breakfast.jpg")

    def test_12_push_disabled_blocks_client_and_persists_disabled(self):
        self._set_confirmed()
        os.environ["APP_ENV"] = "development"
        os.environ["PUSH_ENABLED"] = "false"
        client = FakeClient()
        ok, message = self.push_service.push_confirmed_menu(1, client=client)
        self.assertFalse(ok)
        self.assertIn("实际推送已禁用", message)
        self.assertEqual(client.calls, [])
        conn = self.db.get_db()
        log = conn.execute("SELECT status FROM push_logs WHERE menu_id=1").fetchone()[0]
        menu = conn.execute("SELECT push_status,pushed_at FROM menus WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(log, "disabled")
        self.assertEqual(menu["push_status"], "disabled")
        self.assertIsNone(menu["pushed_at"])

    def test_13_both_production_switches_are_required(self):
        cases = (("development", "true"), ("production", "false"), ("development", "false"))
        for app_env, enabled in cases:
            os.environ["APP_ENV"] = app_env
            os.environ["PUSH_ENABLED"] = enabled
            self.assertFalse(self.push_service.push_is_enabled())
        os.environ["APP_ENV"] = "production"
        os.environ["PUSH_ENABLED"] = "true"
        self.assertTrue(self.push_service.push_is_enabled())

    def test_14_development_confirm_saves_but_delivery_is_disabled(self):
        os.environ["APP_ENV"] = "development"
        os.environ["PUSH_ENABLED"] = "false"
        self.assertTrue(self.menu_service.confirm_menu(1)[0])
        client = FakeClient()
        ok, _ = self.push_service.push_confirmed_menu(1, client=client)
        self.assertFalse(ok)
        self.assertEqual(client.calls, [])
        conn = self.db.get_db()
        menu = conn.execute(
            "SELECT status,confirmed_at,push_status,pushed_at FROM menus WHERE id=1"
        ).fetchone()
        log = conn.execute("SELECT status FROM push_logs WHERE menu_id=1").fetchone()[0]
        conn.close()
        self.assertEqual(menu["status"], "confirmed")
        self.assertTrue(menu["confirmed_at"])
        self.assertEqual(menu["push_status"], "disabled")
        self.assertIsNone(menu["pushed_at"])
        self.assertEqual(log, "disabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
