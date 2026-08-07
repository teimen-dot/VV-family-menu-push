import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from menu_service import _fill_missing_slots_v8
from push_service import format_menu
from rule_engine import (
    GapFiller, MealState, NutritionAnalyzer, analyze_meal_slots,
    generate_afternoon_snack,
)


def dish(dish_id, name, category, meals, *, proteins=None, vegetables=None,
         carb_type=None, roles=None, quick=False, slow=False,
         breakfast_staple_type=None, manual_breakfast=False):
    return {
        "id": dish_id,
        "name_cn": name,
        "name_en": name,
        "category_id": category,
        "meal_tags": meals,
        "protein_types": proteins or [],
        "vegetables": vegetables or [],
        "carb_type": carb_type,
        "breakfast_staple_type": breakfast_staple_type,
        "meal_roles": roles or [],
        "quick_soup": 1 if quick else 0,
        "slow_soup": 1 if slow else 0,
        "manual_only_for_breakfast": 1 if manual_breakfast else 0,
        "cooking_methods": ["steam"],
        "custom_tags": [],
        "taste": "normal",
        "banquet": 0,
    }


BREAKFAST_POOL = [
    dish("porridge", "小米粥", "staple_carb", ["breakfast"], carb_type="porridge"),
    dish("bun", "包子", "staple_carb", ["breakfast"], carb_type="dim_sum"),
    dish("coarse", "玉米", "staple_carb", ["breakfast"], carb_type="coarse_grain"),
    dish("egg", "蒸蛋", "egg_tofu", ["breakfast"], proteins=["egg"]),
    dish("tofu", "凉拌豆腐", "egg_tofu", ["breakfast"], proteins=["tofu"]),
    dish("veg1", "清炒青菜", "vegetable_mushroom", ["breakfast"], vegetables=["青菜"]),
    dish("veg2", "炒西兰花", "vegetable_mushroom", ["breakfast"], vegetables=["西兰花"]),
    dish("meat", "鸡肉", "protein_main", ["breakfast"], proteins=["chicken"]),
    dish("breakfast_soup", "丝瓜鸡蛋汤", "soup", ["breakfast"], proteins=["egg"], vegetables=["丝瓜"], quick=True),
    dish("soy_milk", "豆浆", "egg_tofu", ["breakfast"], proteins=["tofu"], manual_breakfast=True),
]


MEAL_POOL = [
    dish("protein1", "鸡肉", "protein_main", ["lunch", "dinner"], proteins=["chicken"], roles=["protein_main"]),
    dish("protein2", "牛肉", "protein_main", ["lunch", "dinner"], proteins=["beef"], roles=["protein_main"]),
    dish("veg1", "青菜", "vegetable_mushroom", ["lunch", "dinner"], vegetables=["青菜"], roles=["vegetable_dish"]),
    dish("veg2", "西兰花", "vegetable_mushroom", ["lunch", "dinner"], vegetables=["西兰花"], roles=["vegetable_dish"]),
    dish("rice", "米饭", "staple_carb", ["lunch", "dinner"], carb_type="rice", roles=["staple"]),
    dish("quick", "紫菜蛋花汤", "soup", ["lunch", "dinner"], quick=True, roles=["quick_soup"]),
    dish("slow", "松茸鸡汤", "soup", ["lunch", "dinner"], slow=True, roles=["slow_soup"]),
    dish("onepot", "虾仁蔬菜饭", "one_pot_meal", ["lunch"], proteins=["shrimp"], vegetables=["青菜"], carb_type="rice", roles=["one_pot_meal"]),
]


class CoreMealStructureTests(unittest.TestCase):
    def test_breakfast_fixed_slots_follow_diner_count_without_extra_meat_or_soup(self):
        for diners, vegetables in ((2, 1), (3, 2), (5, 2)):
            dishes, state, _ = GapFiller({"dishes": BREAKFAST_POOL}, seed=7).generate_meal(
                "breakfast", diners_count=diners
            )
            slots = analyze_meal_slots("breakfast", state, diners)
            self.assertTrue(all(value["missing_min"] == 0 for value in slots.values()))
            self.assertEqual(1, state.porridge_slot)
            self.assertEqual(1, state.companion_staple_slot)
            self.assertEqual(1, state.coarse_grain_slot)
            self.assertEqual(1, state.egg_slot)
            self.assertEqual(1, state.tofu_slot)
            self.assertEqual(vegetables, state.vegetable_dish_count)
            self.assertFalse(any(item["category_id"] == "protein_main" for item in dishes))
            self.assertFalse(any(item["is_soup"] for item in dishes))
            self.assertNotIn("soy_milk", {item["id"] for item in dishes})

    def test_ai_fill_uses_existing_breakfast_slots_without_duplicate_porridge(self):
        analyzed = {item["id"]: NutritionAnalyzer.analyze(item) for item in BREAKFAST_POOL}
        state = MealState()
        state.add_dish(analyzed["porridge"], is_locked=True)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE menu_items(id INTEGER PRIMARY KEY,menu_id INTEGER,dish_id TEXT,meal_type TEXT,is_locked INTEGER,sort_order INTEGER,source TEXT)")
        conn.execute("INSERT INTO menu_items(menu_id,dish_id,meal_type,is_locked,sort_order,source) VALUES(1,'porridge','breakfast',1,0,'owner')")
        added = []
        with patch("menu_service.check_dishes_availability_batch", side_effect=lambda ids, _loc: {did: {"status": "available"} for did in ids}):
            _fill_missing_slots_v8(
                conn, 1, "breakfast", state, GapFiller({"dishes": BREAKFAST_POOL}, seed=3),
                analyzed, {}, {"porridge"}, set(), 2, "shenzhen", [], set(),
                set(analyzed), added,
            )
        ids = [row[0] for row in conn.execute("SELECT dish_id FROM menu_items ORDER BY id")]
        conn.close()
        self.assertEqual(1, ids.count("porridge"))
        self.assertNotIn("breakfast_soup", ids)
        self.assertNotIn("soy_milk", ids)
        self.assertTrue(all(value["missing_min"] == 0 for value in analyze_meal_slots("breakfast", state, 2).values()))

    def test_complete_one_pot_lunch_only_adds_quick_soup(self):
        dishes, state, _ = GapFiller({"dishes": MEAL_POOL}, seed=2).generate_meal(
            "lunch", locked_dish_ids=["onepot"], diners_count=4
        )
        self.assertTrue(state.has_complete_one_pot_meal)
        self.assertEqual({"onepot", "quick"}, {item["id"] for item in dishes})
        self.assertEqual(1, state.quick_soup_slot)

    def test_normal_lunch_uses_diner_targets_and_quick_soup(self):
        for diners, target in ((2, 1), (3, 2), (5, 2)):
            _, state, _ = GapFiller({"dishes": MEAL_POOL}, seed=4).generate_meal(
                "lunch", diners_count=diners
            )
            self.assertEqual(target, state.protein_count)
            self.assertEqual(target, state.vegetable_dish_count)
            self.assertEqual(1, state.carb_count)
            self.assertEqual(1, state.quick_soup_slot)
            self.assertEqual(0, state.slow_soup_slot)

    def test_dinner_uses_exact_diner_targets_and_slow_soup(self):
        expected = {2: (1, 1), 3: (2, 1), 4: (2, 2), 5: (2, 2)}
        for diners, (proteins, vegetables) in expected.items():
            _, state, _ = GapFiller({"dishes": MEAL_POOL}, seed=6).generate_meal(
                "dinner", diners_count=diners
            )
            self.assertEqual(proteins, state.protein_count)
            self.assertEqual(vegetables, state.vegetable_dish_count)
            self.assertEqual(1, state.carb_count)
            self.assertEqual(1, state.slow_soup_slot)
            self.assertEqual(0, state.quick_soup_slot)


class IntegrationBoundaryTests(unittest.TestCase):
    def test_afternoon_snack_remains_recommendable_and_browsable(self):
        fruit = dish("fruit", "水果", "fruit_snack", ["afternoon_snack"])
        recommendations = generate_afternoon_snack({"dishes": [fruit]})
        self.assertEqual(["fruit"], [item["id"] for item in recommendations])
        source = Path("app.py").read_text(encoding="utf-8")
        reference = source[source.index("def render_meal_plan_reference"):source.index("def render_tomorrow(")]
        self.assertIn('"afternoon_snack": ("下午茶", "Afternoon Tea"', reference)
        self.assertIn("添加菜品", reference)

    def test_afternoon_snack_is_excluded_from_formal_push(self):
        menu = {
            "date": "2099-01-01", "location": "shenzhen", "diner_names": [],
            "items": [
                {"meal_type": "afternoon_snack", "name_cn": "水果", "name_en": "Fruit", "image": None},
                {"meal_type": "dinner", "name_cn": "晚餐", "name_en": "Dinner", "image": None},
            ],
        }
        html = format_menu(menu, "https://example.test")
        self.assertNotIn("下午茶", html)
        self.assertNotIn("水果", html)
        self.assertIn("晚餐", html)

    def test_tonight_dinner_reuses_tomorrow_actions(self):
        source = Path("app.py").read_text(encoding="utf-8")
        reference = source[source.index("def render_meal_plan_reference"):source.index("def render_tomorrow(")]
        self.assertIn('menu_id == today_menu.get("menu_id")', reference)
        self.assertIn('meal_type == "dinner"', reference)
        for action in ("smartReplace", "openDishSearch", "removeDish"):
            self.assertIn(action, reference)
        self.assertIn("添加菜品", reference)


if __name__ == "__main__":
    unittest.main()
