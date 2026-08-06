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


if __name__ == "__main__":
    unittest.main()
