import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app
import db
import inventory
import menu_service


class InventoryNameTests(unittest.TestCase):
    def test_required_aliases_share_canonical_ids(self):
        self.assertEqual(inventory.normalize_ingredient_id("山药"), "yam")
        self.assertEqual(inventory.normalize_ingredient_id("淮山"), "yam")
        self.assertEqual(inventory.normalize_ingredient_id("mushroom_generic"), "mushroom")
        self.assertEqual(inventory.normalize_ingredient_id("蘑菇"), "mushroom")

    def test_name_resolution_prefers_canonical_alias_row(self):
        rows = [
            {"ingredient_id": "淮山", "name_cn": "淮山", "name_en": "Chinese Yam", "aliases": "[]"},
            {"ingredient_id": "yam", "name_cn": "山药", "name_en": "Chinese Yam", "aliases": '["淮山"]'},
            {"ingredient_id": "mushroom_generic", "name_cn": "蘑菇", "name_en": "Mushroom", "aliases": "[]"},
            {"ingredient_id": "mushroom", "name_cn": "菌菇", "name_en": "Mushroom", "aliases": '["蘑菇"]'},
        ]
        for raw_name in ("山药", "淮山"):
            row, _, _ = app.resolve_ingredient_name(raw_name, rows)
            self.assertEqual(row["ingredient_id"], "yam")
        for raw_name in ("蘑菇", "Mushroom"):
            row, _, _ = app.resolve_ingredient_name(raw_name, rows)
            self.assertEqual(row["ingredient_id"], "mushroom")

    def test_known_typo_maps_to_existing_standard_name(self):
        rows = [
            {"ingredient_id": "lettuce_stem", "name_cn": "莴笋", "name_en": "Celtuce", "aliases": "[]"},
            {"ingredient_id": "broccoli", "name_cn": "西兰花", "name_en": "Broccoli", "aliases": "[]"},
        ]
        row, name, corrected_from = app.resolve_ingredient_name("窝笋", rows)
        self.assertEqual(row["ingredient_id"], "lettuce_stem")
        self.assertEqual(name, "莴笋")
        self.assertEqual(corrected_from, "窝笋")

    def test_uncertain_short_name_is_not_forced(self):
        rows = [
            {"ingredient_id": "chicken", "name_cn": "鸡肉", "name_en": "Chicken", "aliases": "[]"},
            {"ingredient_id": "egg", "name_cn": "鸡蛋", "name_en": "Egg", "aliases": "[]"},
        ]
        row, name, corrected_from = app.resolve_ingredient_name("鸡", rows)
        self.assertIsNone(row)
        self.assertEqual(name, "鸡")
        self.assertIsNone(corrected_from)


class DatabaseFeatureTests(unittest.TestCase):
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

    def _prepare_switch_inventory(self, conn):
        conn.execute(
            "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES ('stocked','现有食材','Stocked')"
        )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) "
            "VALUES ('shenzhen','stocked','available',1)"
        )

    def _insert_switch_dish(self, conn, dish_id, category_id, roles, *, meal="breakfast",
                            carb_type=None, vegetables=(), quick_soup=0, slow_soup=0):
        conn.execute(
            "INSERT OR IGNORE INTO categories (id,label_cn,label_en) VALUES (?,?,?)",
            (category_id, category_id, category_id),
        )
        conn.execute(
            "INSERT INTO dishes "
            "(id,name_cn,name_en,category_id,meal_tags,meal_roles,protein_types,vegetables,"
            "carb_type,quick_soup,slow_soup,is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                dish_id, dish_id, dish_id, category_id, json.dumps([meal]), json.dumps(roles),
                "[]", json.dumps(list(vegetables)), carb_type, quick_soup, slow_soup,
            ),
        )
        conn.execute(
            "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?, 'stocked', 1)",
            (dish_id,),
        )

    def _insert_switch_menu(self, conn, menu_id, item_id, dish_id, meal="breakfast"):
        conn.execute(
            "INSERT INTO menus (id,date,location,status) VALUES (?,?,'shenzhen','draft')",
            (menu_id, f"2099-01-{menu_id:02d}"),
        )
        conn.execute(
            "INSERT INTO menu_items (id,menu_id,dish_id,meal_type,sort_order) VALUES (?,?,?,?,1)",
            (item_id, menu_id, dish_id, meal),
        )

    def _cycle_ids(self, menu_id, item_id, count):
        chosen_ids = []
        for _ in range(count):
            chosen = app.get_next_available_same_class_dish(menu_id, item_id, "shenzhen")
            self.assertIsNotNone(chosen)
            chosen_ids.append(chosen["id"])
            conn = db.get_db()
            conn.execute("UPDATE menu_items SET dish_id=? WHERE id=?", (chosen["id"], item_id))
            conn.commit()
            conn.close()
        return chosen_ids

    def test_availability_requires_complete_required_ingredient_data(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO dishes (id,name_cn,name_en,meal_tags,is_active) VALUES ('dish_unknown','资料不完整','Incomplete','[\"lunch\"]',1)"
        )
        conn.commit()
        conn.close()

        result = inventory.check_dish_availability("dish_unknown", "shenzhen")
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["data_complete"])
        self.assertFalse(app.get_dish_availability(["dish_unknown"], "shenzhen")["dish_unknown"]["available"])

    def test_availability_distinguishes_available_almost_and_missing(self):
        conn = db.get_db()
        for ingredient_id in ("stocked", "oyster", "sea_urchin"):
            conn.execute(
                "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, ingredient_id, ingredient_id),
            )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) "
            "VALUES ('shenzhen','stocked','available',1)"
        )
        for dish_id, required_ids in (
            ("dish_available", ("stocked",)),
            ("dish_almost", ("stocked", "oyster")),
            ("dish_missing", ("oyster", "sea_urchin")),
        ):
            conn.execute(
                "INSERT INTO dishes (id,name_cn,name_en,meal_tags,is_active) VALUES (?,?,?,'[\"lunch\"]',1)",
                (dish_id, dish_id, dish_id),
            )
            for ingredient_id in required_ids:
                conn.execute(
                    "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1)",
                    (dish_id, ingredient_id),
                )
        conn.commit()
        conn.close()

        result = inventory.check_dishes_availability_batch(
            ["dish_available", "dish_almost", "dish_missing"], "shenzhen"
        )
        self.assertEqual(result["dish_available"]["status"], "available")
        self.assertEqual(result["dish_almost"]["status"], "almost_available")
        self.assertEqual(result["dish_missing"]["status"], "missing")

    def test_rice_is_the_only_pantry_exempt_required_ingredient(self):
        conn = db.get_db()
        for ingredient_id in ("rice", "oyster", "salt"):
            conn.execute(
                "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, ingredient_id, ingredient_id),
            )
        for dish_id, required_ids in (
            ("dish_plain_rice", ("rice",)),
            ("dish_rice_oyster", ("rice", "oyster")),
            ("dish_salt", ("salt",)),
        ):
            conn.execute(
                "INSERT INTO dishes (id,name_cn,name_en,meal_tags,is_active) VALUES (?,?,?,'[\"lunch\"]',1)",
                (dish_id, dish_id, dish_id),
            )
            for ingredient_id in required_ids:
                conn.execute(
                    "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1)",
                    (dish_id, ingredient_id),
                )
        conn.commit()
        pantry_count = conn.execute(
            "SELECT COUNT(*) AS count FROM current_pantry WHERE location='shenzhen' AND is_active=1"
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(pantry_count, 0)

        result = inventory.check_dishes_availability_batch(
            ["dish_plain_rice", "dish_rice_oyster", "dish_salt"], "shenzhen"
        )
        self.assertEqual(result["dish_plain_rice"]["status"], "available")
        self.assertEqual(
            [item["ingredient_id"] for item in result["dish_plain_rice"]["available_required"]],
            ["rice"],
        )
        self.assertEqual(result["dish_rice_oyster"]["status"], "almost_available")
        self.assertEqual(
            [item["ingredient_id"] for item in result["dish_rice_oyster"]["missing_required"]],
            ["oyster"],
        )
        self.assertEqual(result["dish_salt"]["status"], "missing")

    def test_required_alias_rows_are_deduplicated_for_availability(self):
        conn = db.get_db()
        for ingredient_id, name_cn in (
            ("yam", "山药"),
            ("淮山", "淮山"),
            ("mushroom", "菌菇"),
            ("mushroom_generic", "蘑菇"),
        ):
            conn.execute(
                "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, name_cn, name_cn),
            )
        for dish_id, ingredient_ids in (
            ("dish_yam", ("yam", "淮山")),
            ("dish_mushroom", ("mushroom", "mushroom_generic")),
        ):
            conn.execute(
                "INSERT INTO dishes (id,name_cn,name_en,meal_tags,is_active) "
                "VALUES (?,?,?,'[\"lunch\"]',1)",
                (dish_id, dish_id, dish_id),
            )
            for ingredient_id in ingredient_ids:
                conn.execute(
                    "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1)",
                    (dish_id, ingredient_id),
                )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) "
            "VALUES ('shenzhen','淮山','available',1)"
        )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) "
            "VALUES ('shenzhen','mushroom_generic','available',1)"
        )
        conn.commit()
        conn.close()

        result = inventory.check_dishes_availability_batch(
            ["dish_yam", "dish_mushroom"], "shenzhen"
        )
        for dish_id in ("dish_yam", "dish_mushroom"):
            self.assertEqual(result[dish_id]["status"], "available")
            self.assertEqual(len(result[dish_id]["required"]), 1)
            self.assertEqual(len(result[dish_id]["available_required"]), 1)
            self.assertEqual(result[dish_id]["missing_required"], [])

    def test_ai_fill_adds_only_inventory_available_lunch_roles(self):
        conn = db.get_db()
        conn.execute("INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES ('stocked','现有食材','Stocked')")
        for category in ("protein_main", "vegetable_mushroom", "staple_carb", "soup"):
            conn.execute(
                "INSERT INTO categories (id,label_cn,label_en) VALUES (?,?,?)",
                (category, category, category),
            )
        dishes = (
            ("dish_protein", "库存蛋白", "Stocked protein", "protein_main", "[\"protein_main\"]", "[\"chicken\"]", "[]", None, 0),
            ("dish_vegetable", "库存蔬菜", "Stocked vegetable", "vegetable_mushroom", "[\"vegetable_dish\"]", "[]", "[\"broccoli\"]", None, 0),
            ("dish_staple", "库存主食", "Stocked staple", "staple_carb", "[\"staple\"]", "[]", "[]", "rice", 0),
            ("dish_soup", "库存快汤", "Stocked quick soup", "soup", "[\"quick_soup\"]", "[]", "[]", None, 1),
        )
        for dish_id, name_cn, name_en, category, roles, proteins, vegetables, carb_type, quick_soup in dishes:
            conn.execute(
                "INSERT INTO dishes (id,name_cn,name_en,category_id,meal_tags,meal_roles,protein_types,vegetables,carb_type,quick_soup,is_active) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                (dish_id, name_cn, name_en, category, json.dumps(["lunch"]), roles, proteins, vegetables, carb_type, quick_soup),
            )
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?, 'stocked', 1)",
                (dish_id,),
            )
        conn.execute(
            "INSERT INTO dishes (id,name_cn,name_en,category_id,meal_tags,meal_roles,is_active) "
            "VALUES ('dish_unknown','未知主菜','Unknown protein','protein_main','[\"lunch\"]','[\"protein_main\"]',1)"
        )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) VALUES ('shenzhen','stocked','available',1)"
        )
        conn.execute(
            "INSERT INTO menus (id,date,location,status,diners) VALUES (1,'2099-01-02','shenzhen','draft','[\"vv\",\"bb\"]')"
        )
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        ok, _, review = menu_service.ai_fill_menu(1, "shenzhen", seed=7, meal_type="lunch")
        self.assertTrue(ok)
        self.assertEqual(set(review["added"]), {d[0] for d in dishes})
        self.assertNotIn("dish_unknown", review["added"])

    def test_ai_fill_returns_reason_when_protein_has_no_available_candidate(self):
        conn = db.get_db()
        conn.execute("INSERT INTO categories (id,label_cn,label_en) VALUES ('protein_main','protein','protein')")
        conn.execute("INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES ('oyster','生蚝','Oyster')")
        conn.execute(
            "INSERT INTO dishes (id,name_cn,name_en,category_id,meal_tags,meal_roles,protein_types,is_active) "
            "VALUES ('dish_oyster','生蚝','Oyster','protein_main','[\"lunch\"]','[\"protein_main\"]','[\"seafood\"]',1)"
        )
        conn.execute(
            "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES ('dish_oyster','oyster',1)"
        )
        conn.execute(
            "INSERT INTO menus (id,date,location,status,diners) VALUES (1,'2099-01-02','shenzhen','draft','[\"vv\",\"bb\"]')"
        )
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        ok, _, review = menu_service.ai_fill_menu(1, "shenzhen", seed=7, meal_type="lunch")
        self.assertTrue(ok)
        self.assertNotIn("dish_oyster", review["added"])
        protein_unmet = [u for u in review["unmet_slots"] if u["slot"] == "protein_main"]
        self.assertEqual(protein_unmet[0]["reason"], "no_available_candidate")
        self.assertTrue(protein_unmet[0]["message"])

    def test_quantity_defaults_and_low_round_trip(self):
        conn = db.get_db()
        conn.execute("INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES ('broccoli','西兰花','Broccoli')")
        conn.commit()
        conn.close()

        inventory.add_ingredient_to_pantry("shenzhen", "broccoli", quantity_level="low")
        item = inventory.get_current_pantry("shenzhen")["items"][0]
        self.assertEqual(item["quantity_level"], "low")

        inventory.add_ingredient_to_pantry("shenzhen", "broccoli")
        item = inventory.get_current_pantry("shenzhen")["items"][0]
        self.assertEqual(item["quantity_level"], "enough")

    def test_direct_switch_keeps_tofu_and_egg_slots_separate(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        for dish_id, role in (
            ("dish_tofu_a", "tofu_dish"), ("dish_tofu_b", "tofu_dish"),
            ("dish_tofu_missing", "tofu_dish"),
            ("dish_egg_a", "egg_dish"), ("dish_egg_b", "egg_dish"),
        ):
            self._insert_switch_dish(conn, dish_id, "egg_tofu", [role])
        conn.execute(
            "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES ('missing','缺货','Missing')"
        )
        conn.execute(
            "DELETE FROM dish_ingredients WHERE dish_id='dish_tofu_missing' AND ingredient_id='stocked'"
        )
        conn.execute(
            "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) "
            "VALUES ('dish_tofu_missing','missing',1)"
        )
        self._insert_switch_menu(conn, 1, 1, "dish_tofu_a")
        self._insert_switch_menu(conn, 2, 2, "dish_egg_a")
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        self.assertEqual(set(self._cycle_ids(1, 1, 6)), {"dish_tofu_a", "dish_tofu_b"})
        self.assertEqual(set(self._cycle_ids(2, 2, 6)), {"dish_egg_a", "dish_egg_b"})

    def test_direct_switch_keeps_coarse_grain_slot(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        for dish_id, carb_type in (
            ("dish_coarse_a", "coarse_grain"), ("dish_coarse_b", "coarse_grain"),
            ("dish_porridge", "porridge"), ("dish_white_rice", "rice"),
        ):
            self._insert_switch_dish(
                conn, dish_id, "staple_carb", ["staple"], carb_type=carb_type
            )
        self._insert_switch_menu(conn, 1, 1, "dish_coarse_a")
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        self.assertEqual(set(self._cycle_ids(1, 1, 6)), {"dish_coarse_a", "dish_coarse_b"})

    def test_direct_switch_vegetable_slot_crosses_cold_and_hot_categories(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        self._insert_switch_dish(
            conn, "dish_salad", "cold_dish", ["vegetable_dish"], meal="dinner",
            vegetables=("生菜",)
        )
        self._insert_switch_dish(
            conn, "dish_stir_fry", "vegetable_mushroom", ["vegetable_dish"], meal="dinner",
            vegetables=("西兰花",)
        )
        self._insert_switch_menu(conn, 1, 1, "dish_salad", "dinner")
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        self.assertEqual(app.get_next_available_same_class_dish(1, 1, "shenzhen")["id"], "dish_stir_fry")

    def test_direct_switch_keeps_quick_and_slow_soup_slots_separate(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        for dish_id, role, quick, slow, meal in (
            ("dish_quick_a", "quick_soup", 1, 0, "lunch"),
            ("dish_quick_b", "quick_soup", 1, 0, "lunch"),
            ("dish_slow_in_lunch", "slow_soup", 0, 1, "lunch"),
            ("dish_slow_a", "slow_soup", 0, 1, "dinner"),
            ("dish_slow_b", "slow_soup", 0, 1, "dinner"),
            ("dish_quick_in_dinner", "quick_soup", 1, 0, "dinner"),
        ):
            self._insert_switch_dish(
                conn, dish_id, "soup", [role], meal=meal,
                quick_soup=quick, slow_soup=slow,
            )
        self._insert_switch_menu(conn, 1, 1, "dish_quick_a", "lunch")
        self._insert_switch_menu(conn, 2, 2, "dish_slow_a", "dinner")
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        self.assertEqual(set(self._cycle_ids(1, 1, 5)), {"dish_quick_a", "dish_quick_b"})
        self.assertEqual(set(self._cycle_ids(2, 2, 5)), {"dish_slow_a", "dish_slow_b"})

    def test_cycle_replacement_continues_past_eight_clicks(self):
        conn = db.get_db()
        conn.execute("INSERT INTO categories (id,label_cn,label_en) VALUES ('vegetable','蔬菜','Vegetable')")
        conn.execute("INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES ('greens','青菜','Greens')")
        for index, name in enumerate(("炒菜甲", "炒菜乙", "炒菜丙"), 1):
            conn.execute(
                "INSERT INTO dishes (id,name_cn,name_en,category_id,meal_tags,protein_types,is_active) "
                "VALUES (?,?,?,?,?,?,1)",
                (f"dish_{index}", name, name, "vegetable", json.dumps(["lunch"]), "[]"),
            )
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?, 'greens', 1)",
                (f"dish_{index}",),
            )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) VALUES ('shenzhen','greens','available',1)"
        )
        conn.execute("INSERT INTO menus (id,date,location,status) VALUES (1,'2099-01-01','shenzhen','draft')")
        conn.execute("INSERT INTO menu_items (id,menu_id,dish_id,meal_type,sort_order) VALUES (1,1,'dish_1','lunch',1)")
        conn.commit()
        conn.close()

        seen = []
        for _ in range(10):
            chosen = app.get_next_available_same_class_dish(1, 1, "shenzhen")
            self.assertIsNotNone(chosen)
            seen.append(chosen["id"])
            conn = db.get_db()
            conn.execute("UPDATE menu_items SET dish_id=? WHERE id=1", (chosen["id"],))
            conn.commit()
            conn.close()
        self.assertGreaterEqual(len(set(seen)), 3)
        self.assertEqual(seen[0], seen[3])

    def test_cycle_replaces_unavailable_current_with_only_available_alternative(self):
        conn = db.get_db()
        conn.execute("INSERT INTO categories (id,label_cn,label_en) VALUES ('vegetable','蔬菜','Vegetable')")
        for ingredient_id in ("stocked", "missing"):
            conn.execute(
                "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, ingredient_id, ingredient_id),
            )
        for dish_id, ingredient_id in (("dish_current", "missing"), ("dish_alternative", "stocked")):
            conn.execute(
                "INSERT INTO dishes (id,name_cn,name_en,category_id,meal_tags,protein_types,is_active) "
                "VALUES (?,?,?,'vegetable','[\"lunch\"]','[]',1)",
                (dish_id, dish_id, dish_id),
            )
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1)",
                (dish_id, ingredient_id),
            )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) "
            "VALUES ('shenzhen','stocked','available',1)"
        )
        conn.execute("INSERT INTO menus (id,date,location,status) VALUES (1,'2099-01-01','shenzhen','draft')")
        conn.execute(
            "INSERT INTO menu_items (id,menu_id,dish_id,meal_type,sort_order) "
            "VALUES (1,1,'dish_current','lunch',1)"
        )
        conn.commit()
        conn.close()

        chosen = app.get_next_available_same_class_dish(1, 1, "shenzhen")
        self.assertEqual(chosen["id"], "dish_alternative")


if __name__ == "__main__":
    unittest.main()
