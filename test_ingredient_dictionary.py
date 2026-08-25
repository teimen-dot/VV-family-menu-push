import os
import tempfile
import unittest
from unittest.mock import patch

import db
from ingredient_dictionary import (
    apply_rows, commit_preview, create_preview, export_xlsx, list_dictionary,
    list_pending_dictionary, parse_xlsx, validate_rows,
)
from ingredient_resolution import backfill_aliases, resolve_ingredient_input
from repair_blue_berry_pending import repair


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

    def test_xlsx_pending_sheet_round_trip_and_merge(self):
        pending = resolve_ingredient_input(self.conn, "Mysterious Leaf")
        self.conn.commit()
        canonical, pending_rows = parse_xlsx(
            export_xlsx(list_dictionary(self.conn), list_pending_dictionary(self.conn)),
            include_pending=True,
        )
        self.assertEqual(pending_rows[0]["pending_id"], pending["ingredient_id"])
        self.assertEqual(pending_rows[0]["raw_input"], "Mysterious Leaf")
        pending_rows[0]["target_id"] = "maitake"
        preview = create_preview(self.conn, canonical, pending_rows)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["counts"]["pending_merge"], 1)
        commit_preview(self.conn, preview["preview_token"])
        self.assertEqual(resolve_ingredient_input(self.conn, "Mysterious Leaf")["ingredient_id"], "maitake")

    def test_xlsx_pending_can_become_new_canonical(self):
        pending = resolve_ingredient_input(self.conn, "Cloud Berry")
        self.conn.commit()
        pending_rows = list_pending_dictionary(self.conn)
        pending_rows[0].update({"name_cn": "云莓", "name_en": "Cloudberry", "aliases": ["cloud berry"]})
        preview = create_preview(self.conn, list_dictionary(self.conn), pending_rows)
        self.assertTrue(preview["ok"])
        commit_preview(self.conn, preview["preview_token"])
        self.assertEqual(resolve_ingredient_input(self.conn, "云莓")["ingredient_id"], pending["ingredient_id"])

    def test_pending_readonly_fields_and_partial_names_are_rejected(self):
        resolve_ingredient_input(self.conn, "Unknown Green")
        self.conn.commit()
        pending_rows = list_pending_dictionary(self.conn)
        pending_rows[0]["raw_input"] = "changed"
        pending_rows[0]["name_cn"] = "未知菜"
        preview = create_preview(self.conn, list_dictionary(self.conn), pending_rows)
        self.assertFalse(preview["ok"])
        self.assertTrue(any("只读" in error["message"] for error in preview["errors"]))
        self.assertTrue(any("均为必填" in error["message"] for error in preview["errors"]))

    def test_latin_compact_match_requires_unique_owner(self):
        self.conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES('blueberry','蓝莓','Blueberry','[]')")
        backfill_aliases(self.conn)
        self.conn.commit()
        for value in ("blueberry", "blue berry", "blue-berry", "蓝莓"):
            self.assertEqual(resolve_ingredient_input(self.conn, value)["ingredient_id"], "blueberry")
        self.conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES('abc1','甲','AB-C','[]')")
        self.conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES('abc2','乙','A-BC','[]')")
        backfill_aliases(self.conn)
        self.conn.commit()
        self.assertTrue(resolve_ingredient_input(self.conn, "a bc")["ingredient_id"].startswith("pending_"))

    def test_blue_berry_repair_is_idempotent(self):
        self.conn.execute("INSERT INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES('blueberry','蓝莓','Blueberry','[]')")
        backfill_aliases(self.conn)
        pending = resolve_ingredient_input(self.conn, "blue berry")
        self.assertEqual(pending["ingredient_id"], "blueberry")
        # Simulate the production pending created before compact matching existed.
        self.conn.execute("DELETE FROM ingredient_aliases WHERE ingredient_id='blueberry'")
        self.conn.execute("INSERT OR IGNORE INTO ingredients(ingredient_id,name_cn,name_en,aliases) VALUES(?, '', 'blue berry','[]')",
                          ("pending_dc553147848b6d2ef22a",))
        self.conn.execute("INSERT OR IGNORE INTO pending_ingredients(pending_id,normalized_key,raw_input,detected_language,status) "
                          "VALUES(?,?,'blue berry','en','pending')",
                          ("pending_dc553147848b6d2ef22a", "blue berry"))
        backfill_aliases(self.conn)
        self.conn.commit()
        first = repair(self.conn, apply=True)
        second = repair(self.conn, apply=True)
        self.assertEqual(first["status"], "repaired")
        self.assertEqual(second["status"], "already_repaired")
        self.assertEqual(resolve_ingredient_input(self.conn, "blue berry")["ingredient_id"], "blueberry")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='ingredient_pending_merged'").fetchone()[0], 1)

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
