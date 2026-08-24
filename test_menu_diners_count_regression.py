import importlib
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import app
import db
import menu_service


class MenuDinersCountRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "FAMILY_MENU_DB_PATH": os.path.join(self.tmp.name, "menu.db"),
                "APP_ENV": "development",
                "PUSH_ENABLED": "false",
            },
            clear=False,
        )
        self.env.start()
        importlib.reload(db)
        db.init_db()
        conn = db.get_db()
        conn.execute(
            "INSERT INTO menus "
            "(id,date,location,status,diners,diners_count,meal_mode,banquet_total_diners) "
            "VALUES (1,'2099-01-01','shenzhen','draft','[]',4,'banquet',12)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.env.stop()
        importlib.reload(db)
        self.tmp.cleanup()

    def test_member_list_and_banquet_fields_do_not_override_diners_count(self):
        self.assertEqual(menu_service._get_effective_diners_count(menu_id=1), 4)

        conn = db.get_db()
        conn.execute(
            "UPDATE menus SET diners=?, meal_mode='banquet', banquet_total_diners=20 WHERE id=1",
            (json.dumps(["vivian", "sir", "grandma"]),),
        )
        conn.commit()
        conn.close()

        self.assertEqual(menu_service._get_effective_diners_count(menu_id=1), 4)

    def test_updating_empty_or_nonempty_member_list_preserves_diners_count(self):
        for diners in ([], ["vivian", "sir", "grandma"]):
            with self.subTest(diners=diners):
                self.assertTrue(app.update_menu_diners(1, diners))
                conn = db.get_db()
                row = conn.execute(
                    "SELECT diners,diners_count FROM menus WHERE id=1"
                ).fetchone()
                conn.close()
                self.assertEqual(json.loads(row["diners"]), diners)
                self.assertEqual(row["diners_count"], 4)

    def test_legacy_meal_mode_write_surface_is_removed(self):
        self.assertFalse(hasattr(app, "get_menu_meal_mode"))
        self.assertFalse(hasattr(app, "update_menu_meal_mode"))
        self.assertNotIn("/api/tomorrow/meal-mode", app.OWNER_ONLY_POST_PATHS)
        self.assertNotIn("/api/tomorrow/meal-mode", app.MENU_DRAFT_WRITE_PATHS)
        self.assertNotIn(
            "/api/tomorrow/meal-mode", app.AppHandler.do_POST.__code__.co_consts
        )

    def test_generate_and_bootstrap_use_only_diners_count(self):
        fake_gap_filler = MagicMock()
        fake_gap_filler.generate_day.return_value = (
            {
                "breakfast": {"dishes": []},
                "lunch": {"dishes": []},
                "dinner": {"dishes": []},
            },
            {"review": {"passed": True, "issues": []}},
        )

        with patch.object(menu_service, "_load_pool", return_value={"dishes": []}), \
             patch.object(menu_service, "get_dish_ingredients_map", return_value={}), \
             patch.object(menu_service, "get_available_ingredient_ids", return_value=(set(), set(), set())), \
             patch.object(menu_service, "get_preference_scores", return_value={}), \
             patch.object(menu_service, "check_dishes_availability_batch", return_value={}), \
             patch.object(menu_service, "get_rotation_context", return_value={}), \
             patch.object(menu_service, "GapFiller", return_value=fake_gap_filler), \
             patch.object(menu_service, "generate_afternoon_snack", return_value=[]):
            menu_service.generate_and_store_menu("2099-01-01", "shenzhen", seed=42)
            menu_service.generate_and_store_menu("2099-01-02", "shenzhen", seed=42)

        calls = fake_gap_filler.generate_day.call_args_list
        self.assertEqual([call.kwargs["diners_count"] for call in calls], [4, 4])
        self.assertNotIn("is_banquet", calls[0].kwargs["context"])

    def test_ai_fill_and_reconcile_share_diners_count(self):
        fake_gap_filler = MagicMock()
        fake_gap_filler.get_candidates.return_value = []
        review_result = {"passed": True, "warnings": [], "issues": []}

        with patch.object(menu_service, "_load_pool", return_value={"dishes": []}), \
             patch.object(menu_service, "get_dish_ingredients_map", return_value={}), \
             patch.object(menu_service, "get_inventory_ingredients", return_value=(set(), set(), set())), \
             patch.object(menu_service, "get_preference_scores", return_value={}), \
             patch.object(menu_service, "get_rotation_context", return_value={}), \
             patch.object(menu_service, "GapFiller", return_value=fake_gap_filler), \
             patch.object(menu_service.RuleEngine, "final_review", return_value=review_result.copy()) as final_review:
            ok, _, _ = menu_service.ai_fill_menu(1, location="shenzhen", seed=42)

        self.assertTrue(ok)
        self.assertEqual(final_review.call_args.args[1], 4)

        fill_review = {"passed": True, "warnings": [], "issues": []}
        with patch.object(menu_service, "_load_pool", return_value={"dishes": []}), \
             patch.object(menu_service, "ai_fill_menu", return_value=(True, "ok", fill_review)):
            ok, _, reconcile_review = menu_service.reconcile_meal_for_diners(
                1, location="shenzhen"
            )

        self.assertTrue(ok)
        self.assertEqual(reconcile_review["reconcile_diners"], 4)


if __name__ == "__main__":
    unittest.main()
