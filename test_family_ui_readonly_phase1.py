#!/usr/bin/env python3
"""Phase 1 final Family UI read-only bootstrap and asset contracts."""

import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import app


def menu_for(date_str, location, with_dishes=True):
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

        def fake_get(date_str, location):
            calls.append((date_str, location))
            return menu_for(date_str, location)

        with patch.object(app, "get_menu_with_dishes", side_effect=fake_get):
            result = app.build_family_menu_bootstrap("hongkong", "worker", now=now)

        expected_dates = [(now.date() + timedelta(days=offset)).isoformat() for offset in range(4)]
        self.assertEqual([day["date"] for day in result["days"]], expected_dates)
        self.assertEqual(calls, [(date_str, "hongkong") for date_str in expected_dates])
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
             patch.object(app, "generate_and_store_menu") as generate:
            result = app.build_family_menu_bootstrap(
                "shenzhen", "owner", now=datetime(2026, 8, 18, 8, 0)
            )
        generate.assert_not_called()
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


if __name__ == "__main__":
    unittest.main()
