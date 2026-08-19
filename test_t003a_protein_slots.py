#!/usr/bin/env python3
"""T-003A protein-slot degradation and real-data regression tests."""

import os
import unittest
from unittest.mock import patch

import db
import inventory
import menu_service
from rule_engine import (
    AUTO_POOL_MINIMUMS,
    GapFiller,
    MealState,
    get_rotation_context,
)
from test_t002_blackbox import complete_pool, dish


REAL_PREVIEW_DB = (
    "/Users/heymen/workbuddy/"
    "claw-family-ui-phase2-assets-20260819/test-family_menu.db"
)


class ProteinSlotDegradationTests(unittest.TestCase):
    def test_healthy_pool_size_is_measured_before_caps_and_four_day_lock(self):
        rows = [
            dish(
                f"meat_{index:02d}", ["protein_main"], ["lunch"],
                proteins=["chicken"],
            )
            for index in range(AUTO_POOL_MINIMUMS["meat_main"])
        ]
        filler = GapFiller({"dishes": rows}, seed=1)
        state = MealState()
        state.add_dish(filler.analyzed["meat_00"])
        locked = {row["id"] for row in rows}

        candidates, warning = filler.get_slot_candidates(
            "lunch", "meat_main", state,
            context={"hard_locked_dish_ids": locked},
            exclude_ids=locked,
        )

        self.assertEqual(candidates, [])
        self.assertIsNone(warning)
        self.assertEqual(
            filler.slot_pool_sizes[("lunch", "meat_main")],
            AUTO_POOL_MINIMUMS["meat_main"],
        )
        self.assertEqual(filler.degradation_warnings, [])
        self.assertIn(
            "lunch.meat_main 无任何合法候选",
            filler.hard_slot_warnings[("lunch", "meat_main")],
        )

        fresh_filler = GapFiller({"dishes": rows}, seed=1)
        candidates, warning = fresh_filler.get_slot_candidates(
            "lunch", "meat_main", MealState(),
            context={"hard_locked_dish_ids": {"meat_00"}},
        )
        self.assertNotIn("meat_00", {row["id"] for row in candidates})
        self.assertIsNone(warning)
        self.assertEqual(fresh_filler.degradation_warnings, [])

    def test_low_protein_pool_relaxes_only_window_lock_and_fills_matrix(self):
        rows = [
            dish("low_chicken", ["protein_main"], ["lunch"], proteins=["chicken"]),
            dish("low_fish", ["protein_main"], ["lunch"], proteins=["fish"]),
            *[
                dish(
                    f"veg_{index}", ["vegetable_dish"], ["lunch"],
                    category="vegetable_mushroom",
                )
                for index in range(2)
            ],
            dish(
                "rice", ["staple"], ["lunch"],
                category="staple_carb", carb_type="rice",
            ),
            dish("quick", ["quick_soup"], ["lunch"], category="soup"),
        ]
        filler = GapFiller({"dishes": rows}, seed=2)
        locked_proteins = {"low_chicken", "low_fish"}

        dishes, state, _ = filler.generate_meal(
            "lunch",
            context={"hard_locked_dish_ids": locked_proteins},
            diners_count=4,
        )

        self.assertEqual(state.protein_count, 2)
        self.assertGreaterEqual(state.meat_main_count, 1)
        protein_ids = [
            row["id"] for row in dishes if "protein_main" in row["meal_roles"]
        ]
        self.assertEqual(len(protein_ids), len(set(protein_ids)))
        self.assertTrue(
            any("该分类菜品不足，建议补录" in item
                for item in filler.degradation_warnings)
        )

    def test_true_shortage_is_explicit_and_tofu_never_fills_meat_slot(self):
        tofu = dish(
            "only_tofu", ["protein_main", "tofu_dish"], ["dinner"],
            proteins=["tofu"], category="egg_tofu",
        )
        filler = GapFiller({"dishes": [tofu]}, seed=3)

        _, state, _ = filler.generate_meal("dinner", context={}, diners_count=4)

        self.assertEqual(state.meat_main_count, 0)
        self.assertLess(state.protein_count, 2)
        self.assertIn(
            ("dinner", "meat_main"), filler.hard_slot_warnings
        )
        self.assertIn(
            "蛋白质/肉类必需槽位缺失",
            filler.hard_slot_warnings[("dinner", "meat_main")],
        )

    def test_breakfast_contract_is_unchanged(self):
        result, _ = GapFiller(complete_pool(), seed=4).generate_day(
            context={"historical_last_used": {}, "hard_locked_dish_ids": set()},
            diners_count=4,
        )
        state = result["breakfast"]["state"]
        self.assertEqual(len(result["breakfast"]["dishes"]), 7)
        self.assertEqual((state.egg_dish_count, state.tofu_dish_count), (1, 1))


@unittest.skipUnless(os.path.exists(REAL_PREVIEW_DB), "real preview DB is unavailable")
class RealDataProteinSlotTests(unittest.TestCase):
    def setUp(self):
        self.db_patch = patch.object(db, "DB_PATH", REAL_PREVIEW_DB)
        self.db_patch.start()
        inventory._availability_cache.clear()
        menu_service.invalidate_catalog_cache()

    def tearDown(self):
        inventory._availability_cache.clear()
        menu_service.invalidate_catalog_cache()
        self.db_patch.stop()

    def test_real_classification_pool_fills_both_four_person_meals_when_legal(self):
        pool = menu_service._load_pool()
        all_available = {
            row["id"]: "available" for row in pool["dishes"]
        }
        context = {
            "dish_availability": all_available,
            "hard_locked_dish_ids": set(),
            "historical_last_used": {},
        }

        for meal_type in ("lunch", "dinner"):
            with self.subTest(meal=meal_type):
                filler = GapFiller(pool, seed=31)
                _, state, _ = filler.generate_meal(
                    meal_type, context=context, diners_count=4
                )
                self.assertEqual(state.protein_count, 2)
                self.assertGreaterEqual(state.meat_main_count, 1)
                self.assertGreaterEqual(
                    filler.slot_pool_sizes[(meal_type, "meat_main")],
                    AUTO_POOL_MINIMUMS["meat_main"],
                )

    def test_real_inventory_shortage_is_not_silently_reported_as_complete(self):
        pool = menu_service._load_pool()
        dish_ids = [row["id"] for row in pool["dishes"]]
        availability = inventory.check_dishes_availability_batch(
            dish_ids, "shenzhen"
        )
        rotation = get_rotation_context("2026-08-21", "shenzhen")
        context = {
            **rotation,
            "dish_availability": {
                dish_id: value["status"]
                for dish_id, value in availability.items()
            },
        }

        for meal_type in ("lunch", "dinner"):
            with self.subTest(meal=meal_type):
                filler = GapFiller(pool, seed=17)
                _, state, _ = filler.generate_meal(
                    meal_type, context=context, diners_count=4
                )
                if state.protein_count < 2 or state.meat_main_count < 1:
                    relevant = [
                        message for (meal, slot), message
                        in filler.hard_slot_warnings.items()
                        if meal == meal_type
                        and slot in {"protein_main", "meat_main"}
                    ]
                    self.assertTrue(relevant)
                    self.assertTrue(
                        all("必需槽位缺失" in message for message in relevant)
                    )
                self.assertFalse(
                    any(
                        warning.startswith(
                            (f"{meal_type}.protein_main", f"{meal_type}.meat_main")
                        )
                        for warning in filler.degradation_warnings
                    )
                )


if __name__ == "__main__":
    unittest.main()
