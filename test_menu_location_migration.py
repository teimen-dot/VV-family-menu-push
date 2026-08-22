import os
import sqlite3
import tempfile
import unittest

from migrate_menu_location_unique import check, migrate


LEGACY_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE menus (
 id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL UNIQUE,
 location TEXT NOT NULL, status TEXT DEFAULT 'draft', auto_confirmed INTEGER DEFAULT 0,
 confirmed_at TEXT, pushed_at TEXT, diners_count INTEGER DEFAULT 4,
 diners TEXT DEFAULT '[]', notes_zh TEXT, notes_en TEXT,
 created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')),
 inventory_snapshot_id INTEGER, meal_mode TEXT DEFAULT 'daily', banquet_total_diners INTEGER,
 push_status TEXT DEFAULT 'not_sent', push_error TEXT, confirmed_revision TEXT,
 meal_notes TEXT DEFAULT '{}'
);
CREATE TABLE menu_items (id INTEGER PRIMARY KEY,menu_id INTEGER NOT NULL,dish_id TEXT,
 meal_type TEXT NOT NULL,FOREIGN KEY(menu_id) REFERENCES menus(id));
CREATE TABLE menu_meal_settings (menu_id INTEGER,meal_type TEXT,
 PRIMARY KEY(menu_id,meal_type),FOREIGN KEY(menu_id) REFERENCES menus(id));
CREATE TABLE selections (id INTEGER PRIMARY KEY,menu_id INTEGER,
 FOREIGN KEY(menu_id) REFERENCES menus(id));
CREATE TABLE push_logs (id INTEGER PRIMARY KEY,menu_id INTEGER,
 FOREIGN KEY(menu_id) REFERENCES menus(id));
CREATE TABLE current_pantry (id INTEGER PRIMARY KEY,location TEXT,ingredient_id TEXT);
CREATE TABLE dishes (id TEXT PRIMARY KEY,name_cn TEXT);
CREATE TABLE ingredients (ingredient_id TEXT PRIMARY KEY,name_cn TEXT);
CREATE TABLE dish_ingredients (id INTEGER PRIMARY KEY,dish_id TEXT,ingredient_id TEXT);
"""


class MenuLocationMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "legacy.db")
        conn = sqlite3.connect(self.path)
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO menus(id,date,location,status,diners_count,notes_zh) "
            "VALUES(7,'2026-08-22','shenzhen','confirmed',3,'保留')"
        )
        conn.execute(
            "INSERT INTO menu_items(id,menu_id,dish_id,meal_type) "
            "VALUES(11,7,'dish_a','dinner')"
        )
        conn.execute("INSERT INTO dishes VALUES('dish_a','深圳菜')")
        conn.execute("INSERT INTO ingredients VALUES('beef','牛肉')")
        conn.execute("INSERT INTO current_pantry VALUES(1,'shenzhen','beef')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_forward_preserves_shenzhen_and_allows_two_kitchens(self):
        before = check(self.path)["fingerprint"]
        result = migrate(self.path)
        self.assertTrue(result["changed"])
        self.assertEqual(result["fingerprint"], before)
        self.assertEqual(migrate(self.path)["changed"], False)

        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO menus(date,location,diners_count) "
                "VALUES('2026-08-22','hongkong',3)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO menus(date,location) VALUES('2026-08-22','shenzhen')"
                )
            conn.rollback()
            row = conn.execute(
                "SELECT id,status,diners_count,notes_zh FROM menus WHERE id=7"
            ).fetchone()
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()
        self.assertEqual(row, (7, "confirmed", 3, "保留"))
        self.assertEqual(fk, [])

    def test_snapshot_restore_is_exact_rollback(self):
        with open(self.path, "rb") as handle:
            snapshot = handle.read()
        migrate(self.path)
        with open(self.path, "wb") as handle:
            handle.write(snapshot)
        self.assertEqual(check(self.path)["state"], "legacy")
        with open(self.path, "rb") as handle:
            restored = handle.read()
        self.assertEqual(restored, snapshot)


if __name__ == "__main__":
    unittest.main()
