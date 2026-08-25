import os
import tempfile
import unittest
from unittest.mock import patch

import db
import inventory
from migrate_default_ingredients import DEFAULT_INGREDIENTS, apply_default_ingredients


class DefaultIngredientMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db"))
        self.path_patch.start()
        db.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.tmp.cleanup()

    def test_migration_is_idempotent_and_rice_aliases_share_one_id(self):
        conn = db.get_db()
        conn.execute(
            "INSERT INTO ingredients(ingredient_id,name_cn,name_en) VALUES('米饭','米饭','Cooked Rice')"
        )
        conn.execute(
            "INSERT INTO dishes(id,name_cn,meal_tags,is_active) VALUES('dish_rice','白米饭','[\"lunch\"]',1)"
        )
        conn.execute(
            "INSERT INTO dish_ingredients(dish_id,ingredient_id,required) VALUES('dish_rice','米饭',1)"
        )
        conn.commit()
        apply_default_ingredients(conn)
        apply_default_ingredients(conn)
        self.assertEqual(conn.execute(
            "SELECT ingredient_id FROM dish_ingredients WHERE dish_id='dish_rice'"
        ).fetchone()[0], "rice")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM ingredient_dictionary_metadata WHERE status='default'"
        ).fetchone()[0], len(DEFAULT_INGREDIENTS))
        for text in ("大米", "米", "米饭", "rice"):
            self.assertEqual(conn.execute(
                "SELECT ingredient_id FROM ingredient_aliases WHERE alias_key=?", (text.casefold(),)
            ).fetchone()[0], "rice")
        self.assertTrue(inventory.is_pantry_exempt_ingredient("rice", "大米", conn))
        conn.close()


if __name__ == "__main__":
    unittest.main()
