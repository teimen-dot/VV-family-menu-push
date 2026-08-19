#!/usr/bin/env python3
"""Phase 2 read-only real-data regressions for Pantry, Dishes, and History."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import app
import db


class Phase2RealTabsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "phase2-tabs.db")
        self.photos = os.path.join(self.tmp.name, "photos")
        os.mkdir(self.photos)
        with open(os.path.join(self.photos, "real.jpg"), "wb") as handle:
            handle.write(b"real-photo-fixture")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.photo_patch = patch.object(app, "PHOTOS_DIR", self.photos)
        self.db_patch.start()
        self.photo_patch.start()
        db.init_db()
        self._seed_real_tabs()

    def tearDown(self):
        self.photo_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def _seed_real_tabs(self):
        conn = db.get_db()
        try:
            conn.execute(
                "INSERT INTO categories(id,label_cn,label_en,sort_order) "
                "VALUES('protein_main','蛋白质','Protein',1)"
            )
            for values in (
                ("sz_stock", "深圳库存", "SZ Stock", 1),
                ("hk_stock", "香港库存", "HK Stock", 1),
                ("sz_used", "深圳已用", "SZ Used", 0),
            ):
                conn.execute(
                    "INSERT INTO ingredients(ingredient_id,name_cn,name_en,is_common) "
                    "VALUES(?,?,?,?)", values,
                )
            for values in (
                ("dish_real", "真实菜", "Real Dish", "real.jpg", 1),
                ("dish_fallback", "缺图菜", "Fallback Dish", "missing.jpg", 0),
            ):
                conn.execute(
                    "INSERT INTO dishes(id,name_cn,name_en,category_id,image,banquet,is_active) "
                    "VALUES(?,?,?,'protein_main',?,?,1)", values,
                )
            conn.execute(
                "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) "
                "VALUES('dish_real','sz_stock',1)"
            )
            conn.execute(
                "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) "
                "VALUES('dish_fallback','hk_stock',1)"
            )
            conn.execute(
                "INSERT INTO current_pantry(location,ingredient_id,status,is_active,updated_at) "
                "VALUES('shenzhen','sz_stock','available',1,'2026-08-18 09:00:00')"
            )
            conn.execute(
                "INSERT INTO current_pantry(location,ingredient_id,status,is_active,updated_at) "
                "VALUES('shenzhen','sz_used','available',0,'2026-08-18 08:00:00')"
            )
            conn.execute(
                "INSERT INTO current_pantry(location,ingredient_id,status,is_active,updated_at) "
                "VALUES('hongkong','hk_stock','expiring',1,'2026-08-18 09:00:00')"
            )
            for location, dish_id in (("shenzhen", "dish_real"), ("hongkong", "dish_fallback")):
                menu_id = conn.execute(
                    "INSERT INTO menus(date,location,status,diners_count) "
                    "VALUES('2026-08-18',?,'confirmed',4)", (location,),
                ).lastrowid
                conn.execute(
                    "INSERT INTO menu_items(menu_id,dish_id,meal_type,source) "
                    "VALUES(?,?,'dinner','manual')", (menu_id, dish_id),
                )
            conn.commit()
        finally:
            conn.close()

    def _bootstrap(self, location):
        return app.build_family_menu_bootstrap(
            location, "owner", datetime.fromisoformat("2026-08-19T10:00:00+08:00")
        )

    def test_three_tabs_use_real_rows_and_isolate_locations(self):
        before = self._row_counts()

        shenzhen = self._bootstrap("shenzhen")
        hongkong = self._bootstrap("hongkong")

        self.assertEqual(
            [item["ingredient_id"] for item in shenzhen["pantry"]["items"]],
            ["sz_stock"],
        )
        self.assertEqual(
            [item["ingredient_id"] for item in hongkong["pantry"]["items"]],
            ["hk_stock"],
        )
        self.assertEqual(
            [item["ingredient_id"] for item in shenzhen["pantry"]["recent"]],
            ["sz_used"],
        )
        self.assertEqual(shenzhen["history_stats"], {"days": 1, "meals": 1, "dishes": 1})
        self.assertEqual(hongkong["history_stats"], {"days": 1, "meals": 1, "dishes": 1})
        self.assertEqual(shenzhen["history"][0]["location"], "shenzhen")
        self.assertEqual(hongkong["history"][0]["location"], "hongkong")
        self.assertEqual(before, self._row_counts())

    def test_dishes_keep_banquet_tag_and_only_map_existing_photos(self):
        rows = {dish["id"]: dish for dish in self._bootstrap("shenzhen")["dishes"]}

        self.assertEqual(rows["dish_real"]["image"], "/photos/real.jpg")
        self.assertIsNone(rows["dish_fallback"]["image"])
        self.assertTrue(rows["dish_real"]["banquet"])
        self.assertFalse(rows["dish_fallback"]["banquet"])
        self.assertEqual(rows["dish_real"]["availability"]["status"], "available")

    def test_legacy_schema_safe_still_covers_real_tab_availability(self):
        conn = db.get_db()
        try:
            conn.execute("DROP TABLE ingredient_classifications")
            conn.commit()
        finally:
            conn.close()

        payload = self._bootstrap("shenzhen")

        self.assertEqual(len(payload["dishes"]), 2)
        self.assertEqual(payload["dishes"][0]["availability"]["status"], "available")

    def _row_counts(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("dishes", "ingredients", "current_pantry", "menus", "menu_items", "events")
            }
        finally:
            conn.close()


class Phase2RealTabsStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "public", "family-menu", "index.html")
        with open(path, encoding="utf-8") as handle:
            cls.html = handle.read()

    def test_static_fake_write_handlers_are_disconnected_and_capture_guard_exists(self):
        self.assertNotIn("pAddBtn.addEventListener('click'", self.html)
        self.assertNotIn("pInput.addEventListener('keydown'", self.html)
        self.assertNotIn("document.querySelector('#page-pantry').addEventListener", self.html)
        self.assertNotIn("function addIngredient", self.html)
        self.assertNotIn("function logRecent", self.html)
        self.assertNotIn("consumedStock", self.html)
        self.assertIn("PHASE2_REAL_TABS_BRIDGE_START", self.html)
        self.assertIn("event.stopImmediatePropagation()", self.html)
        self.assertIn("document.addEventListener('keydown', stopPhase2Write, true)", self.html)
        self.assertIn("#page-pantry #stockList .mini-btn", self.html)
        self.assertIn("#page-pantry #recentList .mini-btn", self.html)

    def test_static_rows_are_hidden_or_loading_until_sqlite_hydration(self):
        self.assertIn('id="historyList" hidden', self.html)
        self.assertIn('data-source="sqlite"', self.html)
        self.assertIn("DISH_DB.splice(0, DISH_DB.length, ...rows)", self.html)
        self.assertIn("stockList.dataset.source = 'sqlite'", self.html)
        self.assertIn("historyList.dataset.source = 'sqlite'", self.html)


if __name__ == "__main__":
    unittest.main()
