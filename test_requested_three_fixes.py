#!/usr/bin/env python3
import importlib
import json
import os
import tempfile
import unittest
from datetime import date, timedelta


class RequestedThreeFixesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["FAMILY_MENU_DB_PATH"] = os.path.join(self.tempdir.name, "test.db")
        os.environ["APP_ENV"] = "development"
        os.environ["PUSH_ENABLED"] = "false"
        os.environ["PUSH_ON_CONFIRM"] = "false"
        os.environ["OWNER_AUTH_USERNAME"] = "vivian"
        os.environ["WORKER_AUTH_USERNAME"] = "kitchen"
        os.environ.pop("LOCAL_PREVIEW_UI", None)

        import db
        import inventory
        import menu_service
        import push_service
        import app
        for module in (db, inventory, menu_service, push_service, app):
            importlib.reload(module)
        self.db = db
        self.inventory = inventory
        self.menu_service = menu_service
        self.app = app
        db.init_db()
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self._seed()

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed(self):
        conn = self.db.get_db()
        try:
            conn.execute("INSERT INTO categories(id,label_cn,label_en) VALUES('egg_tofu','蛋豆','Egg/soy')")
            conn.executemany(
                "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES(?,?,?)",
                [("egg", "鸡蛋", "Egg"), ("tofu", "豆腐", "Tofu"), ("rice", "米", "Rice")],
            )
            dishes = [
                ("dish_egg", "蛋卷", '["egg"]', '["breakfast"]', '["egg_dish","tofu_dish"]'),
                ("dish_tofu", "嫩豆腐", '["tofu"]', '["breakfast"]', '["tofu_dish"]'),
            ]
            for dish_id, name, proteins, tags, roles in dishes:
                conn.execute(
                    "INSERT INTO dishes(id,name_cn,category_id,protein_types,meal_tags,meal_roles,is_active) "
                    "VALUES(?,?,'egg_tofu',?,?,?,1)",
                    (dish_id, name, proteins, tags, roles),
                )
            conn.executemany(
                "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES(?,?,1)",
                [("dish_egg", "egg"), ("dish_tofu", "tofu")],
            )
            conn.execute(
                "INSERT INTO menus(id,date,location,status,diners) VALUES(1,?,'shenzhen','draft','[]')",
                (self.tomorrow,),
            )
            conn.commit()
        finally:
            conn.close()
        self.inventory.add_ingredient_to_pantry("shenzhen", "egg")
        self.inventory.add_ingredient_to_pantry("shenzhen", "tofu")
        self.menu_service.invalidate_catalog_cache()

    def _breakfast_missing(self):
        from rule_engine import MealState, NutritionAnalyzer, analyze_meal_slots
        pool = {dish["id"]: dish for dish in self.menu_service._load_pool()["dishes"]}
        conn = self.db.get_db()
        rows = conn.execute(
            "SELECT dish_id FROM menu_items WHERE menu_id=1 AND meal_type='breakfast'"
        ).fetchall()
        conn.close()
        state = MealState()
        for row in rows:
            state.add_dish(NutritionAnalyzer.analyze(pool[row["dish_id"]]))
        return {
            slot for slot, value in analyze_meal_slots("breakfast", state).items()
            if value["missing_min"] > 0
        }

    def test_1_confirmed_owner_can_edit_and_reconfirm_new_revision(self):
        self.assertTrue(self.menu_service.confirm_menu(1)[0])
        conn = self.db.get_db()
        first_revision = conn.execute("SELECT confirmed_revision FROM menus WHERE id=1").fetchone()[0]
        conn.close()
        owner_html = self.app.render_tomorrow("owner", "shenzhen")
        worker_html = self.app.render_tomorrow("worker", "shenzhen")
        self.assertIn("修改菜单 / Edit menu", owner_html)
        self.assertNotIn("修改菜单 / Edit menu", worker_html)
        self.assertNotIn('<button class="meal-act-btn"', owner_html)
        os.environ["LOCAL_PREVIEW_UI"] = "true"
        preview_owner = self.app.render_tomorrow("owner", "shenzhen")
        preview_worker = self.app.render_tomorrow("worker", "shenzhen")
        self.assertEqual(preview_owner.count('<button class="secondary-button" onclick="editMenu()">'), 2)
        self.assertNotIn('onclick="editMenu()"', preview_worker)
        os.environ.pop("LOCAL_PREVIEW_UI", None)
        self.assertFalse(self.app.post_path_allowed("worker", "/api/tomorrow/revert"))
        self.assertTrue(self.menu_service.revert_to_draft(1)[0])
        draft_html = self.app.render_tomorrow("owner", "shenzhen")
        self.assertIn("AI 补充 AI Fill", draft_html)
        self.assertIn("确认菜单", draft_html)
        self.assertTrue(self.menu_service.confirm_menu(1)[0])
        conn = self.db.get_db()
        second_revision = conn.execute("SELECT confirmed_revision FROM menus WHERE id=1").fetchone()[0]
        conn.close()
        self.assertNotEqual(first_revision, second_revision)

    def test_2_egg_and_soy_gaps_and_ai_fill_are_independent(self):
        ok, _, review = self.menu_service.ai_fill_menu(1, "shenzhen", seed=42, meal_type="breakfast")
        self.assertTrue(ok)
        self.assertEqual({item["slot_role"] for item in review["added_details"]}, {"egg", "tofu"})
        self.assertNotIn("egg", self._breakfast_missing())
        self.assertNotIn("tofu", self._breakfast_missing())
        conn = self.db.get_db()
        conn.execute("DELETE FROM menu_items WHERE menu_id=1 AND dish_id='dish_egg'")
        conn.commit()
        conn.close()
        self.assertIn("egg", self._breakfast_missing())
        self.assertNotIn("tofu", self._breakfast_missing())
        conn = self.db.get_db()
        conn.execute("DELETE FROM menu_items WHERE menu_id=1 AND dish_id='dish_tofu'")
        conn.commit()
        conn.close()
        self.assertIn("egg", self._breakfast_missing())
        self.assertIn("tofu", self._breakfast_missing())

    def test_3_common_ingredients_are_location_specific_ranked_and_persistent(self):
        self.inventory.add_ingredient_to_pantry("shenzhen", "rice")
        self.inventory.remove_ingredient_from_pantry("shenzhen", "rice")
        self.inventory.add_ingredient_to_pantry("shenzhen", "rice")
        self.inventory.add_ingredient_to_pantry("hongkong", "tofu")
        shenzhen = self.inventory.get_common_ingredients_static("shenzhen")
        hongkong = self.inventory.get_common_ingredients_static("hongkong")
        self.assertEqual(shenzhen[0]["ingredient_id"], "rice")
        self.assertEqual([item["ingredient_id"] for item in hongkong], ["tofu"])
        self.assertLessEqual(len(shenzhen), 15)
        importlib.reload(self.inventory)
        refreshed = self.inventory.get_common_ingredients_static("shenzhen")
        self.assertEqual(refreshed[0]["ingredient_id"], "rice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
