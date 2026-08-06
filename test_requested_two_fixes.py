#!/usr/bin/env python3
import importlib
import os
import sqlite3
import tempfile
import unittest


class RequestedTwoFixesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        os.environ.update({
            "FAMILY_MENU_DB_PATH": self.db_path,
            "FAMILY_MENU_PHOTOS_DIR": os.path.join(self.tmp.name, "photos"),
            "H5_BASE_URL": "https://menu.ourmenu.site",
            "APP_ENV": "development", "PUSH_ENABLED": "false",
        })
        import db, ingredient_service, inventory, menu_service, photo_manager, app
        for module in (db, ingredient_service, inventory, menu_service, photo_manager, app):
            importlib.reload(module)
        self.db, self.ingredients = db, ingredient_service
        self.inventory, self.photo_manager, self.app = inventory, photo_manager, app
        db.init_db()
        os.makedirs(os.environ["FAMILY_MENU_PHOTOS_DIR"], exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_1_bilingual_create_and_existing_single_language_repair(self):
        conn = self.db.get_db()
        salmon, _ = self.ingredients.add_or_get_ingredient(conn, name_cn="三文鱼")
        pork, _ = self.ingredients.add_or_get_ingredient(conn, name_en="Pork Belly")
        conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases,translation_pending) "
            "VALUES('legacy_shanghai','上海青','','[]',0)"
        )
        repaired, created = self.ingredients.add_or_get_ingredient(conn, name_cn="上海青")
        conn.commit()
        conn.close()
        self.assertEqual(salmon["name_en"], "Salmon")
        self.assertEqual(pork["name_cn"], "五花肉")
        self.assertFalse(created)
        self.assertEqual(repaired["name_en"], "Shanghai Bok Choy")

    def test_2_admin_save_is_immediately_complete_in_family_api(self):
        conn = self.db.get_db()
        conn.execute("INSERT INTO categories(id,label_cn,label_en,active) VALUES('protein_main','主菜','Main',1)")
        beef, _ = self.ingredients.add_or_get_ingredient(conn, name_cn="牛肉", name_en="Beef")
        conn.execute(
            "INSERT INTO dishes(id,name_cn,name_en,category_id,meal_tags,is_active,image) "
            "VALUES('dish_0999','测试牛肉','Test Beef','protein_main','[\"dinner\"]',1,'test_beef.jpg')"
        )
        conn.commit()
        conn.close()
        self.inventory.add_ingredient_to_pantry("shenzhen", beef["ingredient_id"])
        self.assertEqual(self.inventory.check_dish_availability("dish_0999", "shenzhen")["status"], "incomplete")

        payload = {
            "id": "dish_0999", "name_cn": "测试牛肉", "name_en": "Test Beef",
            "category_id": "protein_main", "meal_tags": ["dinner"],
            "required_ingredients": [beef["ingredient_id"]],
        }
        handler = object.__new__(self.photo_manager.PhotoManagerHandler)
        handler._read_body = lambda: payload
        response = {}
        handler._json_response = lambda code, data: response.update(code=code, data=data)
        handler._handle_edit_dish()
        self.assertEqual(response["code"], 200, response)

        dish = next(d for d in self.app.get_all_dishes(location="shenzhen") if d["id"] == "dish_0999")
        self.assertEqual(dish["required_ingredients"][0]["ingredient_id"], beef["ingredient_id"])
        self.assertEqual(dish["availability"]["status"], "available")
        self.assertEqual(dish["image_url"], "https://menu.ourmenu.site/photos/test_beef.jpg")
        self.assertEqual(dish["missing_fields"], [])

        importlib.reload(self.inventory)
        importlib.reload(self.app)
        refreshed = next(d for d in self.app.get_all_dishes(location="shenzhen") if d["id"] == "dish_0999")
        self.assertEqual(refreshed["availability"]["status"], "available")

    def test_3_backfill_only_writes_verified_database_copy(self):
        conn = self.db.get_db()
        conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases,translation_pending) "
            "VALUES('legacy_salmon','三文鱼','','[]',0)"
        )
        conn.commit()
        conn.close()
        output = os.path.join(self.tmp.name, "backfilled.db")
        from backfill_ingredient_translations import backfill_copy
        result = backfill_copy(self.db_path, output)
        source = sqlite3.connect(self.db_path).execute(
            "SELECT name_en FROM ingredients WHERE ingredient_id='legacy_salmon'"
        ).fetchone()[0]
        copied = sqlite3.connect(output).execute(
            "SELECT name_en,translation_pending FROM ingredients WHERE ingredient_id='legacy_salmon'"
        ).fetchone()
        self.assertEqual(source, "")
        self.assertEqual(copied, ("Salmon", 0))
        self.assertGreaterEqual(result["translated"], 1)

    def test_4_admin_add_shows_required_field_and_rejects_stale_page_clearly(self):
        html = self.photo_manager.HTML_PAGE
        add_modal = html.split("<!-- Add Modal -->", 1)[1].split("<!-- Category Manager Modal -->", 1)[0]
        self.assertLess(add_modal.index("必需食材 / Required ingredients"), add_modal.index("V3 汤类标签"))
        self.assertIn("const requiredIngredients = getTagValues('addRequiredIngredients')", html)
        self.assertIn("无需按回车", html)
        self.assertIn(self.photo_manager.ADMIN_UI_VERSION, html)
        self.assertNotIn("'{ADMIN_UI_VERSION}'", html)
        handler = object.__new__(self.photo_manager.PhotoManagerHandler)
        handler._read_body = lambda: {
            "name_cn": "黄瓜", "name_en": "Cucumber Shrimp Patties",
            "category_id": "protein_main", "meal_tags": ["breakfast", "lunch", "dinner"],
        }
        response = {}
        handler._json_response = lambda code, data: response.update(code=code, data=data)
        handler._handle_add_dish()
        self.assertEqual(response["code"], 409)
        self.assertTrue(response["data"]["reload_required"])
        self.assertIn("刷新页面", response["data"]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
