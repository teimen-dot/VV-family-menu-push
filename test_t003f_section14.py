#!/usr/bin/env python3
"""T-003F §14 all-day primary vegetable/protein hard constraints."""

import unittest

import rule_engine
from rule_engine import GapFiller, MealState, choose_rotation_candidate
from test_t002_blackbox import complete_pool, dish


class Section14GenerationTests(unittest.TestCase):
    def test_low_pool_degradation_never_relaxes_primary_vegetable_limit(self):
        broccoli = dish(
            "broccoli", ["vegetable_dish"], ["lunch"],
            category="vegetable_mushroom",
        )
        broccoli["vegetables"] = ["西兰花"]
        filler = GapFiller({"dishes": [broccoli]}, seed=1)

        candidates, warning = filler.get_slot_candidates(
            "lunch",
            "vegetable_dish",
            MealState(),
            context={"day_primary_vegetables": {"西兰花"}},
        )

        self.assertEqual(candidates, [])
        self.assertIsNone(warning)
        self.assertEqual(filler.degradation_warnings, [])
        self.assertIn(("lunch", "vegetable_dish"), filler.hard_slot_warnings)

    def test_prior_meal_primary_protein_is_filtered_but_other_source_remains(self):
        chicken = dish(
            "chicken", ["protein_main"], ["dinner"], proteins=["chicken"]
        )
        beef = dish(
            "beef", ["protein_main"], ["dinner"], proteins=["beef"]
        )
        filler = GapFiller({"dishes": [chicken, beef]}, seed=1)

        candidates, _ = filler.get_slot_candidates(
            "dinner",
            "protein_main",
            MealState(),
            context={"day_primary_proteins": {"chicken"}},
        )

        self.assertEqual([item["id"] for item in candidates], ["beef"])

    def test_same_meal_primary_protein_is_filtered(self):
        first = dish(
            "chicken_one", ["protein_main"], ["dinner"], proteins=["chicken"]
        )
        second = dish(
            "chicken_two", ["protein_main"], ["dinner"], proteins=["chicken"]
        )
        beef = dish(
            "beef", ["protein_main"], ["dinner"], proteins=["beef"]
        )
        filler = GapFiller({"dishes": [first, second, beef]}, seed=1)
        state = MealState()
        state.add_dish(filler.analyzed["chicken_one"])

        candidates, _ = filler.get_slot_candidates(
            "dinner", "protein_main", state, context={}
        )

        self.assertEqual([item["id"] for item in candidates], ["beef"])

    def test_placeholder_is_ignored_but_resolved_vegetable_is_counted(self):
        unresolved = {"vegetables": ["any_available_vegetable"]}
        resolved = {
            "vegetables": ["any_available_vegetable"],
            "resolved_vegetable": "西兰花",
        }

        self.assertIsNone(rule_engine.primary_vegetable_subject(unresolved))
        self.assertEqual(
            rule_engine.primary_vegetable_subject(resolved), "西兰花"
        )

    def test_regular_vegetable_dish_without_subject_data_is_not_auto_selected(self):
        unknown = dish(
            "unknown_vegetable", ["vegetable_dish"], ["lunch"],
            category="vegetable_mushroom",
        )
        unknown["vegetables"] = []
        filler = GapFiller({"dishes": [unknown]}, seed=1)

        candidates, warning = filler.get_slot_candidates(
            "lunch", "vegetable_dish", MealState(), context={}
        )

        self.assertEqual(candidates, [])
        self.assertIsNone(warning)
        trace = filler._last_candidate_filter_trace["candidates"][0]
        self.assertEqual(
            trace["reasons"], ["section14_missing_primary_vegetable_data"]
        )

    def test_egg_and_tofu_are_exempt_primary_proteins(self):
        self.assertIsNone(
            rule_engine.primary_protein_source({"proteins": ["egg"]})
        )
        self.assertIsNone(
            rule_engine.primary_protein_source({"proteins": ["tofu"]})
        )

    def test_full_day_generation_has_unique_primary_subjects(self):
        result, _ = GapFiller(complete_pool(), seed=12).generate_day(
            context={"historical_last_used": {}, "hard_locked_dish_ids": set()},
            diners_count=4,
        )
        dishes = [
            item
            for meal in ("breakfast", "lunch", "dinner")
            for item in result[meal]["dishes"]
        ]
        vegetables = [
            value for item in dishes
            if (value := rule_engine.primary_vegetable_subject(item))
        ]
        proteins = [
            value for item in dishes
            if (value := rule_engine.primary_protein_source(item))
        ]

        self.assertEqual(len(vegetables), len(set(vegetables)))
        self.assertEqual(len(proteins), len(set(proteins)))

    def test_breakfast_ranking_preserves_non_exempt_source_when_neutral_option_exists(self):
        chicken = dish(
            "chicken_porridge", ["staple"], ["breakfast"],
            proteins=["chicken"], category="staple_carb", carb_type="porridge",
        )
        neutral = dish(
            "neutral_porridge", ["staple"], ["breakfast"],
            category="staple_carb", carb_type="porridge",
        )
        filler = GapFiller({"dishes": [chicken, neutral]}, seed=1)

        chosen, ranking = choose_rotation_candidate(
            [filler.analyzed["chicken_porridge"], filler.analyzed["neutral_porridge"]],
            filler.scorer, MealState(), "breakfast", {}, return_trace=True,
        )

        self.assertEqual(chosen["id"], "neutral_porridge")
        self.assertIsNone(ranking[0]["section14_future_reservation"])
        self.assertIn("feasibility penalty", ranking[1]["section14_future_reservation"])


if __name__ == "__main__":
    unittest.main()
