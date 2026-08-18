#!/usr/bin/env python3
"""Phase 1 final Family UI read-only bootstrap and asset contracts."""

import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import app
import db
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

                # The existing default retains its historical audit side effect.
                menu_service.get_menu_with_dishes("2026-08-18", "shenzhen")
                check = sqlite3.connect(db_path)
                try:
                    self.assertEqual(check.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
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
    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(os.path.dirname(__file__), "public", "family-menu")
        with open(os.path.join(cls.root, "index.html"), encoding="utf-8") as handle:
            cls.html = handle.read()
        with open(os.path.join(cls.root, "app.js"), encoding="utf-8") as handle:
            cls.js = handle.read()
        with open(os.path.join(cls.root, "styles.css"), encoding="utf-8") as handle:
            cls.css = handle.read()

    def test_final_ui_uses_only_readonly_bootstrap_for_business_data(self):
        sources = self.html + self.js
        self.assertIn("/api/family-menu/bootstrap", self.js)
        self.assertNotIn("DISH_DB", sources)
        self.assertNotIn("localStorage", sources)
        for endpoint in (
            "/api/tomorrow/add", "/api/tomorrow/remove", "/api/tomorrow/replace",
            "/api/tomorrow/confirm", "/api/tomorrow/ai-fill", "/api/tomorrow/repair",
        ):
            self.assertNotIn(endpoint, sources)
        self.assertEqual(self.js.count("fetch("), 1)

    def test_ui_renders_real_identifiers_and_mobile_contract(self):
        self.assertIn('data-menu-id=', self.js)
        self.assertIn('data-menu-item-id=', self.js)
        self.assertIn('data-dish-id=', self.js)
        self.assertIn("dish.image", self.js)
        self.assertIn("menu.diners_count", self.js)
        self.assertIn("menu.meal_notes", self.js)
        self.assertIn("menu.status", self.js)
        self.assertIn("@media (max-width: 390px)", self.css)
        self.assertIn("viewport-fit=cover", self.html)

    def test_server_injects_role_and_location_without_touching_source(self):
        rendered = app.render_family_menu_readonly("worker", "hongkong")
        self.assertIn('data-role="worker"', rendered)
        self.assertIn('data-location="hongkong"', rendered)
        self.assertNotIn("__ROLE__", rendered)
        self.assertIn("__ROLE__", self.html)

    def test_http_routes_serve_styled_page_css_and_javascript(self):
        html = self._get("/tomorrow", role="owner")
        css = self._get("/family-menu/styles.css")
        js = self._get("/family-menu/app.js")
        self.assertEqual(html["status"], 200)
        self.assertEqual(css["status"], 200)
        self.assertEqual(js["status"], 200)
        self.assertIn("text/html", html["headers"]["Content-Type"])
        self.assertIn("text/css", css["headers"]["Content-Type"])
        self.assertIn("javascript", js["headers"]["Content-Type"])
        self.assertIn(b"/family-menu/styles.css", html["body"])
        self.assertIn(b".meal-grid", css["body"])
        self.assertIn(b"/api/family-menu/bootstrap", js["body"])

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


if __name__ == "__main__":
    unittest.main()
