import os
import tempfile
import unittest
from unittest.mock import patch

import db
from ingredient_resolution import (
    backfill_aliases, ensure_resolution_schema, list_pending,
    merge_ingredient, resolve_ingredient_input,
)


class IngredientResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.path_patch = patch.object(db, "DB_PATH", self.db_path)
        self.path_patch.start()
        db.init_db()
        self.conn = db.get_db()
        self.conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES(?,?,?,?)",
            ("沙拉菜", "沙拉菜", "Salad Greens", '["salad leaves","mixed salad greens"]'),
        )
        ensure_resolution_schema(self.conn)
        backfill_aliases(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_salad_names_and_aliases_share_one_id(self):
        for value in ("沙拉菜", "salad", "Salad Greens", "salad leaves", "mixed salad greens"):
            result = resolve_ingredient_input(self.conn, value)
            self.assertEqual(result["ingredient_id"], "沙拉菜", value)
            self.assertEqual(result["resolution_status"], "canonical")

    def test_unknown_input_reuses_one_pending_id(self):
        first = resolve_ingredient_input(self.conn, "  Mystery   Leaves ")
        second = resolve_ingredient_input(self.conn, "mystery leaves")
        self.conn.commit()
        self.assertEqual(first["ingredient_id"], second["ingredient_id"])
        self.assertTrue(first["ingredient_id"].startswith("pending_"))
        self.assertEqual(len(list_pending(self.conn)), 1)

    def test_merge_rewrites_references_and_resolves_conflicts(self):
        pending = resolve_ingredient_input(self.conn, "mystery leaves")
        source = pending["ingredient_id"]
        self.conn.execute("INSERT INTO dishes(id,name_cn) VALUES('d1','测试沙拉')")
        self.conn.execute("INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES('d1',?,1)", (source,))
        self.conn.execute("INSERT INTO current_pantry(location,ingredient_id,status,quantity_level,is_active) VALUES('hongkong',?,'expiring','low',1)", (source,))
        self.conn.execute("INSERT INTO current_pantry(location,ingredient_id,status,quantity_level,is_active) VALUES('hongkong','沙拉菜','available','enough',0)")
        self.conn.execute("INSERT INTO inventory(date,location) VALUES('2026-08-24','hongkong')")
        inventory_id = self.conn.execute("SELECT id FROM inventory").fetchone()[0]
        self.conn.execute("INSERT INTO inventory_items(inventory_id,ingredient_id) VALUES(?,?)", (inventory_id, source))
        counts = merge_ingredient(self.conn, source, "沙拉菜")
        self.conn.commit()
        self.assertEqual(counts["dish_ingredients"], 1)
        self.assertEqual(self.conn.execute("SELECT ingredient_id FROM dish_ingredients WHERE dish_id='d1'").fetchone()[0], "沙拉菜")
        pantry = self.conn.execute("SELECT status,quantity_level,is_active FROM current_pantry WHERE location='hongkong' AND ingredient_id='沙拉菜'").fetchone()
        self.assertEqual(tuple(pantry), ("expiring", "low", 1))
        self.assertEqual(self.conn.execute("SELECT ingredient_id FROM inventory_items").fetchone()[0], "沙拉菜")
        self.assertEqual(list_pending(self.conn), [])


if __name__ == "__main__":
    unittest.main()
