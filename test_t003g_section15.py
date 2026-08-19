#!/usr/bin/env python3
"""T-003G §15 per-meal leafy-vegetable hard constraints."""

import unittest

import rule_engine
from rule_engine import GapFiller, MealState
from test_t002_blackbox import dish


class Section15CanonicalTests(unittest.TestCase):
    def test_confirmed_leafy_list_and_red_amaranth_alias_are_leafy(self):
        for name in (
            "菜心", "上海青", "小白菜", "菠菜", "空心菜", "生菜",
            "油麦菜", "芥蓝", "苋菜", "红苋菜", "红薯叶", "西兰花",
        ):
            with self.subTest(name=name):
                self.assertTrue(
                    rule_engine.is_leafy_vegetable({"vegetables": [name]})
                )

    def test_confirmed_non_leafy_and_unknown_default_to_non_leafy(self):
        for name in (
            "藕", "冬瓜", "番茄", "菌菇", "萝卜", "茄子", "玉米",
            "芦笋", "莴笋", "西葫芦", "测试新蔬菜",
        ):
            with self.subTest(name=name):
                self.assertFalse(
                    rule_engine.is_leafy_vegetable({"vegetables": [name]})
                )


class Section15GenerationTests(unittest.TestCase):
    @staticmethod
    def _vegetable(dish_id, subject):
        value = dish(
            dish_id, ["vegetable_dish"], ["lunch"],
            category="vegetable_mushroom",
        )
        value["vegetables"] = [subject]
        return value

    def test_second_leafy_dish_is_filtered_but_non_leafy_remains(self):
        first = self._vegetable("first_leaf", "菜心")
        second = self._vegetable("second_leaf", "上海青")
        winter_melon = self._vegetable("winter_melon", "冬瓜")
        filler = GapFiller(
            {"dishes": [first, second, winter_melon]}, seed=1
        )
        state = MealState()
        state.add_dish(filler.analyzed["first_leaf"])

        candidates, _ = filler.get_slot_candidates(
            "lunch", "vegetable_dish", state, context={}
        )

        self.assertEqual([item["id"] for item in candidates], ["winter_melon"])
        excluded = next(
            item for item in filler._last_candidate_filter_trace["candidates"]
            if item["dish_id"] == "second_leaf"
        )
        self.assertEqual(
            excluded["reasons"],
            ["section15_leafy_vegetable_meal_limit:上海青"],
        )

    def test_low_pool_degradation_never_relaxes_leafy_limit(self):
        first = self._vegetable("first_leaf", "菜心")
        second = self._vegetable("second_leaf", "上海青")
        filler = GapFiller({"dishes": [first, second]}, seed=1)
        state = MealState()
        state.add_dish(filler.analyzed["first_leaf"])

        candidates, warning = filler.get_slot_candidates(
            "lunch", "vegetable_dish", state, context={}
        )

        self.assertEqual(candidates, [])
        self.assertIsNone(warning)
        self.assertEqual(filler.degradation_warnings, [])
        self.assertIn(("lunch", "vegetable_dish"), filler.hard_slot_warnings)

    def test_hard_shortage_marks_generation_review_not_passed_for_persistence(self):
        _, logs = GapFiller({"dishes": []}, seed=1).generate_day(
            diners_count=4
        )

        self.assertTrue(logs["review"]["hard_warnings"])
        self.assertFalse(logs["review"]["passed"])


if __name__ == "__main__":
    unittest.main()
