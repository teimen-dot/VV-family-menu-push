import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


WORK_DIR = Path(__file__).resolve().parent
BASELINE_DB = WORK_DIR / "family_menu_test.db"
os.environ["FAMILY_MENU_DB_PATH"] = str(BASELINE_DB)

import app


class SmartReplaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="smart-replace-test-")
        self.db_path = Path(self.temp_dir.name) / "family_menu.db"
        shutil.copyfile(BASELINE_DB, self.db_path)
        conn = self.connect()
        conn.execute(
            "INSERT INTO menus(date,location,status) VALUES('2099-12-31','shenzhen','draft')"
        )
        self.menu_id = conn.execute(
            "SELECT id FROM menus WHERE date='2099-12-31'"
        ).fetchone()["id"]
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def add_menu_item(self, dish_id, meal_type):
        conn = self.connect()
        cursor = conn.execute(
            "INSERT INTO menu_items(menu_id,dish_id,meal_type) VALUES(?,?,?)",
            (self.menu_id, dish_id, meal_type),
        )
        conn.commit()
        item_id = cursor.lastrowid
        conn.close()
        return item_id

    def replace_in_test_db(self, menu_id, menu_item_id, new_dish_id):
        conn = self.connect()
        conn.execute(
            "UPDATE menu_items SET dish_id=? WHERE menu_id=? AND id=?",
            (new_dish_id, menu_id, menu_item_id),
        )
        conn.commit()
        conn.close()
        return True, "已替换"

    @staticmethod
    def all_available(dish_ids, _location):
        return {dish_id: {"status": "available"} for dish_id in dish_ids}

    def cycle(self, start_dish_id, meal_type, count, availability_check=None):
        item_id = self.add_menu_item(start_dish_id, meal_type)
        replacements = []
        availability_check = availability_check or self.all_available
        with patch.object(app, "get_db", side_effect=self.connect), patch.object(
            app, "check_dishes_availability_batch", side_effect=availability_check
        ), patch.object(
            app, "replace_dish_in_menu", side_effect=self.replace_in_test_db
        ):
            for _ in range(count):
                ok, message, replacement_id = app.smart_replace_menu_item(
                    self.menu_id, item_id, "shenzhen"
                )
                self.assertTrue(ok, message)
                replacements.append(replacement_id)
        return replacements

    def dish_rows(self, dish_ids):
        conn = self.connect()
        placeholders = ",".join("?" for _ in dish_ids)
        rows = conn.execute(
            f"SELECT id,category_id,carb_type,protein_types FROM dishes "
            f"WHERE id IN ({placeholders})",
            dish_ids,
        ).fetchall()
        conn.close()
        return rows

    def availability_for_primary_proteins(self, available_primary_proteins):
        def check(dish_ids, _location):
            rows = self.dish_rows(dish_ids)
            primary_by_id = {
                row["id"]: (json.loads(row["protein_types"] or "[]") or [None])[0]
                for row in rows
            }
            return {
                dish_id: {
                    "status": (
                        "available"
                        if primary_by_id.get(dish_id) in available_primary_proteins
                        else "missing"
                    )
                }
                for dish_id in dish_ids
            }

        return check

    def test_coarse_grains_stay_coarse_and_cycle_after_exhaustion(self):
        conn = self.connect()
        pool_size = conn.execute(
            "SELECT COUNT(*) n FROM dishes WHERE is_active=1 "
            "AND category_id='staple_carb' AND carb_type='coarse_grain' "
            "AND meal_tags LIKE '%breakfast%'"
        ).fetchone()["n"]
        conn.close()
        replacements = self.cycle("dish_0207", "breakfast", pool_size + 2)
        rows = self.dish_rows(replacements)
        self.assertEqual({"coarse_grain"}, {row["carb_type"] for row in rows})
        self.assertEqual(pool_size, len(set(replacements[:pool_size])))
        self.assertEqual(replacements[0:2], replacements[pool_size:pool_size + 2])

    def test_porridge_and_dim_sum_never_cross_subtypes(self):
        for dish_id, subtype in (("dish_0124", "porridge"), ("dish_0203", "dim_sum")):
            replacements = self.cycle(dish_id, "breakfast", 5)
            self.assertEqual(
                {subtype},
                {row["carb_type"] for row in self.dish_rows(replacements)},
            )

    def test_egg_and_tofu_use_primary_protein(self):
        for dish_id, primary in (("dish_0131", "egg"), ("dish_0148", "tofu")):
            replacements = self.cycle(dish_id, "breakfast", 4)
            observed = {
                (json.loads(row["protein_types"] or "[]") or [None])[0]
                for row in self.dish_rows(replacements)
            }
            self.assertEqual({primary}, observed)

    def test_main_protein_prefers_available_same_primary_protein(self):
        replacements = self.cycle(
            "dish_0023",
            "lunch",
            1,
            self.availability_for_primary_proteins({"chicken", "beef"}),
        )
        row = self.dish_rows(replacements)[0]
        self.assertEqual("chicken", json.loads(row["protein_types"])[0])

    def test_main_protein_falls_back_when_same_primary_is_unavailable(self):
        replacements = self.cycle(
            "dish_0023",
            "lunch",
            1,
            self.availability_for_primary_proteins({"beef"}),
        )
        row = self.dish_rows(replacements)[0]
        self.assertEqual("beef", json.loads(row["protein_types"])[0])


class DishPickerTests(unittest.TestCase):
    def test_default_picker_only_uses_recommend_and_search_keeps_full_catalog(self):
        source = Path(app.__file__).read_text(encoding="utf-8")
        active = source[source.index("def render_meal_plan_reference"):source.index("def render_tomorrow(")]
        loader = active[active.index("async function loadDishPicker()"):active.index("async function doDishSearch(q)")]
        search = active[active.index("async function doDishSearch(q)"):]
        self.assertIn("/api/dishes/recommend", loader)
        self.assertNotIn("requestJSON('/api/dishes')", loader)
        self.assertIn("/api/dishes?search=", search)

        search_results = app.get_all_dishes(search="淮山")
        self.assertGreater(len(search_results), 1)
        self.assertIn("soup", {dish["category_id"] for dish in search_results})
        self.assertIn("staple_carb", {dish["category_id"] for dish in search_results})

    def test_recommendations_keep_meal_and_exclude_missing_or_incomplete(self):
        statuses = ("available", "almost_available", "missing", "incomplete")

        def availability(dish_ids, _location):
            return {
                dish_id: {
                    "status": statuses[index % len(statuses)],
                    "missing_required": [],
                }
                for index, dish_id in enumerate(dish_ids)
            }

        with patch.object(app, "check_dishes_availability_batch", side_effect=availability):
            result = app.get_dish_recommendations(
                "breakfast", "dish_0207", "staple_carb", "shenzhen"
            )

        recommended = result["available"] + result["almost_available"]
        self.assertTrue(recommended)
        self.assertTrue(
            all(item["availability"] in {"available", "almost_available"} for item in recommended)
        )
        conn = sqlite3.connect(BASELINE_DB)
        meal_tags = {
            row[0]: json.loads(row[1] or "[]")
            for row in conn.execute("SELECT id,meal_tags FROM dishes")
        }
        conn.close()
        self.assertTrue(all("breakfast" in meal_tags[item["id"]] for item in recommended))


if __name__ == "__main__":
    unittest.main()
