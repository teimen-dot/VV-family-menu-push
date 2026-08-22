#!/usr/bin/env python3
"""Phase 2 real-data regressions for Pantry, Dishes, and History."""

import os
import inspect
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import app
import db
import menu_service


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
                "INSERT INTO dish_preference_stats(dish_id,vv_confirm_count,vv_confirm_count_30d) "
                "VALUES('dish_real',7,2)"
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
        shenzhen = self._bootstrap("shenzhen")
        hongkong = self._bootstrap("hongkong")
        after_initialization = self._row_counts()
        self._bootstrap("shenzhen")
        self._bootstrap("hongkong")

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
        self.assertEqual(shenzhen["history"][0]["meals"]["dinner"][0]["image"], "/photos/real.jpg")
        self.assertEqual(hongkong["history"][0]["location"], "hongkong")
        self.assertEqual(after_initialization, self._row_counts())

    def test_dishes_keep_banquet_tag_and_only_map_existing_photos(self):
        rows = {dish["id"]: dish for dish in self._bootstrap("shenzhen")["dishes"]}

        self.assertEqual(rows["dish_real"]["image"], "/photos/real.jpg")
        self.assertIsNone(rows["dish_fallback"]["image"])
        self.assertTrue(rows["dish_real"]["banquet"])
        self.assertFalse(rows["dish_fallback"]["banquet"])
        self.assertEqual(rows["dish_real"]["availability"]["status"], "available")
        self.assertEqual(rows["dish_real"]["vv_confirm_count"], 7)
        self.assertTrue(rows["dish_real"]["created_at"])

    def test_history_is_limited_to_previous_fourteen_days(self):
        conn = db.get_db()
        try:
            old_menu_id = conn.execute(
                "INSERT INTO menus(date,location,status,diners_count) "
                "VALUES('2026-08-04','shenzhen','confirmed',4)"
            ).lastrowid
            conn.execute(
                "INSERT INTO menu_items(menu_id,dish_id,meal_type,source) "
                "VALUES(?,'dish_real','dinner','manual')", (old_menu_id,)
            )
            conn.commit()
        finally:
            conn.close()

        payload = self._bootstrap("shenzhen")
        self.assertEqual([row["date"] for row in payload["history"]], ["2026-08-18"])

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

    def test_static_tabs_use_real_write_endpoints_without_capture_blockers(self):
        self.assertIn("PHASE2_REAL_TABS_BRIDGE_START", self.html)
        self.assertNotIn("stopPhase2Write", self.html)
        self.assertNotIn("stopWrite", self.html)
        for endpoint in (
            "/api/pantry/add-by-name", "/api/pantry/update_status", "/api/pantry/consume",
            "/api/dishes/create", "/api/dishes/update", "/api/dishes/favorite",
        ):
            self.assertIn(endpoint, self.html)
        self.assertIn("data-action=\"consume\"", self.html)
        self.assertIn("data-action=\"restock\"", self.html)
        self.assertNotIn("只读预览", self.html)
        self.assertNotIn("当前为只读", self.html)

    def test_production_pantry_consume_has_no_preview_gate(self):
        self.assertNotIn("preview only", app.AppHandler.do_POST.__code__.co_consts)

    def test_hydration_keeps_real_confirmed_meal_progress(self):
        hydrate = self.html.split("function hydrate(data)", 1)[1].split(
            "async function load(location)", 1
        )[0]
        self.assertIn("updateConfirmProgress();", hydrate)
        self.assertNotIn("可操作测试版", hydrate)
        self.assertNotIn("LIVE TEST DATA", hydrate)

    def test_today_restored_meals_are_hydrated_as_real_cards(self):
        self.assertIn("function updateTodayRestoredMeals", self.html)
        self.assertIn("updateTodayRestoredMeals(days[0]", self.html)
        self.assertIn("menu.meal_settings?.[mealType]", self.html)

    def test_dish_and_same_category_picker_sort_contracts_are_present(self):
        self.assertIn("d.avail === 'ok' && d.fav ? 0", self.html)
        self.assertIn("b.vvConfirmCount", self.html)
        self.assertIn("availableFav", self.html)
        self.assertIn("差少量 · 同类菜", self.html)
        self.assertNotIn("const eggPenalty", self.html)

    def test_history_photos_are_center_cropped_and_almost_fill_is_explained(self):
        self.assertIn(".h-meal .hm-tiles .tile img", self.html)
        self.assertIn("object-fit: cover", self.html)
        self.assertIn("availability_status === 'almost_available'", self.html)
        self.assertIn("差少量：缺", self.html)

    def test_menu_creation_does_not_require_legacy_unique_constraint(self):
        source = inspect.getsource(menu_service.generate_and_store_menu)
        self.assertNotIn("ON CONFLICT(date, location)", source)
        self.assertIn('conn.execute("BEGIN IMMEDIATE")', source)

    def test_pantry_rows_have_no_ingredient_image_surface(self):
        pantry_row = self.html.split("function pantryRow(item, recent = false)", 1)[1].split(
            "function renderPantry", 1
        )[0]
        self.assertNotIn("<img", pantry_row)
        self.assertNotIn("item.image", pantry_row)
        self.assertIn("data-action=\"consume\"", pantry_row)
        self.assertIn("data-action=\"restock\"", pantry_row)

    def test_static_rows_are_hidden_or_loading_until_sqlite_hydration(self):
        self.assertIn('id="historyList" hidden', self.html)
        self.assertIn('data-source="sqlite"', self.html)
        self.assertIn("DISH_DB.splice(0, DISH_DB.length, ...rows)", self.html)
        self.assertIn("stockList.dataset.source = 'sqlite'", self.html)
        self.assertIn("historyList.dataset.source = 'sqlite'", self.html)


if __name__ == "__main__":
    unittest.main()
