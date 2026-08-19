#!/usr/bin/env python3
"""T-002 frozen-rule black-box acceptance and UI contracts."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app
import db
import menu_service
from rule_engine import GapFiller, MealState, NutritionAnalyzer, get_rotation_context


def dish(dish_id, roles, meals, *, proteins=None, category="protein_main",
         carb_type=None, companion=None, name=None):
    proteins = list(proteins or [])
    ingredient_ids = []
    if "egg" in proteins or "egg_dish" in roles:
        ingredient_ids.append("鸡蛋")
    if "tofu" in proteins or "tofu_dish" in roles:
        ingredient_ids.append("tofu")
    return {
        "id": dish_id,
        "name_cn": name or dish_id,
        "name_en": dish_id,
        "category_id": category,
        "meal_roles": list(roles),
        "meal_tags": list(meals),
        "protein_types": proteins,
        "ingredient_ids": ingredient_ids,
        "vegetables": [dish_id] if "vegetable_dish" in roles else [],
        "cooking_methods": (
            ["cold_mix"] if "tofu_dish" in roles
            else ["steam" if dish_id.endswith("0") else "stir_fry"]
        ),
        "carb_type": carb_type,
        "breakfast_staple_type": companion,
        "taste": "normal",
        "banquet": False,
        "drink": None,
        "ingredients_pending": False,
        "pending_review": None,
        "manual_only_for_breakfast": False,
    }


def complete_pool():
    rows = []
    for index in range(4):
        rows.append(dish(f"porridge_{index}", [], ["breakfast"], category="staple_carb", carb_type="porridge"))
        rows.append(dish(f"companion_{index}", ["staple"], ["breakfast"], category="staple_carb", carb_type="dim_sum", companion="bao"))
        rows.append(dish(f"egg_{index}", ["egg_dish"], ["breakfast"], proteins=["egg"], category="egg_tofu"))
        rows.append(dish(f"tofu_{index}", ["tofu_dish"], ["breakfast"], proteins=["tofu"], category="egg_tofu"))
        rows.append(dish(f"grain_{index}", ["staple"], ["breakfast"], category="staple_carb", carb_type="coarse_grain"))
        rows.append(dish(f"quick_{index}", ["quick_soup"], ["lunch"], category="soup"))
        rows.append(dish(f"slow_{index}", ["slow_soup"], ["dinner"], category="soup"))
    for index in range(24):
        rows.append(dish(f"veg_{index:02d}", ["vegetable_dish"], ["breakfast", "lunch", "dinner"], category="vegetable_mushroom"))
    proteins = ["chicken", "beef", "pork", "fish", "shrimp"]
    for index in range(4):
        rows.append(dish(
            f"breakfast_meat_{index}", ["protein_main"], ["breakfast"],
            proteins=[proteins[index]],
        ))
    for index in range(20):
        rows.append(dish(f"meat_{index:02d}", ["protein_main"], ["lunch", "dinner"], proteins=[proteins[index % len(proteins)]]))
    for index in range(8):
        rows.append(dish(f"staple_{index}", ["staple"], ["lunch", "dinner"], category="staple_carb", carb_type="rice"))
    return {"dishes": rows}


class FrozenRuleBlackBoxTests(unittest.TestCase):
    def setUp(self):
        self.pool = complete_pool()

    def _day(self, diners=4, seed=9):
        return GapFiller(self.pool, seed=seed).generate_day(
            context={"historical_last_used": {}, "hard_locked_dish_ids": set()},
            diners_count=diners,
        )

    def test_01_breakfast_has_exactly_one_egg(self):
        result, _ = self._day()
        state = result["breakfast"]["state"]
        self.assertEqual(state.egg_dish_count, 1)
        self.assertEqual(len(result["breakfast"]["dishes"]), 8)

    def test_02_breakfast_has_one_independent_meat_slot(self):
        result, _ = self._day()
        breakfast = result["breakfast"]["dishes"]
        proteins = [
            row for row in breakfast
            if "protein_main" in row["meal_roles"]
            and set(row["proteins"]) & {"fish", "shrimp", "beef", "pork", "chicken"}
        ]
        self.assertEqual(len(proteins), 1)
        self.assertFalse(proteins[0]["is_soup"])
        self.assertEqual({role for row in breakfast for role in row["meal_roles"]} & {"egg_dish", "tofu_dish"}, {"egg_dish", "tofu_dish"})

    def test_03_automatic_day_has_at_most_two_eggs(self):
        result, _ = self._day()
        self.assertLessEqual(sum(result[meal]["state"].auto_egg_dish_count for meal in ("breakfast", "lunch", "dinner")), 2)

    def test_04_lunch_three_person_matrix(self):
        dishes, state, _ = GapFiller(self.pool, seed=2).generate_meal("lunch", context={}, diners_count=3)
        self.assertEqual((state.protein_count, state.vegetable_dish_count, state.carb_count, state.quick_soup_slot, len(dishes)), (2, 1, 1, 1, 5))

    def test_05_dinner_three_person_matrix(self):
        dishes, state, _ = GapFiller(self.pool, seed=2).generate_meal("dinner", context={}, diners_count=3)
        self.assertEqual((state.protein_count, state.vegetable_dish_count, state.carb_count, state.slow_soup_slot, len(dishes)), (2, 1, 1, 1, 5))

    def test_06_dinner_four_person_matrix(self):
        dishes, state, _ = GapFiller(self.pool, seed=2).generate_meal("dinner", context={}, diners_count=4)
        self.assertEqual((state.protein_count, state.vegetable_dish_count, state.carb_count, state.slow_soup_slot, len(dishes)), (2, 2, 1, 1, 6))

    def test_07_tofu_does_not_replace_meat_minimum(self):
        pool = {"dishes": [
            dish("meat", ["protein_main"], ["dinner"], proteins=["chicken"]),
            dish("tofu", ["protein_main", "tofu_dish"], ["dinner"], proteins=["tofu"], category="egg_tofu"),
            *[dish(f"v{i}", ["vegetable_dish"], ["dinner"], category="vegetable_mushroom") for i in range(2)],
            dish("rice", ["staple"], ["dinner"], category="staple_carb", carb_type="rice"),
            dish("slow", ["slow_soup"], ["dinner"], category="soup"),
        ]}
        _, state, _ = GapFiller(pool, seed=1).generate_meal("dinner", context={}, diners_count=4)
        self.assertEqual(state.meat_main_count, 1)
        self.assertEqual(state.protein_count, 2)

    def test_08_two_proteins_prefer_different_sources(self):
        _, state, _ = GapFiller(self.pool, seed=3).generate_meal("lunch", context={}, diners_count=3)
        proteins = [row["proteins"][0] for row in state.dishes if "protein_main" in row["meal_roles"]]
        self.assertEqual(len(set(proteins)), 2)

    def test_09_soup_ingredients_do_not_fill_main_slots(self):
        soup = NutritionAnalyzer.analyze(dish("soup", ["slow_soup"], ["dinner"], proteins=["chicken"], category="soup"))
        state = MealState()
        state.add_dish(soup)
        self.assertEqual((state.protein_count, state.vegetable_dish_count, state.egg_dish_count), (0, 0, 0))

    def test_12_small_pool_degrades_with_warning_and_no_blank(self):
        only = dish("only_veg", ["vegetable_dish"], ["lunch"], category="vegetable_mushroom")
        filler = GapFiller({"dishes": [only]}, seed=1)
        candidates, warning = filler.get_slot_candidates(
            "lunch", "vegetable_dish", MealState(),
            {"hard_locked_dish_ids": {"only_veg"}}, exclude_ids={"only_veg"},
        )
        self.assertEqual([row["id"] for row in candidates], ["only_veg"])
        self.assertIn("建议补录", warning)

    def test_14_automatic_breakfast_tangjiao_does_not_cover_whole_meal(self):
        pool = complete_pool()
        pool["dishes"] = [row for row in pool["dishes"] if not row["id"].startswith("companion_")]
        pool["dishes"].append(dish("tangjiao", ["one_pot_meal", "staple"], ["breakfast"], category="staple_carb", carb_type="dim_sum", companion="bao", name="汤饺"))
        rows, state, _ = GapFiller(pool, seed=1).generate_meal("breakfast", context={})
        self.assertIn("tangjiao", {row["id"] for row in rows})
        self.assertFalse(state.has_manual_one_pot_meal)
        self.assertGreater(len(rows), 1)


class RotationAndPersistenceBlackBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "t002.db"))
        self.patch.start()
        db.init_db()
        conn = db.get_db()
        conn.execute("INSERT INTO categories(id,label_cn,label_en) VALUES('protein_main','主菜','Main')")
        conn.execute("INSERT INTO categories(id,label_cn,label_en) VALUES('one_pot_meal','一餐型','One pot')")
        for dish_id in ("old", "new", "hk"):
            conn.execute("INSERT INTO dishes(id,name_cn,name_en,is_active,meal_tags) VALUES(?,?,?,1,'[\"breakfast\",\"lunch\",\"dinner\"]')", (dish_id, dish_id, dish_id))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _menu_item(self, day, location, dish_id, status="draft", meal="dinner"):
        conn = db.get_db()
        menu_id = conn.execute("INSERT INTO menus(date,location,status,diners_count) VALUES(?,?,?,4)", (day, location, status)).lastrowid
        item_id = conn.execute("INSERT INTO menu_items(menu_id,dish_id,meal_type,source) VALUES(?,?,?,'ai')", (menu_id, dish_id, meal)).lastrowid
        conn.commit()
        conn.close()
        return menu_id, item_id

    def test_10_four_day_lock_and_day_five_release(self):
        self._menu_item("2099-01-01", "shenzhen", "old", status="confirmed")
        self.assertIn("old", get_rotation_context("2099-01-04", "shenzhen")["hard_locked_dish_ids"])
        self.assertNotIn("old", get_rotation_context("2099-01-05", "shenzhen")["hard_locked_dish_ids"])

    def test_11_replace_delete_and_cancel_release_reservations(self):
        menu_id, item_id = self._menu_item("2099-01-02", "shenzhen", "old")
        self.assertIn("old", get_rotation_context("2099-01-03", "shenzhen")["hard_locked_dish_ids"])
        self.assertTrue(menu_service.replace_dish_in_menu(menu_id, item_id, "new")[0])
        locked = get_rotation_context("2099-01-03", "shenzhen")["hard_locked_dish_ids"]
        self.assertNotIn("old", locked)
        self.assertIn("new", locked)
        self.assertTrue(menu_service.remove_dish_from_menu(menu_id, item_id)[0])
        self.assertNotIn("new", get_rotation_context("2099-01-03", "shenzhen")["hard_locked_dish_ids"])
        conn = db.get_db()
        conn.execute("INSERT INTO menu_items(menu_id,dish_id,meal_type,source) VALUES(?,?,'breakfast','ai')", (menu_id, "old"))
        conn.commit()
        conn.close()
        self.assertTrue(menu_service.set_menu_meal_skipped(menu_id, "breakfast", True)[0])
        self.assertNotIn("old", get_rotation_context("2099-01-03", "shenzhen")["hard_locked_dish_ids"])

    def test_13_manual_onepot_makes_ai_fill_zero_additions(self):
        conn = db.get_db()
        conn.execute("UPDATE dishes SET meal_roles='[\"one_pot_meal\"]',category_id='one_pot_meal' WHERE id='old'")
        menu_id = conn.execute("INSERT INTO menus(date,location,status,diners_count) VALUES('2099-02-01','shenzhen','draft',4)").lastrowid
        conn.execute("INSERT INTO menu_items(menu_id,dish_id,meal_type,is_locked,source) VALUES(?,?,'lunch',1,'owner')", (menu_id, "old"))
        conn.commit()
        conn.close()
        pool = {"dishes": [dish("old", ["one_pot_meal"], ["lunch"], category="one_pot_meal")]}
        with patch.object(menu_service, "_load_pool", return_value=pool), \
             patch.object(menu_service, "get_dish_ingredients_map", return_value={}), \
             patch.object(menu_service, "get_inventory_ingredients", return_value=(set(), set(), set())), \
             patch.object(menu_service, "get_preference_scores", return_value={}), \
             patch.object(menu_service, "check_dishes_availability_batch", return_value={"old": {"status": "available"}}):
            ok, _, review = menu_service.ai_fill_menu(menu_id, "shenzhen", meal_type="lunch")
        self.assertTrue(ok)
        self.assertEqual(review["added"], [])

    def test_15_kitchen_rotation_and_inventory_context_are_isolated(self):
        self._menu_item("2099-03-01", "hongkong", "hk", status="confirmed")
        self.assertIn("hk", get_rotation_context("2099-03-03", "hongkong")["hard_locked_dish_ids"])
        self.assertNotIn("hk", get_rotation_context("2099-03-03", "shenzhen")["hard_locked_dish_ids"])

    def test_confirmed_menu_is_not_regenerated(self):
        menu_id, _ = self._menu_item("2099-04-01", "shenzhen", "old", status="confirmed")
        before = db.get_db().execute("SELECT dish_id FROM menu_items WHERE menu_id=?", (menu_id,)).fetchall()
        with patch.object(menu_service, "_load_pool", side_effect=AssertionError("must not load pool")):
            returned_id, review = menu_service.generate_and_store_menu("2099-04-01", "shenzhen")
        after_conn = db.get_db()
        after = after_conn.execute("SELECT dish_id FROM menu_items WHERE menu_id=?", (menu_id,)).fetchall()
        after_conn.close()
        self.assertEqual(returned_id, menu_id)
        self.assertTrue(review["protected"])
        self.assertEqual([row["dish_id"] for row in before], [row["dish_id"] for row in after])


class T002UIAndEnglishContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "public", "family-menu", "index.html")
        with open(path, encoding="utf-8") as handle:
            cls.html = handle.read()

    def test_swap_uses_real_ids_and_shared_lazy_renderer(self):
        self.assertIn("const DISH_DB = [];", self.html)
        self.assertIn("button.dataset.dishId = d.id", self.html)
        self.assertIn("new_dish_id:d.id", self.html)
        self.assertEqual(self.html.count("function makeDishTile("), 1)
        self.assertIn("image.loading = 'lazy'", self.html)
        self.assertIn("image.decoding = 'async'", self.html)
        self.assertIn("object-fit:cover", self.html)
        self.assertIn("return 'neutral'", self.html)

    def test_shanghai_bok_choy_english_is_resolved_without_backfill(self):
        self.assertEqual(app.resolve_ingredient_english_name("上海青"), "Shanghai Bok Choy")
        self.assertEqual(app.resolve_ingredient_english_name("上海青", "Existing"), "Existing")


if __name__ == "__main__":
    unittest.main()
