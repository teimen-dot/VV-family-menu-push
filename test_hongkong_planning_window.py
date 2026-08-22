import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import db
import menu_service


class HongKongPlanningWindowTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "window.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()
        menu_service.invalidate_catalog_cache()

    def tearDown(self):
        menu_service.invalidate_catalog_cache()
        self.db_patch.stop()
        self.tempdir.cleanup()

    def test_hongkong_window_is_idempotent_and_defaults_to_three(self):
        start = date(2098, 1, 10)
        conn = db.get_db()
        try:
            conn.execute("DELETE FROM menus WHERE date BETWEEN ? AND ?", (
                start.isoformat(), date(2098, 1, 13).isoformat(),
            ))
            conn.commit()
        finally:
            conn.close()

        first = menu_service.ensure_planning_window(
            "hongkong", start, days=4, default_diners_count=3,
        )
        second = menu_service.ensure_planning_window(
            "hongkong", start, days=4, default_diners_count=3,
        )

        self.assertEqual(first["errors"], [])
        self.assertEqual(len(first["created"]), 4)
        self.assertEqual(len(second["created"]), 0)
        self.assertEqual(
            [row["menu_id"] for row in first["created"]],
            [row["menu_id"] for row in second["existing"]],
        )
        conn = db.get_db()
        try:
            rows = conn.execute(
                "SELECT location,diners_count FROM menus "
                "WHERE date BETWEEN ? AND ? ORDER BY date",
                (start.isoformat(), date(2098, 1, 13).isoformat()),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([(row["location"], row["diners_count"]) for row in rows], [
            ("hongkong", 3), ("hongkong", 3),
            ("hongkong", 3), ("hongkong", 3),
        ])

    def test_existing_shenzhen_and_hongkong_rows_are_not_overwritten(self):
        start = date(2098, 2, 1)
        conn = db.get_db()
        try:
            conn.execute(
                "INSERT INTO menus(date,location,status,diners_count,notes_zh) "
                "VALUES(?, 'shenzhen', 'confirmed', 2, '深圳保留')",
                (start.isoformat(),),
            )
            conn.execute(
                "INSERT INTO menus(date,location,status,diners_count,notes_zh) "
                "VALUES(?, 'hongkong', 'confirmed', 5, '香港保留')",
                (start.isoformat(),),
            )
            conn.commit()
            before = [tuple(row) for row in conn.execute(
                "SELECT id,date,location,status,diners_count,notes_zh FROM menus "
                "WHERE date=? ORDER BY location", (start.isoformat(),)
            ).fetchall()]
        finally:
            conn.close()

        report = menu_service.ensure_planning_window(
            "hongkong", start, days=4, default_diners_count=3,
        )
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["created"]), 3)
        conn = db.get_db()
        try:
            after = [tuple(row) for row in conn.execute(
                "SELECT id,date,location,status,diners_count,notes_zh FROM menus "
                "WHERE date=? ORDER BY location", (start.isoformat(),)
            ).fetchall()]
        finally:
            conn.close()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
