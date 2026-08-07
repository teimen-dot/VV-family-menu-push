import importlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


WORK_DIR = Path(__file__).resolve().parent
BASELINE_DB = WORK_DIR / "family_menu_test.db"


class DinnerAiFillAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="dinner-ai-fill-")
        self.db_path = Path(self.tempdir.name) / "family_menu.db"
        shutil.copyfile(BASELINE_DB, self.db_path)
        self.old_db_path = os.environ.get("FAMILY_MENU_DB_PATH")
        os.environ["FAMILY_MENU_DB_PATH"] = str(self.db_path)
        import db, inventory, menu_service
        for module in (db, inventory, menu_service):
            importlib.reload(module)
        self.db, self.menu_service = db, menu_service
        self.menu_service.invalidate_catalog_cache()
        conn = self.db.get_db()
        conn.execute(
            "INSERT INTO menus(date,location,status,diners,meal_mode) "
            "VALUES('2099-12-30','shenzhen','draft','[\"vv\",\"sir\"]','daily')"
        )
        self.menu_id = conn.execute(
            "SELECT id FROM menus WHERE date='2099-12-30'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO menu_meal_settings(menu_id,meal_type,diners) "
            "VALUES(?, 'dinner', '[\"vv\",\"sir\",\"guest\"]')",
            (self.menu_id,),
        )
        conn.execute(
            "INSERT INTO menu_items(menu_id,dish_id,meal_type,is_locked,source) "
            "VALUES(?, 'dish_0028', 'dinner', 1, 'owner')",
            (self.menu_id,),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("FAMILY_MENU_DB_PATH", None)
        else:
            os.environ["FAMILY_MENU_DB_PATH"] = self.old_db_path
        self.tempdir.cleanup()

    @staticmethod
    def all_available(dish_ids, _location):
        return {
            dish_id: {
                "status": "available", "required": [], "available_required": [],
                "missing_required": [], "optional": [], "missing_fields": [],
                "data_complete": True,
            }
            for dish_id in dish_ids
        }

    def test_three_diner_dinner_with_one_protein_adds_the_missing_protein(self):
        with patch.object(
            self.menu_service, "check_dishes_availability_batch",
            side_effect=self.all_available,
        ):
            ok, message, review = self.menu_service.ai_fill_menu(
                self.menu_id, "shenzhen", seed=42, meal_type="dinner"
            )
        self.assertTrue(ok, message)
        protein_additions = [
            item for item in review["added_details"]
            if item["slot_role"] == "protein_main"
        ]
        self.assertEqual(1, len(protein_additions))
        dinner_slots = review["slot_analysis_after"]["dinner"]
        self.assertEqual(2, dinner_slots["protein_main"]["target_min"])
        self.assertGreaterEqual(dinner_slots["protein_main"]["current"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
