#!/usr/bin/env python3
import importlib
import os
import tempfile
import unittest
from datetime import date, timedelta


class MealPlanExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_env = {key: os.environ.get(key) for key in (
            "FAMILY_MENU_DB_PATH", "FAMILY_MENU_PHOTOS_DIR", "LOCAL_PREVIEW_UI",
            "APP_ENV", "PUSH_ENABLED",
        )}
        os.environ.update({
            "FAMILY_MENU_DB_PATH": os.path.join(self.tmp.name, "test.db"),
            "FAMILY_MENU_PHOTOS_DIR": os.path.join(self.tmp.name, "photos"),
            "LOCAL_PREVIEW_UI": "true", "APP_ENV": "development", "PUSH_ENABLED": "false",
        })
        import db, menu_service, push_service, app
        for module in (db, menu_service, push_service, app):
            importlib.reload(module)
        self.db, self.push_service, self.app = db, push_service, app
        db.init_db()
        self.today = date.today().isoformat()
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self._seed()

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import db, menu_service, push_service, app
        for module in (db, menu_service, push_service, app):
            importlib.reload(module)
        self.tmp.cleanup()

    def _seed(self):
        conn = self.db.get_db()
        conn.execute("INSERT INTO diners(id,name_cn,name_en,default_attends,sort_order) VALUES('vv','VV','VV',1,1)")
        conn.execute("INSERT INTO diners(id,name_cn,name_en,default_attends,sort_order) VALUES('sir','先生','Sir',1,2)")
        conn.execute("INSERT INTO categories(id,label_cn,label_en,active) VALUES('protein','蛋白质','Protein',1)")
        conn.execute("INSERT INTO dishes(id,name_cn,name_en,category_id,meal_tags,is_active) VALUES('dinner_dish','晚餐菜','Dinner dish','protein','[\"dinner\"]',1)")
        conn.execute("INSERT INTO dishes(id,name_cn,name_en,category_id,meal_tags,is_active) VALUES('lunch_dish','午餐菜','Lunch dish','protein','[\"lunch\"]',1)")
        conn.execute("INSERT INTO menus(id,date,location,status,diners) VALUES(1,?,'shenzhen','draft','[\"vv\",\"sir\"]')", (self.today,))
        conn.execute("INSERT INTO menus(id,date,location,status,diners) VALUES(2,?,'shenzhen','draft','[\"vv\",\"sir\"]')", (self.tomorrow,))
        conn.execute("INSERT INTO menu_items(id,menu_id,dish_id,meal_type,sort_order) VALUES(1,1,'dinner_dish','dinner',1)")
        conn.execute("INSERT INTO menu_items(id,menu_id,dish_id,meal_type,sort_order) VALUES(2,2,'lunch_dish','lunch',1)")
        conn.commit()
        conn.close()

    def test_real_dates_and_existing_ui_actions_render(self):
        html = self.app.render_tomorrow("owner", "shenzhen")
        self.assertIn("餐单", html)
        self.assertIn("Meal Plan", html)
        today_block = html.split("今天", 1)[1].split("明天", 1)[0]
        self.assertIn("晚餐", today_block)
        self.assertNotIn("早餐", today_block)
        self.assertIn("智能换一道 Smart replace", html)
        self.assertIn("搜索更换 Search replace", html)
        self.assertIn("删除 Delete", html)
        self.assertIn("meal-note-button", html)
        self.assertIn("meal-skip-button", html)
        self.assertNotIn("···", html)
        self.assertIn("＋", html)
        self.assertIn("Add breakfast", html)

    def test_meal_diners_note_skip_restore_and_clear_persist(self):
        ok, _ = self.app.update_meal_setting(2, "lunch", diners_marker=True, diners=["vv"])
        self.assertTrue(ok)
        self.app.update_meal_setting(2, "lunch", note_marker=True, note="19:00开饭")
        self.app.update_meal_setting(2, "lunch", skipped_marker=True, skipped=True)
        setting = self.app.get_meal_settings(2)["lunch"]
        self.assertEqual(setting["diners"], ["vv"])
        self.assertEqual(setting["note"], "19:00开饭")
        self.assertTrue(setting["is_skipped"])
        rendered = self.app.render_tomorrow("owner", "shenzhen")
        self.assertNotIn("已单独设置", rendered)
        self.assertIn("修改备注", rendered)
        self.assertIn("恢复本餐", rendered)
        conn = self.db.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM menu_items WHERE menu_id=2 AND meal_type='lunch'").fetchone()[0], 1)
        conn.close()
        pushed = self.push_service.load_menu_for_push(2)
        self.assertEqual(pushed["items"], [])
        self.app.update_meal_setting(2, "lunch", skipped_marker=True, skipped=False)
        self.assertFalse(self.app.get_meal_settings(2)["lunch"]["is_skipped"])
        ok, count = self.app.clear_menu_meal(2, "lunch")
        self.assertTrue(ok)
        self.assertEqual(count, 1)

    def test_kitchen_is_read_only_but_sees_note_and_skip(self):
        self.app.update_meal_setting(2, "lunch", note_marker=True, note="少放盐")
        worker = self.app.render_tomorrow("worker", "shenzhen")
        self.assertIn("备注：少放盐", worker)
        self.assertNotIn("智能换一道 Smart replace", worker)
        for path in (
            "/api/meal-plan/meal-diners", "/api/meal-plan/note",
            "/api/meal-plan/meal-state", "/api/meal-plan/clear-meal",
        ):
            self.assertFalse(self.app.post_path_allowed("worker", path), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
