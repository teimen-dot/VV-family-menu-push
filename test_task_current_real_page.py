#!/usr/bin/env python3
import importlib
import os
import tempfile
import unittest
from datetime import date


class CurrentRealPageRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update({
            "FAMILY_MENU_DB_PATH": os.path.join(self.tmp.name, "test.db"),
            "APP_ENV": "development", "PUSH_ENABLED": "false",
            "LOCAL_PREVIEW_UI": "true",
        })
        import db, inventory, menu_service, app
        for module in (db, inventory, menu_service, app):
            importlib.reload(module)
        self.db, self.inventory, self.menu_service, self.app = db, inventory, menu_service, app
        db.init_db()
        self._seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        conn = self.db.get_db()
        try:
            conn.executemany(
                "INSERT INTO categories(id,label_cn,label_en) VALUES(?,?,?)",
                [("staple_carb", "主食", "Staple"), ("egg_tofu", "蛋豆", "Egg/soy"),
                 ("soup", "汤", "Soup"), ("protein_main", "主菜", "Main"),
                 ("vegetable_mushroom", "蔬菜", "Vegetable"),
                 ("one_pot_meal", "一餐型", "One pot"),
                 ("fruit_snack", "点心", "Snack")],
            )
            conn.executemany(
                "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES(?,?,?)",
                [("rice", "米饭", "Rice"), ("quinoa", "藜麦", "Quinoa"),
                 ("dumpling", "饺子", "Dumpling"), ("tofu", "豆腐", "Tofu"),
                 ("egg", "鸡蛋", "Egg"), ("beef", "牛肉", "Beef"),
                 ("greens", "青菜", "Greens")],
            )
            dishes = [
                ("dish_rice_plain", "纯白米饭", "staple_carb", "rice", "[]", '["dinner"]', '["staple"]', "[]"),
                ("dish_rice_quinoa", "三色藜麦饭", "staple_carb", "coarse_grain", "[]", '["dinner"]', '["staple"]', "[]"),
                ("dish_dumpling_soup", "汤饺", "staple_carb", "dim_sum", '["pork"]', '["breakfast"]', '["staple"]', "[]"),
                ("dish_fried_dumpling", "日式饺子 煎饺", "staple_carb", "dim_sum", "[]", '["breakfast"]', '["staple"]', "[]"),
                ("dish_tofu_cold", "黑鱼子酱配豆腐", "egg_tofu", None, '["tofu"]', '["breakfast"]', "[]", "[]"),
                ("dish_tofu_egg_soup", "丝瓜豆腐鸡蛋汤", "soup", None, '["egg","tofu"]', '["breakfast"]', '["quick_soup"]', "[]"),
                ("dish_lunch_beef", "牛肉粒", "protein_main", None, '["beef"]', '["lunch"]', '["protein_main"]', "[]"),
                ("dish_lunch_greens", "清炒青菜", "vegetable_mushroom", None, "[]", '["lunch"]', '["vegetable_dish"]', '["青菜"]'),
            ]
            conn.executemany(
                "INSERT INTO dishes(id,name_cn,category_id,carb_type,protein_types,meal_tags,meal_roles,vegetables,is_active,image) "
                "VALUES(?,?,?,?,?,?,?,?,1,'test.jpg')", dishes,
            )
            conn.executemany(
                "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES(?,?,1)",
                [("dish_rice_plain", "rice"), ("dish_rice_quinoa", "rice"), ("dish_rice_quinoa", "quinoa"),
                 ("dish_dumpling_soup", "dumpling"), ("dish_fried_dumpling", "dumpling"),
                 ("dish_tofu_cold", "tofu"), ("dish_tofu_egg_soup", "tofu"), ("dish_tofu_egg_soup", "egg"),
                 ("dish_lunch_beef", "beef"), ("dish_lunch_greens", "greens")],
            )
            conn.executemany(
                "INSERT INTO diners(id,name_cn,name_en,default_attends) VALUES(?,?,?,1)",
                [("a", "A", "A"), ("b", "B", "B"), ("c", "C", "C")],
            )
            conn.execute(
                "INSERT INTO menus(id,date,location,status,diners,diners_count) VALUES(1,?,'shenzhen','draft','[]',4)",
                (date.today().isoformat(),),
            )
            conn.executemany(
                "INSERT INTO menu_items(menu_id,dish_id,meal_type,sort_order) VALUES(1,?,?,?)",
                [("dish_rice_quinoa", "dinner", 0), ("dish_dumpling_soup", "breakfast", 0),
                 ("dish_tofu_cold", "breakfast", 1)],
            )
            conn.commit()
        finally:
            conn.close()
        for ingredient in ("quinoa", "dumpling", "tofu", "egg", "beef", "greens"):
            self.inventory.add_ingredient_to_pantry("shenzhen", ingredient)
        self.menu_service.invalidate_catalog_cache()

    def _item_id(self, dish_id):
        conn = self.db.get_db()
        try:
            return conn.execute("SELECT id FROM menu_items WHERE dish_id=?", (dish_id,)).fetchone()[0]
        finally:
            conn.close()

    def test_breakfast_dim_sum_satisfies_shared_companion_slot(self):
        from rule_engine import MealState, NutritionAnalyzer, analyze_meal_slots
        dish = next(d for d in self.menu_service._load_pool()["dishes"] if d["id"] == "dish_dumpling_soup")
        state = MealState()
        state.add_dish(NutritionAnalyzer.analyze(dish))
        self.assertEqual(analyze_meal_slots("breakfast", state, 2)["companion_staple"]["missing_min"], 0)

    def test_rice_family_cycles_to_plain_rice_without_pantry_rice(self):
        ok, _, replacement = self.app.smart_replace_menu_item(1, self._item_id("dish_rice_quinoa"), "shenzhen")
        self.assertTrue(ok)
        self.assertEqual(replacement, "dish_rice_plain")

    def test_dim_sum_full_pool_and_cross_category_tofu_pool(self):
        ok, _, replacement = self.app.smart_replace_menu_item(1, self._item_id("dish_dumpling_soup"), "shenzhen")
        self.assertTrue(ok)
        self.assertEqual(replacement, "dish_fried_dumpling")
        ok, _, replacement = self.app.smart_replace_menu_item(1, self._item_id("dish_tofu_cold"), "shenzhen")
        self.assertTrue(ok)
        self.assertEqual(replacement, "dish_tofu_egg_soup")

    def test_empty_menu_diners_uses_same_three_defaults_as_page_and_ai_fill(self):
        self.assertEqual(self.menu_service._get_effective_diners_count(menu_id=1), 3)
        ok, _, review = self.menu_service.ai_fill_menu(1, "shenzhen", seed=42, meal_type="lunch")
        self.assertTrue(ok)
        self.assertEqual(
            {item["slot_role"] for item in review["added_details"]},
            {"protein_main", "vegetable_dish"},
        )

    def test_future_unknown_names_join_carb_pools_from_fields_only(self):
        conn = self.db.get_db()
        try:
            conn.executemany(
                "INSERT INTO dishes(id,name_cn,category_id,carb_type,protein_types,meal_tags,meal_roles,vegetables,is_active,image) "
                "VALUES(?,?,?,?,?,?,?,?,1,'test.jpg')",
                [
                    ("dish_future_rice", "星云甲号", "one_pot_meal", "rice", "[]", '["dinner"]', "[]", "[]"),
                    ("dish_future_dim", "蓝图乙号", "fruit_snack", "dim_sum", "[]", '["breakfast"]', "[]", "[]"),
                ],
            )
            conn.executemany(
                "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES(?,?,1)",
                [("dish_future_rice", "rice"), ("dish_future_dim", "dumpling")],
            )
            conn.commit()
        finally:
            conn.close()

        self.inventory.add_ingredient_to_pantry("shenzhen", "rice")

        rice_item = self._item_id("dish_rice_quinoa")
        rice_seen = []
        for _ in range(4):
            ok, _, replacement = self.app.smart_replace_menu_item(1, rice_item, "shenzhen")
            self.assertTrue(ok)
            rice_seen.append(replacement)
        self.assertIn("dish_future_rice", rice_seen)

        dim_item = self._item_id("dish_dumpling_soup")
        dim_seen = []
        for _ in range(4):
            ok, _, replacement = self.app.smart_replace_menu_item(1, dim_item, "shenzhen")
            self.assertTrue(ok)
            dim_seen.append(replacement)
        self.assertIn("dish_future_dim", dim_seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
