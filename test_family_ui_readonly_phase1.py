#!/usr/bin/env python3
"""Phase 1 final Family UI read-only bootstrap and asset contracts."""

import hashlib
import io
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import app
import db
import inventory
import menu_service


def menu_for(date_str, location, with_dishes=True, **_kwargs):
    suffix = date_str.replace("-", "")
    meals = {}
    for index, meal_type in enumerate(app.READONLY_MEAL_TYPES, start=1):
        meals[meal_type] = ([{
            "menu_item_id": index,
            "dish_id": f"dish_{suffix}_{index}",
            "name_cn": f"真实菜品{index}",
            "name_en": f"Live Dish {index}",
            "image": f"dish-{index}.jpg",
        }] if with_dishes else [])
    return {
        "date": date_str,
        "exists": True,
        "menu_id": int(suffix[-4:]),
        "status": "draft",
        "location": location,
        "diners": ["vv", "toby"],
        "diners_count": 2,
        "meal_notes": {"lunch": "少油"},
        "availability": {
            f"dish_{suffix}_{index}": {"status": "available", "missing_required": []}
            for index in range(1, 4)
        },
        "shortages": {},
        "meals": meals,
    }


class EffectiveDinersCountTests(unittest.TestCase):
    @staticmethod
    def _menu_row(**overrides):
        row = {
            "diners": None,
            "diners_count": 4,
        }
        row.update(overrides)
        return row

    def test_empty_diners_list_falls_back_to_positive_diners_count(self):
        row = self._menu_row(diners="[]", diners_count=4)
        self.assertEqual(menu_service._get_effective_diners_count(menu_row=row), 4)

    def test_nonempty_diners_list_does_not_override_positive_diners_count(self):
        row = self._menu_row(diners='["a","b","c"]', diners_count=4)
        self.assertEqual(menu_service._get_effective_diners_count(menu_row=row), 4)

    def test_null_or_empty_diners_falls_back_to_positive_diners_count(self):
        for diners in (None, ""):
            with self.subTest(diners=diners):
                row = self._menu_row(diners=diners, diners_count=4)
                self.assertEqual(menu_service._get_effective_diners_count(menu_row=row), 4)

    def test_non_array_json_falls_back_to_positive_diners_count(self):
        for diners in ("{}", "null"):
            with self.subTest(diners=diners):
                row = self._menu_row(diners=diners, diners_count=4)
                self.assertEqual(menu_service._get_effective_diners_count(menu_row=row), 4)

    def test_legacy_banquet_columns_do_not_override_normal_diners(self):
        row = self._menu_row(
            diners="[]",
            diners_count=4,
            meal_mode="banquet",
            banquet_total_diners=8,
        )
        self.assertEqual(menu_service._get_effective_diners_count(menu_row=row), 4)

    def test_all_invalid_values_keep_final_fallback(self):
        row = self._menu_row(
            diners="not-json",
            diners_count=0,
        )
        self.assertEqual(menu_service._get_effective_diners_count(menu_row=row), 4)


class BanquetDishPreservationTests(unittest.TestCase):
    def test_banquet_dish_can_still_be_added_manually(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "banquet-dish.db")
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                conn = db.get_db()
                try:
                    conn.execute(
                        "INSERT INTO dishes(id,name_cn,banquet,is_active) VALUES(?,?,1,1)",
                        ("dish_banquet_manual", "保留家宴菜"),
                    )
                    menu_id = conn.execute(
                        "INSERT INTO menus(date,location,status,diners_count,diners) "
                        "VALUES('2099-01-01','shenzhen','draft',4,'[]')"
                    ).lastrowid
                    conn.commit()
                finally:
                    conn.close()

                self.assertTrue(
                    menu_service.add_dish_to_menu(menu_id, "dish_banquet_manual", "dinner")
                )
                conn = db.get_db()
                try:
                    item = conn.execute(
                        "SELECT dish_id, source, is_locked FROM menu_items WHERE menu_id=?",
                        (menu_id,),
                    ).fetchone()
                    dish = conn.execute(
                        "SELECT banquet FROM dishes WHERE id='dish_banquet_manual'"
                    ).fetchone()
                finally:
                    conn.close()

                self.assertEqual(dict(item), {
                    "dish_id": "dish_banquet_manual", "source": "owner", "is_locked": 1,
                })
                self.assertEqual(dish["banquet"], 1)


class ReadonlyBootstrapTests(unittest.TestCase):
    def test_bootstrap_reads_exact_four_days_and_keeps_location_boundary(self):
        now = datetime(2026, 8, 18, 11, 0)
        calls = []

        def fake_get(date_str, location, **kwargs):
            calls.append((date_str, location, kwargs))
            return menu_for(date_str, location)

        with patch.object(app, "get_menu_with_dishes", side_effect=fake_get):
            result = app.build_family_menu_bootstrap("hongkong", "worker", now=now)

        expected_dates = [(now.date() + timedelta(days=offset)).isoformat() for offset in range(4)]
        self.assertEqual([day["date"] for day in result["days"]], expected_dates)
        self.assertEqual(calls, [
            (date_str, "hongkong", {"record_filter_events": False})
            for date_str in expected_dates
        ])
        self.assertEqual(result["location"], "hongkong")
        self.assertEqual(result["role"], "worker")
        self.assertTrue(result["readonly"])
        self.assertEqual(result["next_meal"]["meal_type"], "lunch")
        self.assertEqual(result["next_meal"]["date"], expected_dates[0])
        self.assertEqual(result["next_meal"]["diners_count"], 2)
        self.assertEqual(result["next_meal"]["note"], "少油")

    def test_after_dinner_cutoff_selects_tomorrow_breakfast(self):
        now = datetime(2026, 8, 18, 22, 1)
        with patch.object(app, "get_menu_with_dishes", side_effect=menu_for):
            result = app.build_family_menu_bootstrap("shenzhen", "owner", now=now)
        self.assertEqual(result["next_meal"]["day_offset"], 1)
        self.assertEqual(result["next_meal"]["meal_type"], "breakfast")
        self.assertEqual(result["next_meal"]["date"], "2026-08-19")

    def test_missing_days_are_reported_without_generation_or_mutation(self):
        missing = {"exists": False, "date": "ignored"}
        with patch.object(app, "get_menu_with_dishes", return_value=missing), \
             patch.object(app, "generate_and_store_menu") as generate, \
             patch.object(app, "ensure_tomorrow_menu") as ensure:
            result = app.build_family_menu_bootstrap(
                "shenzhen", "owner", now=datetime(2026, 8, 18, 8, 0)
            )
        generate.assert_not_called()
        ensure.assert_not_called()
        self.assertTrue(all(not day["menu"]["exists"] for day in result["days"]))
        self.assertEqual(result["next_meal"]["meal_type"], "breakfast")
        self.assertIsNone(result["next_meal"]["menu_id"])

    def test_http_get_uses_cookie_location_and_verified_role(self):
        handler = object.__new__(app.AppHandler)
        handler.path = "/api/family-menu/bootstrap"
        handler.headers = {"Cookie": "loc=hongkong"}
        handler.request_role = lambda: "worker"
        captured = {}
        handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
        with patch.object(app, "build_family_menu_bootstrap", return_value={"readonly": True}) as build:
            app.AppHandler.do_GET(handler)
        build.assert_called_once_with("hongkong", "worker")
        self.assertEqual(captured, {"payload": {"readonly": True}, "status": 200})

    def test_repeated_bootstrap_and_kitchen_switch_are_byte_for_byte_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "readonly.db")
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                conn = db.get_db()
                try:
                    now = datetime(2026, 8, 18, 11, 0)
                    for location in ("shenzhen", "hongkong"):
                        for offset in range(4):
                            date_str = (now.date() + timedelta(days=offset)).isoformat()
                            cursor = conn.execute(
                                "INSERT INTO menus(date,location,status,diners_count,diners) "
                                "VALUES(?,?,'draft',2,'[\"vv\",\"toby\"]')",
                                (date_str, location),
                            )
                            # Deliberately dirty row: legacy reads log an event while strict reads must not.
                            conn.execute(
                                "INSERT INTO menu_items(menu_id,dish_id,custom_name,meal_type) "
                                "VALUES(?,NULL,NULL,'breakfast')",
                                (cursor.lastrowid,),
                            )
                    conn.commit()
                finally:
                    conn.close()

                before_hash = self._sha256(db_path)
                before_snapshot = self._snapshot(db_path)
                first = app.build_family_menu_bootstrap("shenzhen", "owner", now=now)
                second = app.build_family_menu_bootstrap("shenzhen", "owner", now=now)
                hongkong = app.build_family_menu_bootstrap("hongkong", "owner", now=now)
                third = app.build_family_menu_bootstrap("shenzhen", "owner", now=now)
                after_snapshot = self._snapshot(db_path)
                after_hash = self._sha256(db_path)

                self.assertEqual(first, second)
                self.assertEqual(first, third)
                self.assertEqual(hongkong["location"], "hongkong")
                self.assertEqual(before_snapshot, after_snapshot)
                self.assertEqual(before_hash, after_hash)
                check = sqlite3.connect(db_path)
                try:
                    self.assertEqual(check.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
                finally:
                    check.close()

                # All menu reads are now strictly read-only, including the legacy default.
                menu_service.get_menu_with_dishes("2026-08-18", "shenzhen")
                check = sqlite3.connect(db_path)
                try:
                    self.assertEqual(check.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
                finally:
                    check.close()

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _snapshot(path):
        conn = sqlite3.connect(path)
        try:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )]
            return tuple((table, tuple(conn.execute(f'SELECT * FROM "{table}"').fetchall())) for table in tables)
        finally:
            conn.close()


class ReadonlyUiAssetTests(unittest.TestCase):
    SOURCE_PATH = "/Users/heymen/Documents/kimi/Workspaces/菜单系统ui重组/deploy/index.html"
    BRIDGE_START = b"<!-- PHASE1_REAL_DATA_BRIDGE_START -->"
    BRIDGE_END = b"<!-- PHASE1_REAL_DATA_BRIDGE_END -->"

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(os.path.dirname(__file__), "public", "family-menu")
        with open(cls.SOURCE_PATH, "rb") as handle:
            cls.source_bytes = handle.read()
        with open(os.path.join(cls.root, "index.html"), "rb") as handle:
            cls.target_bytes = handle.read()
        cls.html = cls.target_bytes.decode("utf-8")
        cls.bridge = cls.target_bytes.split(cls.BRIDGE_START, 1)[1].split(cls.BRIDGE_END, 1)[0].decode("utf-8")
        with open(os.path.join(cls.root, "app.js"), encoding="utf-8") as handle:
            cls.js = handle.read()
        with open(os.path.join(cls.root, "styles.css"), encoding="utf-8") as handle:
            cls.css = handle.read()

    def test_source_is_byte_for_byte_identical_after_bridge_is_removed(self):
        self.assertEqual(self.target_bytes.count(self.BRIDGE_START), 1)
        self.assertEqual(self.target_bytes.count(self.BRIDGE_END), 1)
        stripped = re.sub(
            self.BRIDGE_START + br".*?" + self.BRIDGE_END + br"\n\n",
            b"",
            self.target_bytes,
            count=1,
            flags=re.DOTALL,
        )
        self.assertEqual(stripped, self.source_bytes)

    def test_original_style_content_sha_is_unchanged(self):
        source_style = re.search(br"<style>(.*?)</style>", self.source_bytes, re.DOTALL).group(1)
        target_style = re.search(br"<style>(.*?)</style>", self.target_bytes, re.DOTALL).group(1)
        self.assertEqual(
            hashlib.sha256(target_style).hexdigest(),
            hashlib.sha256(source_style).hexdigest(),
        )

    def test_bridge_uses_only_readonly_bootstrap_for_menu_business_data(self):
        self.assertIn("DISH_DB", self.source_bytes.decode("utf-8"))
        self.assertNotIn("DISH_DB", self.bridge)
        self.assertIn("/api/family-menu/bootstrap", self.bridge)
        self.assertNotIn("/api/tomorrow", self.bridge)
        for endpoint in (
            "/api/tomorrow/add", "/api/tomorrow/remove", "/api/tomorrow/replace",
            "/api/tomorrow/confirm", "/api/tomorrow/ai-fill", "/api/tomorrow/repair",
        ):
            self.assertNotIn(endpoint, self.bridge)
        self.assertEqual(self.bridge.count("fetch("), 1)

    def test_bridge_binds_real_menu_fields_and_blocks_menu_writes_in_capture_phase(self):
        self.assertIn("dataset.menuId", self.bridge)
        self.assertIn("dataset.menuItemId", self.bridge)
        self.assertIn("dataset.dishId", self.bridge)
        self.assertIn("dish.image", self.bridge)
        self.assertIn("menu.diners_count", self.bridge)
        self.assertIn("menu.meal_notes", self.bridge)
        self.assertIn("nextMeal?.note", self.bridge)
        self.assertIn("availability", self.bridge)
        self.assertIn("document.addEventListener('click', stopWrite, true)", self.bridge)
        self.assertIn("document.addEventListener('input', stopWrite, true)", self.bridge)
        self.assertIn("stopImmediatePropagation()", self.bridge)
        self.assertIn("当前为只读预览", self.bridge)
        for selector in (
            ".stepper button", ".meal-skip", ".op-btn.fav", ".op-btn.shuf",
            ".op-btn.find", ".op-btn.del", ".foot-btn[data-act]",
            ".confirm-meal-btn", "#regenBtn", ".add-meal-btn",
        ):
            self.assertIn(selector, self.bridge)

    def test_target_does_not_reference_superseded_external_assets(self):
        self.assertNotIn('/family-menu/styles.css', self.html)
        self.assertNotIn('/family-menu/app.js', self.html)
        self.assertIn("viewport-fit=cover", self.html)

    def test_server_serves_frozen_source_plus_bridge_without_rewriting_it(self):
        rendered = app.render_family_menu_readonly("worker", "hongkong")
        self.assertEqual(rendered, self.html)

    def test_http_routes_serve_inline_source_ui_and_bridge(self):
        html = self._get("/tomorrow", role="owner")
        css = self._get("/family-menu/styles.css")
        js = self._get("/family-menu/app.js")
        self.assertEqual(html["status"], 200)
        self.assertEqual(css["status"], 200)
        self.assertEqual(js["status"], 200)
        self.assertIn("text/html", html["headers"]["Content-Type"])
        self.assertIn("text/css", css["headers"]["Content-Type"])
        self.assertIn("javascript", js["headers"]["Content-Type"])
        self.assertIn(b"<style>", html["body"])
        self.assertIn(self.BRIDGE_START, html["body"])
        self.assertIn(b"/api/family-menu/bootstrap", html["body"])
        self.assertNotIn(b"/family-menu/styles.css", html["body"])
        self.assertNotIn(b"/family-menu/app.js", html["body"])

    @staticmethod
    def _get(path, role="owner"):
        handler = object.__new__(app.AppHandler)
        handler.path = path
        handler.headers = {"Cookie": "loc=shenzhen"}
        handler.request_role = lambda: role
        handler.wfile = io.BytesIO()
        result = {"headers": {}}
        handler.send_response = lambda status: result.update(status=status)
        handler.send_header = lambda name, value: result["headers"].__setitem__(name, value)
        handler.end_headers = lambda: None
        handler.send_session_refresh_header = lambda: None
        app.AppHandler.do_GET(handler)
        result["body"] = handler.wfile.getvalue()
        return result


class LegacySchemaBootstrapTests(unittest.TestCase):
    def _create_legacy_db(self, path):
        with patch.object(db, "DB_PATH", path):
            db.init_db()
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "INSERT INTO dishes(id,name_cn,name_en,image,is_active) VALUES(?,?,?,?,1)",
                ("dish_legacy", "真实旧库菜", "Legacy Dish", "legacy-dish.jpg"),
            )
            connection.executemany(
                "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES(?,?,?)",
                (
                    ("beef", "牛肉", "Beef"),
                    ("any_available_vegetable", "任意蔬菜", "Any Vegetable"),
                ),
            )
            connection.executemany(
                "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES(?,?,1)",
                (
                    ("dish_legacy", "beef"),
                    ("dish_legacy", "any_available_vegetable"),
                ),
            )
            connection.execute(
                "INSERT INTO current_pantry(location,ingredient_id,status,is_active) "
                "VALUES('shenzhen','beef','available',1)"
            )
            connection.execute(
                "INSERT INTO menus(id,date,location,status,diners_count,diners,meal_notes) "
                "VALUES(187,'2026-08-18','shenzhen','draft',1,'[\"vv\"]','{\"lunch\":\"少油\"}')"
            )
            connection.execute(
                "INSERT INTO menu_items(id,menu_id,dish_id,meal_type,sort_order) "
                "VALUES(3146,187,'dish_legacy','lunch',1)"
            )
            connection.execute("DROP TABLE ingredient_classifications")
            connection.commit()
        finally:
            connection.close()

    def test_bootstrap_legacy_schema_returns_real_ids_and_conservative_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.db")
            self._create_legacy_db(path)
            inventory._availability_cache.clear()
            with patch.object(db, "DB_PATH", path):
                result = app.build_family_menu_bootstrap(
                    "shenzhen", "owner", now=datetime(2026, 8, 18, 11, 0)
                )

            menu = result["days"][0]["menu"]
            dish = menu["meals"]["lunch"][0]
            availability = menu["availability"]["dish_legacy"]
            self.assertEqual(menu["menu_id"], 187)
            self.assertEqual(menu["diners"], ["vv"])
            self.assertEqual(menu["meal_notes"], {"lunch": "少油"})
            self.assertEqual(dish["menu_item_id"], 3146)
            self.assertEqual(dish["dish_id"], "dish_legacy")
            self.assertEqual(dish["name_cn"], "真实旧库菜")
            self.assertEqual(dish["image"], "/photos/legacy-dish.jpg")
            self.assertEqual(availability["status"], "incomplete")
            self.assertFalse(availability["data_complete"])
            self.assertEqual(
                [item["ingredient_id"] for item in availability["available_required"]],
                ["beef"],
            )
            self.assertEqual(
                [item["ingredient_id"] for item in availability["unknown_required"]],
                ["any_available_vegetable"],
            )
            self.assertEqual(availability["missing_required"], [])

    def test_non_bootstrap_availability_remains_strict_on_legacy_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.db")
            self._create_legacy_db(path)
            inventory._availability_cache.clear()
            with patch.object(db, "DB_PATH", path), self.assertRaisesRegex(
                sqlite3.OperationalError,
                "no such table: ingredient_classifications",
            ):
                menu_service.get_menu_with_dishes("2026-08-18", "shenzhen")

    def test_legacy_safe_mode_does_not_swallow_other_operational_errors(self):
        class LockedConnection:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("database is locked")

        with inventory.legacy_schema_safe_availability(), self.assertRaisesRegex(
            sqlite3.OperationalError,
            "database is locked",
        ):
            inventory._ingredient_classes(LockedConnection(), {"beef"})

    def test_bootstrap_image_urls_are_idempotent_and_output_only(self):
        cases = (
            (None, None),
            ("", ""),
            ("dish.jpg", "/photos/dish.jpg"),
            ("/photos/dish.jpg", "/photos/dish.jpg"),
            ("http://images.example/dish.jpg", "http://images.example/dish.jpg"),
            ("https://images.example/dish.jpg", "https://images.example/dish.jpg"),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                raw_menu = menu_for("2026-08-18", "shenzhen")
                raw_menu["meals"]["breakfast"][0]["image"] = source
                with patch.object(app, "get_menu_with_dishes", return_value=raw_menu):
                    result = app.build_family_menu_bootstrap(
                        "shenzhen", "owner", now=datetime(2026, 8, 18, 8, 0)
                    )
                image = result["days"][0]["menu"]["meals"]["breakfast"][0]["image"]
                self.assertEqual(image, expected)
                self.assertEqual(raw_menu["meals"]["breakfast"][0]["image"], source)


if __name__ == "__main__":
    unittest.main()
