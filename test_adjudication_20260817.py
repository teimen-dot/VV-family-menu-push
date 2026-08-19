import json
import os
import random
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import db
import inventory
import menu_service
from apply_adjudication_20260817 import (
    apply_adjudication, ensure_schema, PROTECTED_PENDING_IDS,
    BEEF_BRAISED_RICE_ID, BRAISED_RICE_SOURCE_ID, BRAISED_RICE_SOURCE_NAME,
    FISH_BRAISED_RICE_NAME, MUSHROOM_SOUPS,
)
from rule_engine import (
    GapFiller,
    MealState,
    NO_CANDIDATE_MESSAGE,
    NutritionAnalyzer,
    ScoringEngine,
    analyze_meal_slots,
    choose_rotation_candidate,
    filter_candidates_for_slot,
    get_rotation_context,
    is_auto_candidate,
)


def dish(dish_id, name, roles=(), proteins=(), meals=("lunch",), **extra):
    value = {
        "id": dish_id,
        "name_cn": name,
        "name_en": name,
        "category_id": extra.pop("category_id", "cold_dish"),
        "meal_tags": list(meals),
        "meal_roles": list(roles),
        "protein_types": list(proteins),
        "vegetables": list(extra.pop("vegetables", ())),
        "cooking_methods": [],
        "custom_tags": [],
        "carb_type": extra.pop("carb_type", None),
        "breakfast_staple_type": extra.pop("breakfast_staple_type", None),
        "taste": "normal",
        "banquet": False,
    }
    value.update(extra)
    return value


class FrozenRuleTests(unittest.TestCase):
    def test_auto_pool_filter_and_tangjiao_exception(self):
        tang = NutritionAnalyzer.analyze(dish(
            "tang", "汤饺", ("one_pot_meal", "staple"), ("pork",),
            meals=("breakfast", "lunch"), breakfast_staple_type="bao",
        ))
        self.assertTrue(is_auto_candidate(tang, "breakfast"))
        self.assertFalse(is_auto_candidate(tang, "lunch"))

        onepot = NutritionAnalyzer.analyze(dish(
            "onepot", "焖饭", ("one_pot_meal",), ("beef",), meals=("lunch",)
        ))
        self.assertFalse(is_auto_candidate(onepot, "lunch"))
        for field, value in (
            ("banquet", True), ("drink", "breakfast"),
            ("ingredients_pending", True), ("pending_review", "待裁决"),
        ):
            blocked = dish(f"blocked_{field}", field, ("protein_main",), ("chicken",))
            blocked[field] = value
            self.assertFalse(is_auto_candidate(NutritionAnalyzer.analyze(blocked), "lunch"))

    def test_manual_onepot_covers_meal_but_auto_tangjiao_does_not(self):
        onepot = NutritionAnalyzer.analyze(dish(
            "onepot", "手动焖饭", ("one_pot_meal",), ("beef",), meals=("dinner",)
        ))
        manual = MealState()
        manual.add_dish(onepot, source="owner")
        self.assertTrue(manual.has_manual_one_pot_meal)
        self.assertTrue(all(
            value["missing_min"] == 0
            for value in analyze_meal_slots("dinner", manual, 4).values()
        ))

        tang = NutritionAnalyzer.analyze(dish(
            "tang", "汤饺", ("one_pot_meal", "staple"), ("pork",),
            meals=("breakfast",), breakfast_staple_type="bao",
        ))
        automatic = MealState()
        automatic.add_dish(tang, source="ai")
        self.assertFalse(automatic.has_manual_one_pot_meal)
        self.assertTrue(any(
            value["missing_min"] > 0
            for value in analyze_meal_slots("breakfast", automatic, 4).values()
        ))

    def test_tofu_never_satisfies_separate_meat_minimum(self):
        tofu_chicken = NutritionAnalyzer.analyze(dish(
            "tofu", "鸡丝青瓜丝嫩豆腐", ("tofu_dish", "protein_main"),
            ("chicken", "tofu"),
        ))
        state = MealState()
        state.add_dish(tofu_chicken)
        self.assertEqual(state.protein_count, 1)
        self.assertEqual(state.meat_main_count, 0)
        self.assertEqual(filter_candidates_for_slot([tofu_chicken], "meat_main"), [])

        chicken = NutritionAnalyzer.analyze(dish(
            "chicken", "葱香鸡肉", ("protein_main",), ("chicken",)
        ))
        state.add_dish(chicken)
        self.assertEqual(state.meat_main_count, 1)

    def test_cold_category_is_not_implicitly_a_vegetable(self):
        cold = NutritionAnalyzer.analyze(dish(
            "cold", "刺身拼盘", (), ("fish",), vegetables=("青瓜",)
        ))
        salad = NutritionAnalyzer.analyze(dish(
            "salad", "沙拉菜", ("vegetable_dish",), (), vegetables=("生菜",)
        ))
        self.assertEqual(filter_candidates_for_slot([cold], "vegetable_dish"), [])
        self.assertEqual(filter_candidates_for_slot([salad], "vegetable_dish"), [salad])

    def test_rotation_is_never_used_then_oldest(self):
        candidates = [
            NutritionAnalyzer.analyze(dish("old", "旧菜", ("protein_main",), ("beef",))),
            NutritionAnalyzer.analyze(dish("new", "新近菜", ("protein_main",), ("beef",))),
            NutritionAnalyzer.analyze(dish("never", "未吃菜", ("protein_main",), ("beef",))),
        ]
        scorer = ScoringEngine(random.Random(1))
        chosen = choose_rotation_candidate(
            candidates, scorer, MealState(), "lunch",
            {"historical_last_used": {"old": "2026-01-01", "new": "2026-08-01"}},
        )
        self.assertEqual(chosen["id"], "never")
        chosen = choose_rotation_candidate(
            candidates[:2], scorer, MealState(), "lunch",
            {"historical_last_used": {"old": "2026-01-01", "new": "2026-08-01"}},
        )
        self.assertEqual(chosen["id"], "old")


class DatabaseRuleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "test.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()
        inventory._availability_cache.clear()
        menu_service.invalidate_catalog_cache()

    def tearDown(self):
        inventory._availability_cache.clear()
        menu_service.invalidate_catalog_cache()
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_rotation_boundaries_sources_release_and_kitchen_isolation(self):
        conn = db.get_db()
        today = date.today()

        def add_menu(menu_id, day, location, status, dish_id):
            conn.execute(
                "INSERT INTO menus(id,date,location,status) VALUES (?,?,?,?)",
                (menu_id, day.isoformat(), location, status),
            )
            conn.execute(
                "INSERT INTO menu_items(menu_id,dish_id,meal_type,source) "
                "VALUES (?,?,'lunch','ai')", (menu_id, dish_id),
            )

        add_menu(1, today - timedelta(days=1), "shenzhen", "confirmed", "d0")
        add_menu(2, today - timedelta(days=5), "shenzhen", "pushed", "old")
        add_menu(3, today - timedelta(days=2), "shenzhen", "draft", "draft_past")
        add_menu(4, today + timedelta(days=2), "shenzhen", "draft", "future")
        add_menu(5, today, "shenzhen", "draft", "current")
        add_menu(6, today, "hongkong", "draft", "hk_only")
        conn.commit()
        conn.close()

        context = get_rotation_context(today.isoformat(), "shenzhen")
        self.assertEqual(context["historical_last_used"]["d0"], (today - timedelta(days=1)).isoformat())
        self.assertNotIn("future", context["historical_last_used"])
        self.assertNotIn("draft_past", context["historical_last_used"])
        self.assertTrue({"d0", "future", "current"} <= context["hard_locked_dish_ids"])
        self.assertNotIn("hk_only", context["hard_locked_dish_ids"])

        released = get_rotation_context(today.isoformat(), "shenzhen", exclude_menu_id=5)
        self.assertNotIn("current", released["hard_locked_dish_ids"])
        d4 = get_rotation_context((today + timedelta(days=3)).isoformat(), "shenzhen")
        self.assertNotIn("d0", d4["hard_locked_dish_ids"])
        ok, message = menu_service.revert_to_draft(1)
        self.assertFalse(ok)
        self.assertIn("历史", message)

    def test_placeholder_classes_and_exact_household_exemptions_are_strict(self):
        conn = db.get_db()
        for ingredient_id, name in (
            ("any_available_fish", "任意可用鱼"),
            ("any_available_protein", "任意可用蛋白质"),
            ("fish_stock", "鳕鱼"), ("tofu_stock", "豆腐"),
            ("noodle_stock", "面条"), ("面粉", "面粉"), ("oyster", "生蚝"),
        ):
            conn.execute(
                "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, name, name),
            )
        conn.executemany(
            "INSERT INTO ingredient_classifications(ingredient_id,class_id) VALUES (?,?)",
            (("fish_stock", "fish"), ("fish_stock", "protein"),
             ("tofu_stock", "protein"), ("noodle_stock", "noodle")),
        )
        for dish_id, ingredient_id in (
            ("fish_dish", "any_available_fish"),
            ("protein_dish", "any_available_protein"),
            ("noodle_dish", "noodle_stock"),
            ("flour_dish", "面粉"),
        ):
            conn.execute(
                "INSERT INTO dishes(id,name_cn,meal_tags,is_active) VALUES (?,?, '[\"lunch\"]',1)",
                (dish_id, dish_id),
            )
            conn.execute(
                "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES (?,?,1)",
                (dish_id, ingredient_id),
            )
        conn.executemany(
            "INSERT INTO current_pantry(location,ingredient_id,status,is_active) VALUES (?,?,?,1)",
            (("shenzhen", "fish_stock", "available"),
             ("hongkong", "tofu_stock", "available")),
        )
        conn.commit()
        conn.close()

        self.assertEqual(inventory.check_dish_availability("fish_dish", "shenzhen")["status"], "available")
        self.assertEqual(inventory.check_dish_availability("fish_dish", "hongkong")["status"], "missing")
        self.assertEqual(inventory.check_dish_availability("protein_dish", "hongkong")["status"], "available")
        self.assertEqual(
            inventory.check_dish_availability("noodle_dish", "shenzhen")["status"],
            "missing",
        )
        self.assertEqual(
            inventory.check_dish_availability("flour_dish", "shenzhen")["status"],
            "available",
        )

    def test_zero_candidate_leaves_slots_empty_with_exact_message(self):
        conn = db.get_db()
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO categories(id,label_cn,label_en) "
            "VALUES ('protein_main','蛋白质','Protein')"
        )
        conn.execute(
            "INSERT INTO dishes(id,name_cn,name_en,category_id,meal_tags,meal_roles,is_active) "
            "VALUES ('incomplete','资料待完善','Incomplete','protein_main','[\"lunch\"]',"
            "'[\"protein_main\"]',1)"
        )
        conn.execute(
            "INSERT INTO menus(id,date,location,status) VALUES (1,?,'shenzhen','draft')",
            (today,),
        )
        conn.commit()
        conn.close()
        ok, _, review = menu_service.ai_fill_menu(1, "shenzhen", meal_type="lunch")
        self.assertTrue(ok)
        self.assertEqual(review["added"], [])
        self.assertTrue(review["unmet_slots"])
        self.assertTrue(all(
            item["message"] == NO_CANDIDATE_MESSAGE
            for item in review["unmet_slots"]
        ))


class MigrationProtectionTests(unittest.TestCase):
    def _production_backup(self):
        source = os.environ.get(
            "ADJUDICATION_PRODUCTION_BACKUP",
            os.path.join(os.path.dirname(__file__), "family_menu.db"),
        )
        if not os.path.isfile(source):
            self.skipTest("frozen local database not present")
        return source

    def _copy_database(self, source, target):
        source_conn = sqlite3.connect(
            f"file:{source}?mode=ro&immutable=1", uri=True,
        )
        target_conn = sqlite3.connect(target)
        source_conn.backup(target_conn)
        target_conn.close()
        source_conn.close()

    def _pending_snapshot(self, conn):
        values = {}
        for dish_id in PROTECTED_PENDING_IDS:
            row = conn.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()
            req = conn.execute(
                "SELECT ingredient_id,required FROM dish_ingredients "
                "WHERE dish_id=? ORDER BY ingredient_id", (dish_id,),
            ).fetchall()
            values[dish_id] = (
                tuple(row) if row else None,
                tuple((item["ingredient_id"], item["required"]) for item in req),
            )
        return values

    def _adjudicated_snapshot(self, conn):
        dish_ids = (BRAISED_RICE_SOURCE_ID, BEEF_BRAISED_RICE_ID, *MUSHROOM_SOUPS)
        dishes = {
            row["id"]: tuple(row)
            for row in conn.execute(
                "SELECT id,name_cn,name_en,category_id,meal_tags,protein_types,carb_type,"
                "meal_roles,quick_soup,slow_soup,image,image_uploaded,is_active,deleted_at,"
                "ingredients_pending,pending_review FROM dishes WHERE id IN ({}) "
                "ORDER BY id".format(",".join("?" for _ in dish_ids)),
                dish_ids,
            )
        }
        required = {
            dish_id: tuple(
                row["ingredient_id"] for row in conn.execute(
                    "SELECT ingredient_id FROM dish_ingredients "
                    "WHERE dish_id=? AND required=1 ORDER BY ingredient_id", (dish_id,),
                )
            )
            for dish_id in dish_ids
        }
        return dishes, required

    def test_production_shape_strict_dry_run_split_and_idempotency(self):
        source = self._production_backup()
        with tempfile.TemporaryDirectory() as tempdir:
            target = os.path.join(tempdir, "copy.db")
            self._copy_database(source, target)
            conn = sqlite3.connect(target)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=OFF")
            ensure_schema(conn)
            conn.commit()

            before = self._pending_snapshot(conn)

            # Default dry-run semantics: strict migration succeeds, then rolls back.
            conn.execute("BEGIN IMMEDIATE")
            apply_adjudication(conn, strict=True)
            dry_run_state = self._adjudicated_snapshot(conn)
            self.assertIn(BEEF_BRAISED_RICE_ID, dry_run_state[0])
            conn.rollback()
            self.assertEqual(
                conn.execute(
                    "SELECT name_cn FROM dishes WHERE id=?", (BRAISED_RICE_SOURCE_ID,)
                ).fetchone()["name_cn"],
                BRAISED_RICE_SOURCE_NAME,
            )
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM dishes WHERE id=?", (BEEF_BRAISED_RICE_ID,)
            ).fetchone())

            conn.execute("BEGIN IMMEDIATE")
            apply_adjudication(conn, strict=True)
            conn.commit()
            after_first_pending = self._pending_snapshot(conn)
            after_first = self._adjudicated_snapshot(conn)

            fish = after_first[0][BRAISED_RICE_SOURCE_ID]
            beef = after_first[0][BEEF_BRAISED_RICE_ID]
            self.assertEqual(fish[1], FISH_BRAISED_RICE_NAME)
            self.assertEqual(fish[5], '["fish"]')
            self.assertEqual(beef[1:3], ("牛肉焖饭", "Beef Braised Rice"))
            self.assertEqual(beef[5], '["beef"]')
            self.assertIsNone(beef[10])
            self.assertEqual(beef[11], 0)
            self.assertEqual(after_first[1][BRAISED_RICE_SOURCE_ID], ("any_available_fish",))
            self.assertEqual(after_first[1][BEEF_BRAISED_RICE_ID], ("beef",))
            self.assertFalse(conn.execute(
                "SELECT 1 FROM dishes WHERE name_cn=? AND is_active=1",
                (BRAISED_RICE_SOURCE_NAME,),
            ).fetchone())
            for dish_id, accepted_names in MUSHROOM_SOUPS.items():
                self.assertIn(after_first[0][dish_id][1], accepted_names)
                self.assertIn("any_available_mushroom", after_first[1][dish_id])
                self.assertIn("quick_soup", json.loads(after_first[0][dish_id][7]))

            conn.execute("BEGIN IMMEDIATE")
            apply_adjudication(conn, strict=True)
            conn.commit()
            after_second_pending = self._pending_snapshot(conn)
            after_second = self._adjudicated_snapshot(conn)

            self.assertEqual(before, after_first_pending)
            self.assertEqual(before, after_second_pending)
            self.assertEqual(after_first, after_second)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM dishes WHERE id=?", (BEEF_BRAISED_RICE_ID,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            indexes = {
                tuple(row["name"] for row in conn.execute(f"PRAGMA index_info('{idx['name']}')"))
                for idx in conn.execute("PRAGMA index_list(menus)") if idx["unique"]
            }
            self.assertIn(("date", "location"), indexes)
            conn.close()


if __name__ == "__main__":
    unittest.main()
