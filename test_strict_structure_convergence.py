import unittest

from rule_engine import (
    assign_meal_structure,
    is_animal_protein_main,
)


def dish(name, *, roles=(), proteins=(), category="protein_main", carb=None,
         vegetables=(), meal_tags=("dinner",), quick=False, slow=False):
    return {
        "id": name,
        "name_cn": name,
        "meal_roles": list(roles),
        "proteins": list(proteins),
        "protein_types": list(proteins),
        "category_id": category,
        "carb_type": carb,
        "vegetables": list(vegetables),
        "meal_tags": list(meal_tags),
        "is_soup": quick or slow,
        "quick_soup": quick,
        "slow_soup": slow,
    }


class StrictStructureConvergenceTests(unittest.TestCase):
    def test_chinese_and_english_meat_are_normalized_but_egg_tofu_are_not(self):
        for protein in ("牛肉", "虾", "鸡肉", "猪肉", "beef", "shrimp", "fish"):
            self.assertTrue(is_animal_protein_main(
                dish(protein, roles=("protein_main",), proteins=(protein,))
            ), protein)
        self.assertFalse(is_animal_protein_main(
            dish("鸡蛋", roles=("protein_main", "egg_dish"), proteins=("鸡蛋",))
        ))
        self.assertFalse(is_animal_protein_main(
            dish("豆腐", roles=("protein_main", "tofu_dish"), proteins=("豆腐",))
        ))

    def test_three_diner_dinner_eight_ai_dishes_converge_to_five(self):
        items = [
            dish("煎鸡翅", roles=("protein_main",), proteins=("鸡肉",)),
            dish("清炒西兰花", roles=("vegetable_dish",), category="vegetable_mushroom",
                 vegetables=("西兰花",)),
            dish("三色藜麦饭", roles=("staple",), category="staple_carb", carb="rice"),
            dish("金蒜牛肉粒", roles=("protein_main",), proteins=("牛肉",)),
            dish("香煎豆腐块", roles=("tofu_dish",), proteins=("豆腐",), category="egg_tofu"),
            dish("榨菜肉丝", roles=("protein_main",), proteins=("猪肉",)),
            dish("莲藕松茸鸡汤", roles=("slow_soup",), category="soup", slow=True),
            dish("温泉蛋", roles=("egg_dish",), proteins=("鸡蛋",), category="egg_tofu"),
        ]
        result = assign_meal_structure("dinner", items, 3)
        self.assertEqual(result["target_dish_count"], 5)
        self.assertEqual(result["structure_dish_count"], 5)
        self.assertEqual(len(result["unmatched_ai_indices"]), 3)
        self.assertEqual(set(result["assignments"].values()), {
            "meat_main", "vegetable_dish", "staple", "slow_soup"
        })

    def test_manual_extra_is_preserved_and_does_not_take_two_slots(self):
        items = [
            dish("手动鸡蛋", roles=("egg_dish",), proteins=("鸡蛋",), category="egg_tofu"),
            dish("牛肉一", roles=("protein_main",), proteins=("牛肉",), vegetables=("芹菜",)),
            dish("鱼二", roles=("protein_main",), proteins=("fish",)),
            dish("青菜", roles=("vegetable_dish",), category="vegetable_mushroom", vegetables=("菜心",)),
            dish("米饭", roles=("staple",), category="staple_carb", carb="rice"),
            dish("汤", roles=("slow_soup",), category="soup", slow=True),
        ]
        result = assign_meal_structure("dinner", items, 3, owner_indices={0})
        self.assertEqual(result["target_dish_count"], 5)
        self.assertEqual(result["manual_extra_count"], 1)
        self.assertEqual(result["extra_indices"], [0])
        self.assertEqual(result["structure_dish_count"], 5)


if __name__ == "__main__":
    unittest.main()
