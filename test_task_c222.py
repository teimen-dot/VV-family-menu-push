#!/usr/bin/env python3
"""C2.2.2 new Family UI integration and permission regression tests."""

import importlib
import os
import sqlite3
import tempfile
import unittest


class NewFamilyUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "ui.db")
        os.environ["FAMILY_MENU_DB_PATH"] = self.db_path
        os.environ["APP_ENV"] = "development"
        os.environ["PUSH_ENABLED"] = "false"
        os.environ["OWNER_AUTH_USERNAME"] = "vivian"
        os.environ["WORKER_AUTH_USERNAME"] = "kitchen"
        import db
        import app
        importlib.reload(db)
        importlib.reload(app)
        db.init_db()
        self.app = app

    def tearDown(self):
        self.tmp.cleanup()

    def test_shell_injects_only_server_verified_role(self):
        owner = self.app.render_family_ui("owner", "shenzhen")
        worker = self.app.render_family_ui("worker", "hongkong")
        self.assertIn('data-role="owner"', owner)
        self.assertIn('data-role="worker"', worker)
        self.assertNotIn("__ROLE__", owner + worker)
        self.assertEqual(self.app.authenticated_role("vivian"), "owner")
        self.assertEqual(self.app.authenticated_role("kitchen"), "worker")
        self.assertEqual(self.app.authenticated_role("admin"), "unknown")

    def test_ui_has_no_mock_or_localstorage_business_source(self):
        root = os.path.join(os.path.dirname(__file__), "family_ui")
        parts = []
        for name in ("index.html", "app.js"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                parts.append(handle.read())
        sources = "\n".join(parts)
        self.assertNotIn("localStorage", sources)
        self.assertNotIn("dishLibrary", sources)
        self.assertNotIn("mock data", sources.lower())
        for endpoint in ("/api/tomorrow", "/api/pantry", "/api/dishes", "/api/history", "/api/ui-context"):
            self.assertIn(endpoint, sources)

    def test_worker_menu_writes_remain_forbidden(self):
        self.assertFalse(self.app.post_path_allowed("worker", "/api/tomorrow/confirm"))
        self.assertFalse(self.app.post_path_allowed("worker", "/api/tomorrow/ai-fill"))
        self.assertFalse(self.app.post_path_allowed("worker", "/api/tomorrow/replace"))
        self.assertTrue(self.app.post_path_allowed("worker", "/api/pantry/update_status"))

    def test_ui_assets_and_responsive_contracts(self):
        root = os.path.join(os.path.dirname(__file__), "family_ui")
        with open(os.path.join(root, "styles.css"), encoding="utf-8") as handle:
            css = handle.read()
        with open(os.path.join(root, "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        with open(os.path.join(root, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("viewport-fit=cover", html)
        self.assertIn("@media (max-width: 390px)", css)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", css)
        self.assertIn("if (!owner)", js)
        self.assertIn("credentials: 'same-origin'", js)


if __name__ == "__main__":
    unittest.main()
