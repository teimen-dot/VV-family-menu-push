#!/usr/bin/env python3
import importlib
import json
import os
import tempfile
import unittest
from datetime import date, timedelta


class TomorrowSearchMobileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_env = {
            key: os.environ.get(key)
            for key in ("FAMILY_MENU_DB_PATH", "FAMILY_MENU_PHOTOS_DIR", "LOCAL_PREVIEW_UI", "APP_ENV", "PUSH_ENABLED")
        }
        os.environ.update({
            "FAMILY_MENU_DB_PATH": os.path.join(self.tmp.name, "test.db"),
            "FAMILY_MENU_PHOTOS_DIR": os.path.join(self.tmp.name, "photos"),
            "LOCAL_PREVIEW_UI": "true", "APP_ENV": "development", "PUSH_ENABLED": "false",
        })
        import db, inventory, menu_service, app
        for module in (db, inventory, menu_service, app):
            importlib.reload(module)
        self.db, self.inventory, self.app = db, inventory, app
        db.init_db()
        self.tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self._seed()

    def tearDown(self):
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import db, inventory, menu_service, app
        for module in (db, inventory, menu_service, app):
            importlib.reload(module)
        self.tmp.cleanup()

    def _seed(self):
        conn = self.db.get_db()
        conn.execute("INSERT INTO categories(id,label_cn,label_en,active) VALUES('vegetable_mushroom','蔬菜','Vegetable',1)")
        conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES('asparagus','芦笋','Asparagus','[]')")
        conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES('tofu','豆腐','Tofu','[]')")
        dishes = [
            ("dish_current", "芦笋炒菜", "Stir-fried Asparagus"),
            ("dish_available", "清炒芦笋", "Sauteed Asparagus"),
            ("dish_missing", "豆腐芦笋", "Tofu Asparagus"),
            ("dish_chicken", "香煎鸡肉", "Pan-fried Chicken"),
        ]
        for dish_id, cn, en in dishes:
            conn.execute(
                "INSERT INTO dishes(id,name_cn,name_en,category_id,meal_tags,is_active,image) "
                "VALUES(?,?,?,'vegetable_mushroom','[\"lunch\"]',1,?)",
                (dish_id, cn, en, dish_id + ".jpg"),
            )
        for index in range(25):
            conn.execute(
                "INSERT INTO dishes(id,name_cn,name_en,category_id,meal_tags,is_active) "
                "VALUES(?,?,?,'vegetable_mushroom','[\"lunch\"]',1)",
                (f"dish_search_{index:02d}", f"芦笋菜{index:02d}", f"Asparagus Dish {index:02d}"),
            )
        for index in range(10):
            dish_id = f"dish_roulette_{index:02d}"
            conn.execute(
                "INSERT INTO dishes(id,name_cn,name_en,category_id,meal_tags,is_active) "
                "VALUES(?,?,?,'vegetable_mushroom','[\"lunch\"]',1)",
                (dish_id, f"轮换菜{index:02d}", f"Rotation Dish {index:02d}"),
            )
            conn.execute(
                "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES(?, 'asparagus', 1)",
                (dish_id,),
            )
        conn.execute("INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES('dish_available','asparagus',1)")
        conn.execute("INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES('dish_missing','tofu',1)")
        conn.execute("INSERT INTO current_pantry(location,ingredient_id,status,is_active) VALUES('shenzhen','asparagus','available',1)")
        conn.execute("INSERT INTO config(key,value) VALUES('inventory_version_shenzhen','1')")
        conn.execute("INSERT INTO menus(id,date,location,status,diners) VALUES(1,?,'shenzhen','draft','[]')", (self.tomorrow,))
        conn.execute(
            "INSERT INTO menu_items(id,menu_id,dish_id,meal_type,sort_order,source) "
            "VALUES(10,1,'dish_current','lunch',0,'owner')"
        )
        conn.commit()
        conn.close()

    def test_search_is_bilingual_and_limited_to_twenty(self):
        self.assertLessEqual(len(self.app.get_all_dishes(search="芦笋", location="shenzhen")), 20)
        english = self.app.get_all_dishes(search="Asparagus", location="shenzhen")
        self.assertTrue(english)
        self.assertLessEqual(len(english), 20)

    def test_smart_replace_keeps_same_meal_category_and_requires_available(self):
        ok, message, replacement = self.app.smart_replace_menu_item(1, 10, "shenzhen")
        self.assertTrue(ok, message)
        self.assertNotEqual(replacement, "dish_missing")
        self.assertEqual(
            "available",
            self.inventory.check_dish_availability(replacement, "shenzhen")["status"],
        )
        conn = self.db.get_db()
        row = conn.execute(
            "SELECT mi.dish_id,mi.meal_type,d.category_id FROM menu_items mi JOIN dishes d ON d.id=mi.dish_id WHERE mi.id=10"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(row), (replacement, "lunch", "vegetable_mushroom"))

    def test_smart_replace_cycles_after_all_legal_candidates(self):
        conn = self.db.get_db()
        pool_size = conn.execute(
            "SELECT COUNT(DISTINCT d.id) FROM dishes d JOIN dish_ingredients di ON di.dish_id=d.id "
            "WHERE d.is_active=1 AND d.category_id='vegetable_mushroom' "
            "AND d.meal_tags LIKE '%lunch%' AND di.ingredient_id='asparagus' AND di.required=1"
        ).fetchone()[0]
        conn.close()
        replacements = []
        for _ in range(pool_size + 2):
            ok, message, replacement = self.app.smart_replace_menu_item(1, 10, "shenzhen")
            self.assertTrue(ok, message)
            replacements.append(replacement)
        self.assertEqual(pool_size, len(set(replacements[:pool_size])))
        self.assertEqual(replacements[:2], replacements[pool_size:pool_size + 2])

    def test_owner_mobile_markup_is_stable_and_worker_has_no_replace_controls(self):
        owner = self.app.render_tomorrow_reference_preview("owner", "shenzhen")
        worker = self.app.render_tomorrow_reference_preview("worker", "shenzhen")
        self.assertIn("智能换一道 Smart replace", owner)
        self.assertIn("搜索更换 Search replace", owner)
        self.assertIn("setTimeout(()=>q?doDishSearch(q):loadDishPicker(),300)", owner)
        self.assertIn("requestId!==searchRequestId", owner)
        self.assertIn('loading="lazy"', owner)
        self.assertIn("处理中… Processing…", owner)
        self.assertIn('title="删除 Delete"', owner)
        pick_source = owner.split("async function doPickDish", 1)[1].split("async function removeDish", 1)[0]
        self.assertNotIn("location.reload", pick_source)
        self.assertNotIn("智能换一道 Smart replace", worker)
        self.assertFalse(self.app.post_path_allowed("worker", "/api/tomorrow/smart-replace"))

        os.environ["LOCAL_PREVIEW_UI"] = "false"
        legacy = self.app.render_tomorrow("owner", "shenzhen")
        self.assertIn("普通切换 Switch", legacy)
        self.assertIn("搜索更换 Search replace", legacy)
        self.assertIn("requestId!==_searchRequestId", legacy)
        self.assertIn('loading="lazy"', legacy)
        legacy_pick = legacy.split("async function doPickDish", 1)[1].split("async function removeDish", 1)[0]
        self.assertNotIn("location.reload", legacy_pick)


if __name__ == "__main__":
    unittest.main(verbosity=2)
