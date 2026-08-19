#!/usr/bin/env python3
"""T-003H locked §2 breakfast structure and tofu evidence tests."""

import copy
import unittest

from menu_rule_auditor import audit_final_menu
from rule_engine import (
    GapFiller,
    MealState,
    analyze_meal_slots,
    has_egg_ingredient,
    has_tofu_ingredient,
    is_breakfast_meat_candidate,
    is_breakfast_tofu_candidate,
)
from test_t002_blackbox import complete_pool, dish
from test_t003a_final_auditor import valid_record


class BreakfastLockedStructureGenerationTests(unittest.TestCase):
    def test_generated_breakfast_has_eight_distinct_locked_slots(self):
        result, _ = GapFiller(complete_pool(), seed=9).generate_day(
            context={"historical_last_used": {}, "hard_locked_dish_ids": set()},
            diners_count=4,
        )

        breakfast = result["breakfast"]
        slots = analyze_meal_slots("breakfast", breakfast["state"])

        self.assertEqual(len(breakfast["dishes"]), 8)
        self.assertTrue(all(value["current"] == value["target_min"] for value in slots.values()))
        self.assertEqual(slots["breakfast_meat"]["current"], 1)

    def test_meat_porridge_tangjiao_and_meat_soup_do_not_fill_independent_meat(self):
        rows = [
            dish("meat_porridge", [], ["breakfast"], proteins=["chicken"], carb_type="porridge"),
            dish("tangjiao", ["one_pot_meal", "staple"], ["breakfast"], proteins=["pork"], companion="bao", name="汤饺"),
            dish("meat_soup", ["quick_soup"], ["breakfast"], proteins=["pork"], category="soup"),
        ]
        state = MealState()
        filler = GapFiller({"dishes": rows})
        for row in rows:
            state.add_dish(filler.analyzed[row["id"]])

        self.assertEqual(analyze_meal_slots("breakfast", state)["breakfast_meat"]["current"], 0)
        self.assertTrue(all(not is_breakfast_meat_candidate(filler.analyzed[row["id"]]) for row in rows))

    def test_breakfast_tofu_requires_tag_cold_method_role_and_tofu_evidence(self):
        valid = dish("valid", ["tofu_dish"], ["breakfast"], proteins=["tofu"])
        cases = {
            "no_breakfast_tag": {**valid, "meal_tags": ["lunch"]},
            "not_cold": {**valid, "cooking_methods": ["steam"]},
            "no_role": {**valid, "meal_roles": []},
            "no_tofu_evidence": {**valid, "ingredient_ids": ["鸡蛋"]},
        }

        self.assertTrue(is_breakfast_tofu_candidate(valid))
        for name, value in cases.items():
            with self.subTest(name=name):
                self.assertFalse(is_breakfast_tofu_candidate(value))


class TofuIngredientEvidenceTests(unittest.TestCase):
    def test_roles_use_ingredient_evidence_not_dish_name(self):
        self.assertTrue(has_tofu_ingredient({"ingredient_ids": ["silken_tofu"]}))
        self.assertTrue(has_egg_ingredient({"ingredient_ids": ["鸡蛋"]}))
        self.assertFalse(has_tofu_ingredient({"name_cn": "豆腐假名", "ingredient_ids": ["牛肉"]}))

    def test_dual_role_requires_both_egg_and_tofu_ingredients(self):
        record = valid_record()
        egg = next(
            item for item in record["menu"]["meals"]["breakfast"]
            if "egg_dish" in item["meal_roles"]
        )
        egg["meal_roles"] = ["egg_dish", "tofu_dish"]
        egg["ingredient_ids"] = ["鸡蛋"]

        audit = audit_final_menu(record["menu"], record["evidence"])

        self.assertIn("A01", audit["violation_ids"])


class BreakfastA01ReverseTests(unittest.TestCase):
    def setUp(self):
        self.record = valid_record()

    def _rejects(self, record):
        audit = audit_final_menu(record["menu"], record["evidence"])
        self.assertEqual(audit["status"], "VIOLATION")
        self.assertIn("A01", audit["violation_ids"])

    def test_egg_roll_cannot_fill_tofu_slot(self):
        record = copy.deepcopy(self.record)
        breakfast = record["menu"]["meals"]["breakfast"]
        tofu = next(item for item in breakfast if "tofu_dish" in item["meal_roles"])
        egg = next(item for item in breakfast if "egg_dish" in item["meal_roles"])
        breakfast.remove(tofu)
        egg["meal_roles"] = ["egg_dish", "tofu_dish"]
        egg["ingredient_ids"] = ["鸡蛋"]
        self._rejects(record)

    def test_missing_independent_tofu_is_rejected(self):
        record = copy.deepcopy(self.record)
        breakfast = record["menu"]["meals"]["breakfast"]
        breakfast[:] = [item for item in breakfast if "tofu_dish" not in item["meal_roles"]]
        self._rejects(record)

    def test_missing_independent_meat_is_rejected(self):
        record = copy.deepcopy(self.record)
        breakfast = record["menu"]["meals"]["breakfast"]
        breakfast[:] = [item for item in breakfast if not is_breakfast_meat_candidate(item)]
        self._rejects(record)

    def test_meat_porridge_and_tangjiao_cannot_replace_independent_meat(self):
        record = copy.deepcopy(self.record)
        breakfast = record["menu"]["meals"]["breakfast"]
        breakfast[:] = [item for item in breakfast if not is_breakfast_meat_candidate(item)]
        porridge = next(item for item in breakfast if item.get("carb_type") == "porridge")
        companion = next(item for item in breakfast if item.get("breakfast_staple_type"))
        porridge["protein_types"] = porridge["proteins"] = ["chicken"]
        companion["protein_types"] = companion["proteins"] = ["pork"]
        self._rejects(record)

    def test_non_cold_or_non_breakfast_tofu_cannot_fill_slot(self):
        for mutation in (
            {"cooking_methods": ["steam"]},
            {"meal_tags": ["lunch", "dinner"]},
        ):
            with self.subTest(mutation=mutation):
                record = copy.deepcopy(self.record)
                tofu = next(
                    item for item in record["menu"]["meals"]["breakfast"]
                    if "tofu_dish" in item["meal_roles"]
                )
                tofu.update(mutation)
                self._rejects(record)


if __name__ == "__main__":
    unittest.main()
