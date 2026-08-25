import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app
import db
import inventory
import menu_service
from ingredient_resolution import ensure_resolution_schema
from rule_engine import (
    GapFiller, MealState, NutritionAnalyzer, analyze_meal_slots,
    is_breakfast_tofu_candidate, is_breakfast_egg_candidate,
)


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
                            carb_type=None, vegetables=(), proteins=(),
                            quick_soup=0, slow_soup=0, custom_tags=()):
        conn.execute(
            "INSERT OR IGNORE INTO categories (id,label_cn,label_en) VALUES (?,?,?)",
            (category_id, category_id, category_id),
        )
        conn.execute(
            "INSERT INTO dishes "
            "(id,name_cn,name_en,category_id,meal_tags,meal_roles,protein_types,vegetables,"
            "carb_type,quick_soup,slow_soup,custom_tags,is_active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                dish_id, dish_id, dish_id, category_id, json.dumps([meal]), json.dumps(roles),
                json.dumps(list(proteins)), json.dumps(list(vegetables)),
                carb_type, quick_soup, slow_soup, json.dumps(list(custom_tags)),
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

    def test_leafy_placeholder_accepts_only_controlled_leafy_inventory(self):
        conn = db.get_db()
        for ingredient_id, name_cn in (
            ("any_available_leafy_vegetable", "任意可用绿叶菜"),
            ("bok_choy", "上海青"), ("broccoli", "西兰花"),
        ):
            conn.execute(
                "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, name_cn, name_cn),
            )
        conn.execute(
            "INSERT INTO ingredient_classifications(ingredient_id,class_id) "
            "VALUES('bok_choy','leafy_vegetable')"
        )
        conn.execute(
            "INSERT INTO dishes(id,name_cn,meal_tags,is_active) "
            "VALUES('dish_leafy','青菜面','[\"lunch\"]',1)"
        )
        conn.execute(
            "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) "
            "VALUES('dish_leafy','any_available_leafy_vegetable',1)"
        )
        conn.execute(
            "INSERT INTO current_pantry(location,ingredient_id,status,is_active) "
            "VALUES('shenzhen','broccoli','available',1)"
        )
        conn.commit()
        conn.close()

        self.assertEqual(
            inventory.check_dish_availability("dish_leafy", "shenzhen")["status"],
            "missing",
        )
        conn = db.get_db()
        conn.execute(
            "INSERT INTO current_pantry(location,ingredient_id,status,is_active) "
            "VALUES('shenzhen','bok_choy','available',1)"
        )
        conn.commit()
        conn.close()
        inventory._invalidate_availability_cache("shenzhen")
        self.assertEqual(
            inventory.check_dish_availability("dish_leafy", "shenzhen")["status"],
            "available",
        )

    def test_household_staples_are_hidden_and_not_added_to_pantry(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en,is_common) "
            "VALUES('ginger','姜','Ginger',1)"
        )
        conn.execute(
            "INSERT INTO current_pantry(location,ingredient_id,status,is_active) "
            "VALUES('shenzhen','ginger','available',1)"
        )
        conn.commit()
        conn.close()
        self.assertEqual(inventory.get_current_pantry("shenzhen")["items"], [])
        self.assertEqual(inventory.get_common_ingredients_static(), [])
        result = inventory.add_ingredient_to_pantry("shenzhen", "ginger")
        self.assertTrue(result["pantry_exempt"])

    def test_dictionary_default_status_is_authoritative(self):
        conn = db.get_db()
        ensure_resolution_schema(conn)
        conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en,is_common) "
            "VALUES('custom_default','测试常备','House Default',1)"
        )
        conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES('ginger','姜','Ginger')"
        )
        conn.execute(
            "INSERT INTO ingredient_dictionary_metadata(ingredient_id,status) "
            "VALUES('custom_default','default')"
        )
        conn.execute(
            "INSERT INTO ingredient_dictionary_metadata(ingredient_id,status) "
            "VALUES('ginger','canonical')"
        )
        conn.commit()
        self.assertTrue(inventory.is_pantry_exempt_ingredient(
            "custom_default", "测试常备", conn))
        self.assertFalse(inventory.is_pantry_exempt_ingredient("ginger", "姜", conn))
        conn.close()
        self.assertEqual(inventory.get_common_ingredients_static(), [])

    def test_ai_fill_falls_back_to_exactly_one_missing_ingredient(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO categories(id,label_cn,label_en) VALUES('protein_main','蛋白质','Protein')"
        )
        for ingredient_id in ("stocked", "missing_one"):
            conn.execute(
                "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, ingredient_id, ingredient_id),
            )
        conn.execute(
            "INSERT INTO current_pantry(location,ingredient_id,status,is_active) "
            "VALUES('shenzhen','stocked','available',1)"
        )
        for dish_id, required in (
            ("dish_used", ("stocked",)),
            ("dish_available", ("stocked",)),
            ("dish_almost", ("stocked", "missing_one")),
        ):
            conn.execute(
                "INSERT INTO dishes(id,name_cn,category_id,meal_tags,meal_roles,protein_types,is_active) "
                "VALUES (?,?, 'protein_main','[\"breakfast\",\"lunch\"]',"
                "'[\"protein_main\"]','[\"beef\"]',1)",
                (dish_id, dish_id),
            )
            for ingredient_id in required:
                conn.execute(
                    "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES (?,?,1)",
                    (dish_id, ingredient_id),
                )
        conn.execute(
            "INSERT INTO menus(id,date,location,status,diners_count) "
            "VALUES(1,'2099-01-02','shenzhen','draft',2)"
        )
        conn.execute(
            "INSERT INTO menu_items(menu_id,dish_id,meal_type,source) "
            "VALUES(1,'dish_used','breakfast','ai')"
        )
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        ok, _, review = menu_service.ai_fill_menu(1, "shenzhen", seed=7, meal_type="lunch")
        self.assertTrue(ok)
        self.assertTrue(any(
            item["dish_id"] == "dish_available"
            and item["availability_status"] == "available"
            for item in review["added_details"]
        ))

        conn = db.get_db()
        conn.execute(
            "DELETE FROM menu_items WHERE menu_id=1 AND meal_type='lunch'"
        )
        conn.execute(
            "INSERT INTO menu_items(menu_id,dish_id,meal_type,source) "
            "VALUES(1,'dish_available','breakfast','ai')"
        )
        conn.commit()
        conn.close()

        ok, _, review = menu_service.ai_fill_menu(1, "shenzhen", seed=7, meal_type="lunch")
        self.assertTrue(ok)
        detail = next(item for item in review["added_details"] if item["dish_id"] == "dish_almost")
        self.assertEqual(detail["availability_status"], "almost_available")
        self.assertEqual([row["name_cn"] for row in detail["missing_required"]], ["missing_one"])

    def test_idempotent_catalog_repairs_add_roles_and_generic_leafy_placeholder(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO categories(id,label_cn,label_en) VALUES('soup','汤','Soup')"
        )
        conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES('蔬菜','蔬菜','Vegetable')"
        )
        conn.execute(
            "INSERT INTO dishes(id,name_cn,category_id,meal_tags,meal_roles,vegetables,slow_soup,is_active) "
            "VALUES('dish_soup','松茸鸡汤','soup','[\"dinner\"]','[]','[\"蔬菜\"]',1,1)"
        )
        conn.execute(
            "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES('dish_soup','蔬菜',1)"
        )
        conn.commit()
        conn.close()

        inventory.ensure_inventory_taxonomy()
        inventory.ensure_inventory_taxonomy()
        menu_service.ensure_dish_slot_metadata()
        menu_service.ensure_dish_slot_metadata()
        conn = db.get_db()
        row = conn.execute(
            "SELECT meal_roles,vegetables FROM dishes WHERE id='dish_soup'"
        ).fetchone()
        required = conn.execute(
            "SELECT ingredient_id FROM dish_ingredients WHERE dish_id='dish_soup'"
        ).fetchall()
        conn.close()
        self.assertIn("slow_soup", json.loads(row["meal_roles"]))
        self.assertEqual(json.loads(row["vegetables"]), ["any_available_leafy_vegetable"])
        self.assertEqual([item["ingredient_id"] for item in required],
                         ["any_available_leafy_vegetable"])

    def test_all_21_canonical_household_staples_are_pantry_exempt(self):
        expected = {
            "大米", "米", "米饭", "面粉", "水", "油", "食用油", "盐", "糖",
            "生抽", "老抽", "蚝油", "醋", "料酒", "葱", "姜", "蒜", "淀粉",
            "胡椒", "鸡精", "小米",
        }
        self.assertEqual(set(inventory.PANTRY_EXEMPT_CANONICAL_IDS), expected)

        conn = db.get_db()
        dish_ids = []
        for index, ingredient_id in enumerate(sorted(expected)):
            conn.execute(
                "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, ingredient_id, ingredient_id),
            )
            dish_id = f"dish_exempt_{index}"
            dish_ids.append(dish_id)
            conn.execute(
                "INSERT INTO dishes (id,name_cn,name_en,meal_tags,is_active) VALUES (?,?,?,'[\"lunch\"]',1)",
                (dish_id, dish_id, dish_id),
            )
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1)",
                (dish_id, ingredient_id),
            )
        conn.commit()
        conn.close()

        for location in ("shenzhen", "hongkong"):
            result = inventory.check_dishes_availability_batch(dish_ids, location)
            self.assertTrue(all(row["status"] == "available" for row in result.values()))
            self.assertTrue(all(not row["missing_required"] for row in result.values()))

    def test_canonical_aliases_and_backend_ids_receive_the_same_exemption(self):
        conn = db.get_db()
        aliases = (
            "rice", "白米", "scallion", "葱花",
            "ginger", "garlic", "蒜蓉", "小米",
        )
        for index, ingredient_id in enumerate(aliases):
            conn.execute(
                "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, ingredient_id, ingredient_id),
            )
            conn.execute(
                "INSERT INTO dishes (id,name_cn,name_en,meal_tags,is_active) VALUES (?,?,?,'[\"lunch\"]',1)",
                (f"dish_alias_{index}", ingredient_id, ingredient_id),
            )
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1)",
                (f"dish_alias_{index}", ingredient_id),
            )
        conn.commit()
        conn.close()

        result = inventory.check_dishes_availability_batch(
            [f"dish_alias_{index}" for index in range(len(aliases))], "shenzhen"
        )
        self.assertTrue(all(row["status"] == "available" for row in result.values()))

    def test_non_exempt_common_beef_is_still_reported_precisely(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO ingredients (ingredient_id,name_cn,name_en,is_common) "
            "VALUES ('beef','牛肉','Beef',1)"
        )
        conn.execute(
            "INSERT INTO ingredients (ingredient_id,name_cn,name_en) "
            "VALUES ('rice','米饭','Rice')"
        )
        conn.execute(
            "INSERT INTO dishes (id,name_cn,name_en,meal_tags,is_active) "
            "VALUES ('dish_rice_beef','牛肉饭','Beef rice','[\"lunch\"]',1)"
        )
        conn.executemany(
            "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES ('dish_rice_beef',?,1)",
            (("rice",), ("beef",)),
        )
        conn.commit()
        conn.close()

        result = inventory.check_dish_availability("dish_rice_beef", "shenzhen")

        self.assertEqual(result["status"], "almost_available")
        self.assertEqual(
            [item["name_cn"] for item in result["missing_required"]],
            ["牛肉"],
        )
        missing_label = "缺 " + "、".join(
            item["name_cn"] for item in result["missing_required"]
        )
        self.assertEqual(missing_label, "缺 牛肉")
        self.assertNotIn("米饭", [item["name_cn"] for item in result["missing_required"]])

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
        self.assertTrue(protein_unmet[0]["hard_warning"])
        self.assertIn("必需槽位缺失", protein_unmet[0]["hard_warning_message"])
        self.assertIn(protein_unmet[0]["hard_warning_message"], review["hard_warnings"])

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
        conn.execute(
            "INSERT INTO ingredients (ingredient_id,name_cn,name_en) "
            "VALUES ('tofu','豆腐','Tofu')"
        )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) "
            "VALUES ('shenzhen','tofu','available',1)"
        )
        for dish_id, role in (
            ("dish_tofu_a", "tofu_dish"), ("dish_tofu_b", "tofu_dish"),
            ("dish_tofu_missing", "tofu_dish"),
            ("dish_egg_a", "egg_dish"), ("dish_egg_b", "egg_dish"),
        ):
            self._insert_switch_dish(
                conn, dish_id, "egg_tofu", [role],
                custom_tags=("早餐豆腐",) if role == "tofu_dish" else ("鸡蛋做法轮换",),
            )
            if role == "tofu_dish":
                conn.execute(
                    "UPDATE dishes SET protein_types='[\"tofu\"]', "
                    "cooking_methods='[\"cold_mix\"]' WHERE id=?",
                    (dish_id,),
                )
                conn.execute(
                    "INSERT INTO dish_ingredients "
                    "(dish_id,ingredient_id,required) VALUES (?,'tofu',1)",
                    (dish_id,),
                )
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

        self.assertEqual(
            set(self._cycle_ids(1, 1, 6)),
            {"dish_tofu_a", "dish_tofu_b", "dish_tofu_missing"},
        )
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

    def test_direct_switch_vegetable_slot_stays_in_vegetable_mushroom_category(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        self._insert_switch_dish(
            conn, "dish_leafy", "vegetable_mushroom", ["vegetable_dish"], meal="dinner",
            vegetables=("生菜",)
        )
        self._insert_switch_dish(
            conn, "dish_stir_fry", "vegetable_mushroom", ["vegetable_dish"], meal="dinner",
            vegetables=("西兰花",)
        )
        self._insert_switch_menu(conn, 1, 1, "dish_leafy", "dinner")
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

    def test_cycle_reports_full_ring_size_and_wrap(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        for index in range(5):
            self._insert_switch_dish(
                conn, f"dish_rice_{index}", "staple_carb", ["staple"],
                meal="lunch", carb_type="rice",
            )
        self._insert_switch_menu(conn, 1, 1, "dish_rice_0", "lunch")
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        sequence = []
        wraps = []
        for _ in range(5):
            chosen = app.get_next_available_same_class_dish(1, 1, "shenzhen")
            sequence.append(chosen["id"])
            wraps.append(chosen["_cycle_wrapped"])
            self.assertEqual(chosen["_pool_size"], 5)
            conn = db.get_db()
            conn.execute("UPDATE menu_items SET dish_id=? WHERE id=1", (chosen["id"],))
            conn.commit()
            conn.close()

        self.assertEqual(sequence, [
            "dish_rice_1", "dish_rice_2", "dish_rice_3", "dish_rice_4", "dish_rice_0"
        ])
        self.assertEqual(wraps, [False, False, False, False, True])

    def test_rice_cycle_keeps_same_day_rice_but_excludes_missing_variant(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO categories (id,label_cn,label_en) VALUES ('staple_carb','主食','Staple')"
        )
        for ingredient_id in ("rice", "special_grain"):
            conn.execute(
                "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, ingredient_id, ingredient_id),
            )
        for index in range(5):
            dish_id = f"dish_rice_{index}"
            conn.execute(
                "INSERT INTO dishes "
                "(id,name_cn,name_en,category_id,meal_tags,meal_roles,carb_type,is_active) "
                "VALUES (?,?,?,'staple_carb','[\"lunch\",\"dinner\"]',"
                "'[\"staple\"]',?,1)",
                (dish_id, f"测试米饭{index}", dish_id,
                 "rice" if index == 4 else "coarse_grain"),
            )
            ingredient_id = "special_grain" if index == 0 else "rice"
            conn.execute(
                "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) VALUES (?,?,1)",
                (dish_id, ingredient_id),
            )
        conn.execute(
            "INSERT INTO menus (id,date,location,status) VALUES (1,'2099-01-01','shenzhen','draft')"
        )
        conn.execute(
            "INSERT INTO menu_items (id,menu_id,dish_id,meal_type,sort_order) VALUES "
            "(1,1,'dish_rice_1','lunch',1),(2,1,'dish_rice_2','dinner',1)"
        )
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        seen = []
        for _ in range(4):
            chosen = app.get_next_available_same_class_dish(1, 1, "shenzhen")
            self.assertEqual(chosen["_pool_size"], 4)
            seen.append(chosen["id"])
            conn = db.get_db()
            conn.execute("UPDATE menu_items SET dish_id=? WHERE id=1", (chosen["id"],))
            conn.commit()
            conn.close()
        self.assertEqual(set(seen), {f"dish_rice_{index}" for index in range(1, 5)})
        self.assertNotIn("dish_rice_0", seen)
        conn = db.get_db()
        self.assertFalse(menu_service._dish_blocked_for_menu(
            conn, 1, "dish_rice_2", 1, meal_type="lunch"
        ))
        conn.close()

    def test_breakfast_dim_sum_names_join_companion_rotation_pool(self):
        for name, expected in (
            ("包子", "bao"), ("花卷", "huajuan"),
            ("日式饺子 煎饺", "jiaozi"), ("水饺", "jiaozi"),
        ):
            analysis = NutritionAnalyzer.analyze({
                "id": name, "name_cn": name, "category_id": "staple_carb",
                "meal_tags": ["breakfast"], "meal_roles": ["staple"],
                "carb_type": "dim_sum", "ingredient_ids": ["flour"],
            })
            self.assertEqual(analysis["breakfast_staple_type"], expected)

    def test_meat_slot_cycle_excludes_egg_and_tofu_protein_mains(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        self._insert_switch_dish(
            conn, "dish_beef", "protein_main", ["protein_main"],
            meal="lunch", proteins=("beef",),
        )
        self._insert_switch_dish(
            conn, "dish_chicken", "protein_main", ["protein_main"],
            meal="lunch", proteins=("chicken",),
        )
        self._insert_switch_dish(
            conn, "dish_egg", "protein_main", ["protein_main", "egg_dish"],
            meal="lunch", proteins=("egg",),
        )
        self._insert_switch_dish(
            conn, "dish_tofu", "protein_main", ["protein_main", "tofu_dish"],
            meal="lunch", proteins=("tofu",),
        )
        self._insert_switch_menu(conn, 1, 1, "dish_beef", "lunch")
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        chosen = app.get_next_available_same_class_dish(1, 1, "shenzhen")
        self.assertEqual(chosen["id"], "dish_chicken")
        self.assertEqual(chosen["_pool_size"], 2)

    def test_breakfast_tofu_uses_canonical_ingredient_without_cold_role(self):
        dish = {
            "id": "tofu_breakfast", "name_cn": "早餐豆腐", "category_id": "protein_main",
            "meal_tags": ["breakfast"], "meal_roles": ["protein_main"],
            "protein_types": ["tofu"], "ingredient_ids": ["tofu"],
            "cooking_methods": ["steam"], "custom_tags": ["早餐豆腐"],
        }
        analysis = NutritionAnalyzer.analyze(dish)
        self.assertTrue(is_breakfast_tofu_candidate(analysis))

    def test_breakfast_meat_target_depends_on_diners(self):
        state = MealState()
        self.assertEqual(analyze_meal_slots("breakfast", state, 1)["breakfast_meat"]["target_min"], 1)
        self.assertEqual(analyze_meal_slots("breakfast", state, 2)["breakfast_meat"]["target_min"], 1)
        self.assertEqual(analyze_meal_slots("breakfast", state, 3)["breakfast_meat"]["target_min"], 1)
        self.assertEqual(analyze_meal_slots("breakfast", state, 4)["breakfast_meat"]["target_min"], 2)

    def test_new_diners_matrix_and_meat_vegetable_offset(self):
        empty = MealState()
        for diners in (1, 2, 3):
            slots = analyze_meal_slots("breakfast", empty, diners)
            self.assertEqual(slots["vegetable"]["target_min"], 1)
            self.assertEqual(slots["breakfast_meat"]["target_min"], 1)
        for diners in (4, 6, 7):
            slots = analyze_meal_slots("breakfast", empty, diners)
            self.assertEqual(slots["vegetable"]["target_min"], 2)
            self.assertEqual(slots["breakfast_meat"]["target_min"], 2)

        self.assertEqual(set(analyze_meal_slots("lunch", empty, 1)), {"one_pot_meal"})
        self.assertEqual(analyze_meal_slots("lunch", empty, 2)["meat_main"]["target_min"], 1)
        self.assertEqual(analyze_meal_slots("lunch", empty, 3)["meat_main"]["target_min"], 2)
        self.assertEqual(analyze_meal_slots("lunch", empty, 6)["vegetable_dish"]["target_min"], 2)
        self.assertEqual(analyze_meal_slots("dinner", empty, 1)["meat_main"]["target_min"], 2)
        self.assertNotIn("egg_tofu", analyze_meal_slots("dinner", empty, 3))
        self.assertEqual(analyze_meal_slots("dinner", empty, 4)["egg_tofu"]["target_min"], 1)

        meat_with_veg = NutritionAnalyzer.analyze({
            "id": "meat_with_veg", "name_cn": "牛肉炒菜心",
            "category_id": "protein_main", "meal_tags": ["breakfast", "lunch", "dinner"],
            "meal_roles": ["protein_main"], "protein_types": ["beef"],
            "vegetables": ["菜心"],
        })
        state = MealState()
        state.add_dish(meat_with_veg, source="ai")
        self.assertEqual(analyze_meal_slots("breakfast", state, 4)["vegetable"]["target_min"], 1)
        self.assertEqual(analyze_meal_slots("lunch", state, 3)["vegetable_dish"]["target_min"], 1)
        self.assertEqual(analyze_meal_slots("dinner", state, 4)["vegetable_dish"]["target_min"], 1)
        self.assertEqual(analyze_meal_slots("lunch", state, 2)["vegetable_dish"]["target_min"], 1)

    def test_breakfast_egg_requires_rotation_tag(self):
        base = {
            "id": "egg", "name_cn": "蒸蛋", "category_id": "egg_tofu",
            "meal_tags": ["breakfast"], "meal_roles": ["egg_dish"],
            "protein_types": ["egg"],
        }
        self.assertFalse(is_breakfast_egg_candidate(NutritionAnalyzer.analyze(base)))
        base["custom_tags"] = ["鸡蛋做法轮换"]
        self.assertTrue(is_breakfast_egg_candidate(NutritionAnalyzer.analyze(base)))

    def test_one_diner_lunch_auto_generates_only_one_pot(self):
        one_pot = {
            "id": "dish_one_pot", "name_cn": "汤面", "category_id": "one_pot_meal",
            "meal_tags": ["lunch"], "meal_roles": [],
            "protein_types": ["pork"], "vegetables": ["菜心"],
        }
        ordinary = {
            "id": "dish_ordinary", "name_cn": "牛肉", "category_id": "protein_main",
            "meal_tags": ["lunch"], "meal_roles": ["protein_main"],
            "protein_types": ["beef"], "vegetables": [],
        }
        context = {"dish_availability": {"dish_one_pot": "available", "dish_ordinary": "available"}}
        filler = GapFiller({"dishes": [one_pot, ordinary]}, seed=3)
        dishes, _, _ = filler.generate_meal("lunch", context=context, diners_count=1)
        self.assertEqual([dish["id"] for dish in dishes], ["dish_one_pot"])

    def test_reconcile_to_one_diner_replaces_ai_stir_fry_with_one_pot(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        self._insert_switch_dish(
            conn, "dish_one_pot", "one_pot_meal", [], meal="lunch",
            proteins=("pork",), vegetables=("菜心",),
        )
        self._insert_switch_dish(
            conn, "dish_old_meat", "protein_main", ["protein_main"], meal="lunch",
            proteins=("beef",),
        )
        conn.execute(
            "INSERT INTO menus(id,date,location,status,diners_count) "
            "VALUES(1,'2099-01-02','shenzhen','draft',1)"
        )
        conn.execute(
            "INSERT INTO menu_items(menu_id,dish_id,meal_type,is_locked,sort_order,source) "
            "VALUES(1,'dish_old_meat','lunch',0,1,'ai')"
        )
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        ok, _, _ = menu_service.reconcile_meal_for_diners(1, "shenzhen")
        self.assertTrue(ok)
        conn = db.get_db()
        ids = [row["dish_id"] for row in conn.execute(
            "SELECT dish_id FROM menu_items WHERE menu_id=1 AND meal_type='lunch'"
        ).fetchall()]
        conn.close()
        self.assertEqual(ids, ["dish_one_pot"])

    def test_breakfast_tofu_tag_repair_is_idempotent(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO categories(id,label_cn,label_en) VALUES('egg_tofu','蛋豆','Egg tofu')"
        )
        conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES('tofu','豆腐','Tofu')"
        )
        conn.execute(
            "INSERT INTO dishes(id,name_cn,category_id,meal_tags,meal_roles,custom_tags,is_active) "
            "VALUES('tag_tofu','蒸豆腐','egg_tofu','[\"breakfast\"]','[\"tofu_dish\"]','[]',1)"
        )
        conn.execute(
            "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES('tag_tofu','tofu',1)"
        )
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        self.assertEqual(menu_service.ensure_breakfast_rotation_metadata(), 1)
        self.assertEqual(menu_service.ensure_breakfast_rotation_metadata(), 0)
        conn = db.get_db()
        row = conn.execute(
            "SELECT custom_tags FROM dishes WHERE id='tag_tofu'"
        ).fetchone()
        conn.close()
        self.assertIn("早餐豆腐", json.loads(row["custom_tags"]))

    def test_reconcile_downsizes_ai_dishes_but_preserves_owner_choice(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        for dish_id, protein in (
            ("meat_owner", "beef"), ("meat_ai_1", "chicken"), ("meat_ai_2", "fish"),
        ):
            self._insert_switch_dish(
                conn, dish_id, "protein_main", ["protein_main"], meal="lunch",
                proteins=(protein,),
            )
        for dish_id, vegetable in (("veg_1", "菜心"), ("veg_2", "西兰花")):
            self._insert_switch_dish(
                conn, dish_id, "vegetable_mushroom", ["vegetable_dish"],
                meal="lunch", vegetables=(vegetable,),
            )
        self._insert_switch_dish(
            conn, "rice", "staple_carb", ["staple"], meal="lunch", carb_type="rice"
        )
        self._insert_switch_dish(
            conn, "quick_soup", "soup", ["quick_soup"], meal="lunch", quick_soup=1
        )
        conn.execute(
            "INSERT INTO menus(id,date,location,status,diners_count) "
            "VALUES(1,'2099-01-02','shenzhen','draft',2)"
        )
        rows = [
            ("meat_owner", 1, "owner"), ("meat_ai_1", 0, "ai"),
            ("meat_ai_2", 0, "ai"), ("veg_1", 0, "ai"),
            ("veg_2", 0, "ai"), ("rice", 0, "ai"), ("quick_soup", 0, "ai"),
        ]
        for order, (dish_id, locked, source) in enumerate(rows, 1):
            conn.execute(
                "INSERT INTO menu_items(menu_id,dish_id,meal_type,is_locked,sort_order,source) "
                "VALUES(1,?,'lunch',?,?,?)",
                (dish_id, locked, order, source),
            )
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        ok, _, _ = menu_service.reconcile_meal_for_diners(1, "shenzhen")
        self.assertTrue(ok)
        conn = db.get_db()
        remaining = conn.execute(
            "SELECT mi.dish_id,mi.source,d.category_id FROM menu_items mi "
            "JOIN dishes d ON d.id=mi.dish_id WHERE mi.menu_id=1 AND mi.meal_type='lunch'"
        ).fetchall()
        conn.close()
        self.assertIn("meat_owner", {row["dish_id"] for row in remaining})
        self.assertEqual(sum(row["category_id"] == "protein_main" for row in remaining), 1)
        self.assertEqual(sum(row["category_id"] == "vegetable_mushroom" for row in remaining), 1)

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

    def test_locked_rice_cycle_and_manual_paths_honor_pantry_exemption(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO categories (id,label_cn,label_en) "
            "VALUES ('staple_carb','主食','Staple')"
        )
        for ingredient_id in ("rice", "16谷米"):
            conn.execute(
                "INSERT INTO ingredients (ingredient_id,name_cn,name_en) VALUES (?,?,?)",
                (ingredient_id, ingredient_id, ingredient_id),
            )
        dishes = (
            ("dish_current", ["rice"]),
            ("dish_0091", ["rice", "16谷米"]),
            ("dish_0094", ["rice"]),
        )
        for dish_id, ingredient_ids in dishes:
            conn.execute(
                "INSERT INTO dishes "
                "(id,name_cn,name_en,category_id,meal_tags,meal_roles,carb_type,is_active) "
                "VALUES (?,?,?,'staple_carb','[\"lunch\",\"dinner\"]',"
                "'[\"staple\"]','rice',1)",
                (dish_id, dish_id, dish_id),
            )
            for ingredient_id in ingredient_ids:
                conn.execute(
                    "INSERT INTO dish_ingredients (dish_id,ingredient_id,required) "
                    "VALUES (?,?,1)",
                    (dish_id, ingredient_id),
                )
        conn.execute(
            "INSERT INTO current_pantry (location,ingredient_id,status,is_active) "
            "VALUES ('shenzhen','16谷米','available',1)"
        )
        conn.execute(
            "INSERT INTO menus (id,date,location,status) "
            "VALUES (1,'2099-01-01','shenzhen','confirmed')"
        )
        conn.execute(
            "INSERT INTO menu_items (menu_id,dish_id,meal_type,sort_order) "
            "VALUES (1,'dish_0091','lunch',1),(1,'dish_0094','dinner',2)"
        )
        conn.execute(
            "INSERT INTO menus (id,date,location,status) "
            "VALUES (2,'2099-01-02','shenzhen','draft')"
        )
        conn.execute(
            "INSERT INTO menu_items (id,menu_id,dish_id,meal_type,sort_order) "
            "VALUES (20,2,'dish_current','lunch',1)"
        )
        conn.commit()

        self.assertFalse(menu_service._dish_blocked_for_menu(conn, 2, "dish_0091"))
        self.assertFalse(menu_service._dish_blocked_for_menu(conn, 2, "dish_0094"))
        conn.close()
        menu_service.invalidate_catalog_cache()

        chosen = app.get_next_available_same_class_dish(2, 20, "shenzhen")
        self.assertEqual(chosen["id"], "dish_0091")
        self.assertTrue(menu_service.add_dish_to_menu(2, "dish_0091", "dinner"))
        self.assertTrue(menu_service.add_dish_to_menu(2, "dish_0094", "dinner"))

    def test_direct_switch_ignores_cross_day_lock_context(self):
        conn = db.get_db()
        self._prepare_switch_inventory(conn)
        for dish_id in ("dish_current", "dish_locked_alternative"):
            self._insert_switch_dish(
                conn, dish_id, "protein_main", ["protein_main"], meal="lunch"
            )
        self._insert_switch_menu(conn, 1, 1, "dish_locked_alternative", "lunch")
        conn.execute("UPDATE menus SET date='2099-01-01',status='confirmed' WHERE id=1")
        self._insert_switch_menu(conn, 2, 2, "dish_current", "lunch")
        conn.commit()
        conn.close()
        menu_service.invalidate_catalog_cache()

        chosen = app.get_next_available_same_class_dish(2, 2, "shenzhen")
        self.assertEqual(chosen["id"], "dish_locked_alternative")

    def test_cross_day_lock_context_does_not_block_available_candidate(self):
        dishes = []
        availability = {}
        for index in range(24):
            dish_id = f"dish_veg_{index:02d}"
            dishes.append({
                "id": dish_id,
                "name_cn": dish_id,
                "category_id": "vegetable_mushroom",
                "meal_tags": ["dinner"],
                "meal_roles": ["vegetable_dish"],
                "vegetables": [f"蔬菜{index}"],
                "protein_types": [],
                "ingredient_ids": [f"ingredient_{index}"],
            })
            availability[dish_id] = "available" if index == 0 else "missing"

        filler = GapFiller({"dishes": dishes}, seed=1)
        candidates, warning = filler.get_slot_candidates(
            "dinner", "vegetable_dish", MealState(),
            context={
                "dish_availability": availability,
                "hard_locked_dish_ids": {"dish_veg_00"},
            },
        )

        self.assertEqual([dish["id"] for dish in candidates], ["dish_veg_00"])

    def test_cross_day_locks_are_ignored_even_when_entire_pool_was_locked(self):
        dishes = []
        availability = {}
        locked = set()
        for index in range(4):
            dish_id = f"dish_breakfast_meat_{index}"
            dishes.append({
                "id": dish_id,
                "name_cn": dish_id,
                "category_id": "protein_main",
                "meal_tags": ["breakfast"],
                "meal_roles": ["protein_main"],
                "protein_types": ["chicken"],
                "vegetables": [],
                "ingredient_ids": [f"ingredient_{index}"],
            })
            availability[dish_id] = "available"
            locked.add(dish_id)

        filler = GapFiller({"dishes": dishes}, seed=1)
        candidates, warning = filler.get_slot_candidates(
            "breakfast", "breakfast_meat", MealState(),
            context={
                "dish_availability": availability,
                "hard_locked_dish_ids": locked,
            },
        )

        self.assertEqual(len(candidates), 4)
        self.assertIsNone(warning)


if __name__ == "__main__":
    unittest.main()
