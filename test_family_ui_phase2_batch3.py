#!/usr/bin/env python3
"""Phase 2 Batch 3 persistence regressions for newly opened write surfaces."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import app
import db
import menu_service
import push_service


class Phase2Batch3PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "batch3.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()
        conn = db.get_db()
        try:
            conn.execute(
                "INSERT INTO categories(id,label_cn,label_en,sort_order,active) "
                "VALUES('protein_main','主菜','Main',1,1)"
            )
            self.menu_id = conn.execute(
                "INSERT INTO menus(date,location,status,diners_count,diners) "
                "VALUES('2099-02-03','shenzhen','draft',4,'[\"a\",\"b\",\"c\"]')"
            ).lastrowid
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_diners_count_is_persisted_without_touching_member_list(self):
        ok, message = menu_service.update_menu_diners_count(
            self.menu_id, 3, location="shenzhen"
        )

        self.assertTrue(ok, message)
        conn = db.get_db()
        try:
            row = conn.execute(
                "SELECT diners_count, diners FROM menus WHERE id=?", (self.menu_id,)
            ).fetchone()
            event = conn.execute(
                "SELECT event_type FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["diners_count"], 3)
        self.assertEqual(json.loads(row["diners"]), ["a", "b", "c"])
        self.assertEqual(event["event_type"], "diners_count_updated")

    def test_cancel_restore_state_round_trips_through_menu_reader(self):
        ok, message = menu_service.set_menu_meal_skipped(
            self.menu_id, "breakfast", True
        )
        self.assertTrue(ok, message)
        cancelled = menu_service.get_menu_with_dishes("2099-02-03", "shenzhen")
        self.assertTrue(cancelled["meal_settings"]["breakfast"]["is_skipped"])

        ok, message = menu_service.set_menu_meal_skipped(
            self.menu_id, "breakfast", False
        )
        self.assertTrue(ok, message)
        restored = menu_service.get_menu_with_dishes("2099-02-03", "shenzhen")
        self.assertFalse(restored["meal_settings"]["breakfast"]["is_skipped"])

        conn = db.get_db()
        try:
            events = [row["event_type"] for row in conn.execute(
                "SELECT event_type FROM events ORDER BY id"
            )]
        finally:
            conn.close()
        self.assertEqual(events, ["meal_cancelled", "meal_restored"])

    def test_dish_create_edit_favorite_and_ingredients_are_persistent(self):
        create_payload = {
            "name_cn": "三棒测试菜",
            "name_en": "Batch Three Dish",
            "category_id": "protein_main",
            "meal_tags": ["lunch", "dinner"],
            "banquet": True,
            "protein_types": ["鸡肉"],
            "vegetables": ["菜心"],
            "cooking_methods": ["炒"],
            "custom_tags": ["manual"],
            "ingredients": ["鸡肉", "菜心"],
            "taste": "清淡",
        }

        ok, created = app.save_family_dish(create_payload)
        self.assertTrue(ok, created)
        dish_id = created["id"]
        ok, favorite = app.toggle_family_dish_favorite(dish_id)
        self.assertTrue(ok, favorite)
        self.assertTrue(favorite["favorite"])

        edit_payload = dict(create_payload)
        edit_payload.update({
            "name_cn": "三棒测试菜已编辑",
            "name_en": "Batch Three Dish Edited",
            "banquet": False,
            "custom_tags": ["manual", "favorite"],
            "ingredients": ["鸡肉", "生菜"],
        })
        ok, edited = app.save_family_dish(edit_payload, dish_id=dish_id)
        self.assertTrue(ok, edited)

        conn = db.get_db()
        try:
            dish = conn.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)).fetchone()
            ingredients = [row["name_cn"] for row in conn.execute(
                "SELECT i.name_cn FROM dish_ingredients di "
                "JOIN ingredients i ON i.ingredient_id=di.ingredient_id "
                "WHERE di.dish_id=? ORDER BY i.name_cn",
                (dish_id,),
            )]
            events = [row["event_type"] for row in conn.execute(
                "SELECT event_type FROM events ORDER BY id"
            )]
        finally:
            conn.close()

        self.assertEqual(dish["name_cn"], "三棒测试菜已编辑")
        self.assertEqual(dish["banquet"], 0)
        self.assertEqual(json.loads(dish["meal_tags"]), ["lunch", "dinner"])
        self.assertEqual(json.loads(dish["custom_tags"]), ["manual", "favorite"])
        self.assertEqual(ingredients, ["生菜", "鸡肉"])
        self.assertEqual(
            events,
            ["dish_added", "dish_favorite_updated", "dish_edited"],
        )


class Phase2Batch3StaticContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "public", "family-menu", "index.html")
        with open(path, encoding="utf-8") as handle:
            cls.html = handle.read()

    def test_all_menu_write_controls_target_real_endpoints(self):
        for endpoint in (
            "/api/menu/diners-count", "/api/tomorrow/meal-note",
            "/api/tomorrow/cycle-replace", "/api/tomorrow/replace",
            "/api/tomorrow/add", "/api/tomorrow/remove",
            "/api/tomorrow/ai-fill", "/api/tomorrow/repair",
            "/api/tomorrow/confirm", "/api/tomorrow/revert",
            "/api/tomorrow/meal-state", "/api/tomorrow/drinks",
        ):
            self.assertIn(endpoint, self.html)
        self.assertNotIn("stopPhase2Write", self.html)
        self.assertNotIn("stopWrite", self.html)

    def test_confirm_push_hook_keeps_existing_production_gate(self):
        with patch.dict(os.environ, {
            "APP_ENV": "development", "PUSH_ENABLED": "true", "PUSH_ON_CONFIRM": "true",
        }, clear=False):
            self.assertFalse(push_service.push_on_confirm_is_enabled())
        with patch.dict(os.environ, {
            "APP_ENV": "production", "PUSH_ENABLED": "true", "PUSH_ON_CONFIRM": "true",
        }, clear=False):
            self.assertTrue(push_service.push_on_confirm_is_enabled())


if __name__ == "__main__":
    unittest.main()
