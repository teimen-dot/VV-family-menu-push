#!/usr/bin/env python3
"""T-003A final-output auditor, rejection samples, and real-data simulation."""

import copy
import os
import unittest

from menu_rule_auditor import AUDIT_RULES, audit_final_menu, audit_menu_sequence
from rule_engine import GapFiller
from run_t003a_real_audit import DEFAULT_REAL_DB, run_real_data_audit
from test_t002_blackbox import complete_pool


def valid_record(date_str="2099-01-01", location="shenzhen", diners=4, seed=41):
    result, logs = GapFiller(complete_pool(), seed=seed).generate_day(
        context={
            "dish_availability": {
                row["id"]: "available" for row in complete_pool()["dishes"]
            },
            "hard_locked_dish_ids": set(),
            "historical_last_used": {},
        },
        diners_count=diners,
    )
    meals = {}
    for meal in ("breakfast", "lunch", "dinner"):
        meals[meal] = []
        for analysis in result[meal]["dishes"]:
            item = copy.deepcopy(analysis)
            item["dish_id"] = item["id"]
            item["source"] = "ai"
            meals[meal].append(item)
    evidence = {
        **logs["review"],
        "rotation_context_location": location,
        "inventory_context_location": location,
    }
    return {
        "menu": {
            "date": date_str,
            "location": location,
            "status": "draft",
            "diners_count": diners,
            "meals": meals,
        },
        "evidence": evidence,
    }


def renamed_day(record, date_str, keep_dish_id):
    value = copy.deepcopy(record)
    value["menu"]["date"] = date_str
    for meal, items in value["menu"]["meals"].items():
        for item in items:
            if item["dish_id"] == keep_dish_id:
                continue
            item["dish_id"] = f"{date_str}-{meal}-{item['dish_id']}"
            item["id"] = item["dish_id"]
    value["evidence"]["degradation_warnings"] = []
    value["evidence"]["degradation_events"] = []
    value["evidence"]["hard_warnings"] = []
    return value


class FinalMenuAuditorContractTests(unittest.TestCase):
    def test_clause_catalog_is_numbered_and_source_tagged(self):
        self.assertEqual([item["id"] for item in AUDIT_RULES], [
            "A01", "A02", "A03", "A04", "A05",
            "A06", "A07", "A08", "A09",
        ])
        self.assertTrue(all(item["clause"].startswith("§") for item in AUDIT_RULES))

    def test_valid_final_output_passes_without_trusting_helper_state(self):
        record = valid_record()
        audit = audit_final_menu(record["menu"], record["evidence"])
        self.assertEqual(audit["status"], "PASS")
        self.assertTrue(audit["passed"])

    def test_explicit_egg_tofu_combo_can_cover_two_breakfast_slots_at_six_dishes(self):
        record = valid_record()
        breakfast = record["menu"]["meals"]["breakfast"]
        egg = next(item for item in breakfast if "egg_dish" in item["meal_roles"])
        tofu = next(item for item in breakfast if "tofu_dish" in item["meal_roles"])
        combo = copy.deepcopy(egg)
        combo["id"] = combo["dish_id"] = "explicit_egg_tofu_combo"
        combo["meal_roles"] = ["egg_dish", "tofu_dish"]
        breakfast[:] = [
            item for item in breakfast if item not in (egg, tofu)
        ] + [combo]

        audit = audit_final_menu(record["menu"], record["evidence"])

        self.assertEqual(len(breakfast), 6)
        self.assertEqual(audit["status"], "PASS")

    def test_ordinary_multi_role_dish_cannot_fill_two_breakfast_slots(self):
        record = valid_record()
        breakfast = record["menu"]["meals"]["breakfast"]
        egg = next(item for item in breakfast if "egg_dish" in item["meal_roles"])
        vegetables = [
            item for item in breakfast if "vegetable_dish" in item["meal_roles"]
        ]
        egg["meal_roles"] = ["egg_dish", "vegetable_dish"]
        breakfast.remove(vegetables[0])

        audit = audit_final_menu(record["menu"], record["evidence"])

        self.assertEqual(audit["status"], "VIOLATION")
        self.assertIn("A01", audit["violation_ids"])

    def test_unassigned_automatic_breakfast_protein_is_rejected(self):
        record = valid_record()
        extra = copy.deepcopy(record["menu"]["meals"]["lunch"][0])
        extra["id"] = extra["dish_id"] = "mechanical_extra_breakfast_protein"
        record["menu"]["meals"]["breakfast"].append(extra)

        audit = audit_final_menu(record["menu"], record["evidence"])

        self.assertEqual(audit["status"], "VIOLATION")
        self.assertIn("A01", audit["violation_ids"])

    def test_soup_with_main_role_is_rejected(self):
        record = valid_record()
        soup = next(
            item for item in record["menu"]["meals"]["lunch"]
            if "quick_soup" in item["meal_roles"]
        )
        soup["meal_roles"].append("protein_main")

        audit = audit_final_menu(record["menu"], record["evidence"])

        self.assertEqual(audit["status"], "VIOLATION")
        self.assertIn("A06", audit["violation_ids"])

    def test_kitchen_context_mismatch_is_rejected(self):
        record = valid_record(location="hongkong")
        record["evidence"]["inventory_context_location"] = "shenzhen"

        audit = audit_final_menu(record["menu"], record["evidence"])

        self.assertEqual(audit["status"], "VIOLATION")
        self.assertIn("A09", audit["violation_ids"])

    def test_healthy_pool_filter_empty_is_explicit_hard_shortage_not_degradation(self):
        record = valid_record()
        dinner = record["menu"]["meals"]["dinner"]
        protein = next(item for item in dinner if "protein_main" in item["meal_roles"])
        dinner.remove(protein)
        warning = (
            "dinner.protein_main 无任何合法候选，"
            "蛋白质/肉类必需槽位缺失"
        )
        record["evidence"]["hard_warnings"] = [warning]
        record["evidence"]["slot_pool_sizes"]["dinner.protein_main"] = 20

        audit = audit_final_menu(record["menu"], record["evidence"])

        self.assertEqual(audit["status"], "HARD_SHORTAGE")
        self.assertTrue(audit["rule_compliant"])
        self.assertFalse(audit["menu_complete"])
        self.assertEqual(record["evidence"]["degradation_warnings"], [])


class ReverseViolationSamples(unittest.TestCase):
    def setUp(self):
        self.base = valid_record()

    def _assert_rejected(self, record, rule_id, previous=None):
        audit = audit_final_menu(
            record["menu"], record["evidence"], previous or []
        )
        self.assertEqual(audit["status"], "VIOLATION")
        self.assertIn(rule_id, audit["violation_ids"])

    def test_01_dinner_with_zero_protein_is_rejected(self):
        record = copy.deepcopy(self.base)
        record["menu"]["meals"]["dinner"] = [
            item for item in record["menu"]["meals"]["dinner"]
            if "protein_main" not in item["meal_roles"]
        ]
        self._assert_rejected(record, "A03")

    def test_02_breakfast_with_multiple_eggs_is_rejected(self):
        record = copy.deepcopy(self.base)
        egg = next(
            item for item in record["menu"]["meals"]["breakfast"]
            if "egg_dish" in item["meal_roles"]
        )
        extra = copy.deepcopy(egg)
        extra["id"] = extra["dish_id"] = "extra_breakfast_egg"
        record["menu"]["meals"]["breakfast"].append(extra)
        self._assert_rejected(record, "A02")

    def test_03_same_meal_duplicate_dish_id_is_rejected(self):
        record = copy.deepcopy(self.base)
        record["menu"]["meals"]["dinner"].append(
            copy.deepcopy(record["menu"]["meals"]["dinner"][0])
        )
        self._assert_rejected(record, "A05")

    def test_04_tofu_cannot_replace_the_meat_minimum(self):
        record = copy.deepcopy(self.base)
        for item in record["menu"]["meals"]["lunch"]:
            if "protein_main" in item["meal_roles"]:
                item["meal_roles"] = ["protein_main", "tofu_dish"]
                item["protein_types"] = ["tofu"]
        self._assert_rejected(record, "A04")

    def test_05_healthy_pool_four_day_lock_break_is_rejected(self):
        first = copy.deepcopy(self.base)
        repeated = next(
            item for item in first["menu"]["meals"]["lunch"]
            if "protein_main" in item["meal_roles"]
        )["dish_id"]
        second = renamed_day(first, "2099-01-02", repeated)
        warning = (
            "lunch.meat_main 该分类菜品不足，建议补录 "
            "(20/20)；允许4天窗口内同菜重复"
        )
        second["evidence"]["degradation_warnings"] = [warning]
        second["evidence"]["degradation_events"] = [{
            "meal": "lunch", "slot": "meat_main", "dish_id": repeated,
            "pool_size": 20, "minimum": 20, "warning": warning,
        }]
        second["evidence"]["slot_pool_sizes"]["lunch.meat_main"] = 20
        self._assert_rejected(second, "A07", [first])

    def test_06_low_pool_degradation_without_warning_is_rejected(self):
        first = copy.deepcopy(self.base)
        repeated = next(
            item for item in first["menu"]["meals"]["lunch"]
            if "protein_main" in item["meal_roles"]
        )["dish_id"]
        second = renamed_day(first, "2099-01-02", repeated)
        warning = (
            "lunch.meat_main 该分类菜品不足，建议补录 "
            "(1/20)；允许4天窗口内同菜重复"
        )
        second["evidence"]["degradation_events"] = [{
            "meal": "lunch", "slot": "meat_main", "dish_id": repeated,
            "pool_size": 1, "minimum": 20, "warning": warning,
        }]
        second["evidence"]["slot_pool_sizes"]["lunch.meat_main"] = 1
        self._assert_rejected(second, "A08", [first])

    def test_all_six_bad_samples_are_green_tests_that_reject_bad_menus(self):
        method_names = [
            name for name in dir(self) if name.startswith("test_0")
        ]
        self.assertEqual(len(method_names), 6)


@unittest.skipUnless(os.path.isfile(DEFAULT_REAL_DB), "real preview DB unavailable")
class RealDataSevenDayAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_real_data_audit(DEFAULT_REAL_DB, "2026-08-21")

    def test_copy_integrity_draft_only_and_menu_189_protection(self):
        self.assertEqual(self.report["source_quick_check"], "ok")
        self.assertEqual(self.report["copy_quick_check_before"], "ok")
        self.assertEqual(self.report["copy_quick_check_after"], "ok")
        self.assertTrue(self.report["source_unchanged"])
        self.assertTrue(self.report["menu_189_unchanged_in_copy"])
        self.assertTrue(self.report["generated_only_draft"])
        self.assertEqual(self.report["push_attempts"], 0)

    def test_both_kitchens_run_seven_days_with_2345_cycle(self):
        self.assertEqual(len(self.report["days"]), 14)
        for location in ("shenzhen", "hongkong"):
            days = [
                item for item in self.report["days"]
                if item["location"] == location
            ]
            self.assertEqual(
                [item["diners_count"] for item in days],
                [2, 3, 4, 5, 2, 3, 4],
            )
            five = next(item for item in days if item["diners_count"] == 5)
            self.assertEqual(five["meals"]["lunch"]["target"]["protein_main"], 2)
            self.assertEqual(five["meals"]["lunch"]["target"]["vegetable_dish"], 2)

    def test_real_outputs_have_no_rule_violation(self):
        self.assertTrue(self.report["all_rule_compliant"])
        self.assertFalse(any(
            item["status"] == "VIOLATION" for item in self.report["days"]
        ))

    def test_report_includes_daily_dish_names_and_hard_gap_diagnostics(self):
        for day in self.report["days"]:
            self.assertEqual(
                set(day["meal_dish_names"]),
                {"breakfast", "lunch", "dinner", "afternoon_snack"},
            )
            for meal in ("breakfast", "lunch", "dinner"):
                self.assertTrue(day["meal_dish_names"][meal])
            for gap in day["hard_gap_diagnostics"]:
                self.assertIn(gap["cause"], {
                    "POOL_BELOW_MINIMUM",
                    "INVENTORY_FILTERED_EMPTY",
                    "FOUR_DAY_LOCK_FILTERED_EMPTY",
                    "SAME_DAY_OR_CAP_FILTERED_EMPTY",
                })
                self.assertIn("pool_size", gap)
                self.assertIn("availability_status_counts", gap)

    def test_rice_pool_is_reported_as_an_independent_data_todo(self):
        todo = self.report["rice_pool_todo"]
        self.assertEqual(todo["minimum"], 8)
        self.assertEqual(todo["count"], 5)
        self.assertLess(todo["count"], todo["minimum"])
        self.assertTrue(todo["below_minimum"])


if __name__ == "__main__":
    unittest.main()
