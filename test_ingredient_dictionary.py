import os
import tempfile
import unittest
from unittest.mock import patch

import db
from ingredient_dictionary import (
    apply_rows, commit_preview, create_preview, export_xlsx, list_dictionary,
    parse_xlsx, validate_rows,
)
from ingredient_resolution import backfill_aliases, resolve_ingredient_input


class IngredientDictionaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db"))
        self.path_patch.start()
        db.init_db()
        self.conn = db.get_db()
        self.conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES(?,?,?,?)",
                          ("maitake", "舞茸", "Maitake Mushroom", '["maitake mush"]'))
        backfill_aliases(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_create_and_alias_resolve(self):
        result = apply_rows(self.conn, [{"ingredient_id": "", "name_cn": "芝麻菜",
            "name_en": "Arugula", "aliases": ["rocket"]}])[0]
        self.assertTrue(result["ingredient_id"].startswith("ingredient_"))
        self.assertEqual(resolve_ingredient_input(self.conn, "ROCKET")["ingredient_id"], result["ingredient_id"])

    def test_alias_conflict_is_rejected_before_write(self):
        _, errors = validate_rows(self.conn, [{"ingredient_id": "", "name_cn": "测试菜",
            "name_en": "Test Green", "aliases": ["maitake mush"]}])
        self.assertTrue(errors)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ingredients WHERE name_cn='测试菜'").fetchone()[0], 0)

    def test_xlsx_round_trip(self):
        self.conn.execute(
            "INSERT INTO ingredient_dictionary_metadata(ingredient_id,status) VALUES('maitake','default')"
        )
        self.conn.commit()
        original = list_dictionary(self.conn)
        parsed = parse_xlsx(export_xlsx(original))
        self.assertEqual(parsed[0]["ingredient_id"], "maitake")
        self.assertEqual(parsed[0]["name_cn"], "舞茸")
        self.assertEqual(parsed[0]["status"], "default")

    def test_default_status_can_be_changed_back_to_canonical(self):
        rows = [{"ingredient_id": "maitake", "name_cn": "舞茸",
                 "name_en": "Maitake Mushroom", "aliases": ["maitake mush"],
                 "status": "default"}]
        apply_rows(self.conn, rows)
        self.assertEqual(list_dictionary(self.conn)[0]["status"], "default")
        rows[0]["status"] = "canonical"
        apply_rows(self.conn, rows)
        self.assertEqual(list_dictionary(self.conn)[0]["status"], "canonical")
        self.assertEqual(self.conn.execute(
            "SELECT value FROM config WHERE key='inventory_version_shenzhen'"
        ).fetchone()[0], "2")
        self.assertEqual(self.conn.execute(
            "SELECT value FROM config WHERE key='inventory_version_hongkong'"
        ).fetchone()[0], "2")

    def test_invalid_status_is_rejected(self):
        _, errors = validate_rows(self.conn, [{"ingredient_id": "maitake", "name_cn": "舞茸",
            "name_en": "Maitake Mushroom", "aliases": ["maitake mush"], "status": "always"}])
        self.assertTrue(any("状态必须" in item["message"] for item in errors))

    def test_unchanged_legacy_incomplete_rows_round_trip(self):
        self.conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) "
                          "VALUES('legacy','旧食材','','[]')")
        self.conn.commit()
        rows = parse_xlsx(export_xlsx(list_dictionary(self.conn)))
        results, errors = validate_rows(self.conn, rows)
        self.assertFalse(errors)
        self.assertTrue(all(row["action"] == "unchanged" for row in results))

    def test_preview_detects_version_change(self):
        preview = create_preview(self.conn, [{"ingredient_id": "maitake", "name_cn": "舞茸",
            "name_en": "Maitake Mushroom", "aliases": ["maitake mush", "hen of the woods"]}])
        self.conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES('x','甲','X')")
        backfill_aliases(self.conn)
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "发生变化"):
            commit_preview(self.conn, preview["preview_token"])


if __name__ == "__main__":
    unittest.main()
