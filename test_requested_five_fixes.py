#!/usr/bin/env python3
import importlib
import os
import tempfile
import unittest
from datetime import date, timedelta


class RequestedFiveFixesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["FAMILY_MENU_DB_PATH"] = os.path.join(self.tempdir.name, "test.db")
        os.environ["FAMILY_MENU_PHOTOS_DIR"] = os.path.join(self.tempdir.name, "photos")
        os.environ["APP_ENV"] = "development"
        os.environ["PUSH_ENABLED"] = "false"
        os.environ["LOCAL_PREVIEW_UI"] = "true"
        import db, ingredient_service, inventory, menu_service, photo_manager, app
        for module in (db, ingredient_service, inventory, menu_service, photo_manager, app):
            importlib.reload(module)
        self.db, self.ingredients = db, ingredient_service
        self.inventory, self.menu_service = inventory, menu_service
        self.photo_manager, self.app = photo_manager, app
        db.init_db()
        os.makedirs(os.environ["FAMILY_MENU_PHOTOS_DIR"], exist_ok=True)
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self._seed()

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed(self):
        conn = self.db.get_db()
        try:
            conn.execute("INSERT INTO categories(id,label_cn,label_en,active) VALUES('staple_carb','主食','Staple',1)")
            conn.execute("INSERT INTO categories(id,label_cn,label_en,active) VALUES('protein_main','主菜','Main',1)")
            for dish_id, name in (("dish_jiaozi", "煎饺"), ("dish_baozi", "包子")):
                conn.execute(
                    "INSERT INTO dishes(id,name_cn,name_en,category_id,meal_tags,carb_type,is_active) "
                    "VALUES(?,?,?,'staple_carb','[\"breakfast\"]','dim_sum',1)",
                    (dish_id, name, name),
                )
            conn.execute(
                "INSERT INTO menus(id,date,location,status,diners) VALUES(1,?,'shenzhen','draft','[]')",
                (self.tomorrow,),
            )
            conn.execute(
                "INSERT INTO menu_items(menu_id,dish_id,meal_type,sort_order,source) "
                "VALUES(1,'dish_jiaozi','breakfast',0,'owner')"
            )
            conn.commit()
        finally:
            conn.close()
        self.menu_service.invalidate_catalog_cache()

    def test_1_baozi_replacement_uses_real_category_and_meal_tags(self):
        conn = self.db.get_db()
        item_id = conn.execute("SELECT id FROM menu_items WHERE menu_id=1").fetchone()[0]
        conn.close()
        self.assertTrue(self.menu_service.replace_dish_in_menu(1, item_id, "dish_baozi")[0])
        validation = self.app.validate_menu_after_mutation(1)
        self.assertEqual(validation["meal_slots"]["breakfast"]["companion_staple"]["missing_min"], 0)

    def test_2_pending_count_uses_independent_meal_slots_only(self):
        missing = {"breakfast": {}, "lunch": {"staple": {"missing_min": 1}}, "dinner": {}}
        self.assertEqual(self.app.independent_missing_issue_keys(missing), {"lunch:staple"})

    def test_3_complete_new_dish_is_immediately_visible_with_image_and_status(self):
        payload = {
            "name_cn": "测试牛肉菜", "name_en": "Test Beef Dish", "category_id": "protein_main",
            "ui_version": self.photo_manager.ADMIN_UI_VERSION,
            "meal_tags": ["lunch"], "protein_types": ["beef"],
            "required_ingredients": ["牛肉"], "meal_roles": ["protein_main"],
        }
        handler = object.__new__(self.photo_manager.PhotoManagerHandler)
        handler._read_body = lambda: payload
        response = {}
        handler._json_response = lambda code, data: response.update(code=code, data=data)
        handler._handle_add_dish()
        self.assertEqual(response["code"], 200, response)
        dish_id = response["data"]["id"]
        ingredient_id = response["data"]["required_ingredients"][0]
        self.inventory.add_ingredient_to_pantry("shenzhen", ingredient_id)
        filename = self.photo_manager.store_photo_for_dish(
            payload["name_cn"], "test_beef_dish", b"\xff\xd8\xffok"
        )
        dish = next(item for item in self.app.get_all_dishes() if item["id"] == dish_id)
        availability = self.inventory.check_dish_availability(dish_id, "shenzhen")
        self.assertEqual(dish["image"], filename)
        self.assertEqual(availability["status"], "available")
        self.assertTrue(availability["data_complete"])

    def test_4_bilingual_add_never_fuzzy_merges_and_admin_can_complete(self):
        conn = self.db.get_db()
        salmon, _ = self.ingredients.add_or_get_ingredient(conn, "三文鱼")
        roe, _ = self.ingredients.add_or_get_ingredient(conn, "三文鱼籽")
        unknown, _ = self.ingredients.add_or_get_ingredient(conn, "Dragon Fruit Blossom")
        conn.commit()
        self.assertNotEqual(salmon["ingredient_id"], roe["ingredient_id"])
        self.assertEqual(salmon["name_en"], "Salmon")
        self.assertEqual(unknown["translation_pending"], 1)
        completed = self.ingredients.update_ingredient_names(
            conn, unknown["ingredient_id"], "火龙果花", "Dragon Fruit Blossom"
        )
        conn.commit()
        conn.close()
        self.assertEqual(completed["translation_pending"], 0)

    def test_5_pantry_status_commits_before_success_and_ui_updates_without_reload(self):
        conn = self.db.get_db()
        ingredient, _ = self.ingredients.add_or_get_ingredient(conn, "鸡蛋")
        conn.commit()
        conn.close()
        self.inventory.add_ingredient_to_pantry("shenzhen", ingredient["ingredient_id"])
        result = self.inventory.update_ingredient_status("shenzhen", ingredient["ingredient_id"], "expiring")
        self.assertTrue(result["ok"])
        pantry = self.inventory.get_current_pantry("shenzhen")
        item = next(value for value in pantry["items"] if value["ingredient_id"] == ingredient["ingredient_id"])
        self.assertEqual(item["status"], "expiring")
        html = self.app.render_pantry_reference_preview("owner", "shenzhen")
        toggle_source = html.split("async function toggleNeedsAttention", 1)[1].split("async function usedUpAndRecord", 1)[0]
        used_source = html.split("async function usedUpAndRecord", 1)[1].split("async function toggleCommon", 1)[0]
        self.assertNotIn("location.reload", toggle_source)
        self.assertNotIn("location.reload", used_source)
        self.assertIn("pantryStateMatches", toggle_source)
        self.assertIn("row.remove()", used_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
