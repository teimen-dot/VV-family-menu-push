#!/usr/bin/env python3
"""Phase 2 writable DB, retired purchase surface, and audit regressions."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import app
import db
import inventory
import menu_service


class Phase2WritableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "phase2.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def _event_types(self):
        conn = db.get_db()
        try:
            return [row["event_type"] for row in conn.execute(
                "SELECT event_type FROM events ORDER BY id"
            )]
        finally:
            conn.close()

    def _seed_menu(self):
        tomorrow = app.get_tomorrow_date()
        conn = db.get_db()
        try:
            conn.execute(
                "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES "
                "('phase2_ingredient','测试食材','Test Ingredient')"
            )
            for dish_id, name in (("dish_phase2_a", "测试菜A"), ("dish_phase2_b", "测试菜B")):
                conn.execute(
                    "INSERT INTO dishes(id,name_cn,name_en,is_active,meal_tags) "
                    "VALUES(?,?,?,1,'[\"dinner\"]')",
                    (dish_id, name, name),
                )
            menu_id = conn.execute(
                "INSERT INTO menus(date,location,status,diners_count,diners) "
                "VALUES(?,'shenzhen','draft',4,'[]')",
                (tomorrow,),
            ).lastrowid
            conn.execute(
                "INSERT INTO menu_items(menu_id,dish_id,custom_name,meal_type) "
                "VALUES(?,NULL,NULL,'breakfast')",
                (menu_id,),
            )
            conn.commit()
            return tomorrow, menu_id
        finally:
            conn.close()

    def test_menu_get_and_refresh_paths_do_not_append_events(self):
        tomorrow, _ = self._seed_menu()
        self.assertEqual(self._event_types(), [])

        menu_service.get_menu_with_dishes(tomorrow, "shenzhen")
        menu_service.get_menu_with_dishes(tomorrow, "shenzhen", record_filter_events=True)
        app.build_family_menu_bootstrap(
            "shenzhen", "owner", now=datetime.fromisoformat(f"{tomorrow}T11:00:00+08:00")
        )

        handler = object.__new__(app.AppHandler)
        handler.path = "/api/tomorrow"
        handler.headers = {"Cookie": "loc=shenzhen"}
        handler.request_role = lambda: "owner"
        handler.send_json = lambda payload, status=200: None
        with patch.object(app, "ensure_tomorrow_menu") as ensure:
            app.AppHandler.do_GET(handler)
        ensure.assert_not_called()

        self.assertEqual(self._event_types(), [])

    def test_real_menu_and_inventory_writes_remain_audited(self):
        _, menu_id = self._seed_menu()

        self.assertTrue(app.update_menu_diners(menu_id, ["vv"]))
        self.assertTrue(menu_service.add_dish_to_menu(menu_id, "dish_phase2_a", "dinner"))
        conn = db.get_db()
        try:
            item_id = conn.execute(
                "SELECT id FROM menu_items WHERE menu_id=? AND dish_id='dish_phase2_a'",
                (menu_id,),
            ).fetchone()["id"]
        finally:
            conn.close()
        self.assertTrue(menu_service.replace_dish_in_menu(
            menu_id, item_id, "dish_phase2_b"
        )[0])
        self.assertTrue(menu_service.remove_dish_from_menu(menu_id, item_id)[0])
        self.assertTrue(inventory.add_ingredient_to_pantry(
            "shenzhen", "phase2_ingredient", submitted_by="phase2-test"
        )["ok"])

        self.assertTrue({
            "diners_updated", "dish_added", "dish_replaced", "dish_removed",
            "pantry_item_added",
        }.issubset(set(self._event_types())))

    def test_purchase_runtime_is_gone_but_legacy_table_and_pantry_write_remain(self):
        for name in (
            "create_purchase_requests", "get_purchase_requests", "update_purchase_status",
            "notify_purchase_requests", "process_selection_shortages",
        ):
            self.assertFalse(hasattr(inventory, name), name)
        self.assertFalse(hasattr(app, "get_menu_purchase_requests"))
        self.assertNotIn("/api/purchase/update", app.OWNER_ONLY_POST_PATHS)
        self.assertNotIn("/api/purchase-requests", app.AppHandler.do_GET.__code__.co_consts)
        self.assertNotIn("/api/purchase/update", app.AppHandler.do_POST.__code__.co_consts)

        conn = db.get_db()
        try:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='purchase_requests'"
            ).fetchone())
            conn.execute(
                "INSERT INTO ingredients(ingredient_id,name_cn) VALUES('legacy_purchase_ing','旧采购食材')"
            )
            conn.execute(
                "INSERT INTO purchase_requests(menu_date,location,ingredient_id,status) "
                "VALUES('2099-01-01','shenzhen','legacy_purchase_ing','needed')"
            )
            conn.commit()
        finally:
            conn.close()

        result = inventory.save_pantry_changes(
            "shenzhen",
            [{"ingredient_id": "legacy_purchase_ing", "status": "available"}],
            submitted_by="phase2-test",
        )
        self.assertEqual(result["auto_purchased"], 0)
        conn = db.get_db()
        try:
            status = conn.execute(
                "SELECT status FROM purchase_requests WHERE ingredient_id='legacy_purchase_ing'"
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(status, "needed")

    def test_status_update_commits_version_before_audit_connection(self):
        conn = db.get_db()
        try:
            conn.execute(
                "INSERT INTO ingredients(ingredient_id,name_cn) "
                "VALUES('batch3_status_ing','三棒状态食材')"
            )
            conn.execute(
                "INSERT INTO current_pantry(location,ingredient_id,status,is_active) "
                "VALUES('shenzhen','batch3_status_ing','available',1)"
            )
            conn.commit()
        finally:
            conn.close()

        result = inventory.update_ingredient_status(
            "shenzhen", "batch3_status_ing", "expiring", submitted_by="batch3-test"
        )

        self.assertTrue(result["ok"])
        conn = db.get_db()
        try:
            status = conn.execute(
                "SELECT status FROM current_pantry "
                "WHERE location='shenzhen' AND ingredient_id='batch3_status_ing'"
            ).fetchone()["status"]
            event = conn.execute(
                "SELECT event_type FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()["event_type"]
        finally:
            conn.close()
        self.assertEqual(status, "expiring")
        self.assertEqual(event, "pantry_status_updated")


if __name__ == "__main__":
    unittest.main()
