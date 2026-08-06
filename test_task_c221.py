import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import photo_manager


ROOT = Path(__file__).resolve().parent


class RoleModelTests(unittest.TestCase):
    def test_role_mapping_is_configured_server_side(self):
        env = {"OWNER_AUTH_USERNAME": "vv_owner", "WORKER_AUTH_USERNAME": "home_worker"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(app.authenticated_role("vv_owner"), "owner")
            self.assertEqual(app.authenticated_role("home_worker"), "worker")
            self.assertEqual(app.authenticated_role("family"), "unknown")
            self.assertEqual(app.authenticated_role("admin"), "unknown")
            self.assertEqual(app.authenticated_role(""), "unknown")

    def test_worker_can_only_write_pantry(self):
        for path in app.PANTRY_POST_PATHS:
            self.assertTrue(app.post_path_allowed("worker", path), path)
        for path in app.OWNER_ONLY_POST_PATHS:
            self.assertFalse(app.post_path_allowed("worker", path), path)
            self.assertTrue(app.post_path_allowed("owner", path), path)

    def test_read_only_post_endpoints_remain_available(self):
        for path in ("/api/dishes/availability", "/api/dishes/recommend"):
            self.assertTrue(app.post_path_allowed("worker", path))

    def test_unknown_role_never_writes(self):
        for path in app.PANTRY_POST_PATHS | app.OWNER_ONLY_POST_PATHS:
            self.assertFalse(app.post_path_allowed("unknown", path), path)

    def test_nginx_overwrites_authenticated_user_header(self):
        nginx = (ROOT / "deploy/nginx-family-menu.conf.example").read_text()
        self.assertIn("proxy_set_header X-Authenticated-User $remote_user;", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:8090;", nginx)


class PwaTests(unittest.TestCase):
    def test_manifests(self):
        expected = {
            "family": ("家庭菜单", "菜单", "#2c2620"),
            "admin": ("菜品管理", "菜品", "#007aff"),
        }
        for app_name, values in expected.items():
            manifest = json.loads((ROOT / "pwa" / app_name / "manifest.webmanifest").read_text())
            self.assertEqual((manifest["name"], manifest["short_name"], manifest["theme_color"]), values)
            self.assertEqual(manifest["start_url"], "/")
            self.assertEqual(manifest["scope"], "/")
            self.assertEqual(manifest["display"], "standalone")
            self.assertNotIn("serviceworker", json.dumps(manifest).lower())

    def test_icons_exist_and_differ(self):
        for app_name in ("family", "admin"):
            for icon in ("apple-touch-icon.png", "icon-192.png", "icon-512.png", "favicon.png"):
                self.assertGreater((ROOT / "pwa" / app_name / icon).stat().st_size, 100)
        self.assertNotEqual(
            (ROOT / "pwa/family/icon-512.png").read_bytes(),
            (ROOT / "pwa/admin/icon-512.png").read_bytes(),
        )

    def test_no_service_worker_or_api_cache(self):
        sources = (ROOT / "app.py").read_text() + (ROOT / "photo_manager.py").read_text()
        self.assertNotIn("navigator.serviceWorker.register", sources)
        self.assertIn('self.send_header("Cache-Control", "no-store")', sources)


class ResponsiveMarkupTests(unittest.TestCase):
    def test_family_mobile_layout_rules(self):
        source = (ROOT / "app.py").read_text()
        self.assertIn("@media (max-width:767px)", source)
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", source)
        self.assertIn('class="pantry-name"', source)
        self.assertIn("viewport-fit=cover", source)

    def test_admin_mobile_layout_rules(self):
        source = (ROOT / "photo_manager.py").read_text()
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", source)
        self.assertIn("-webkit-line-clamp: 2", source)
        self.assertIn("aspect-ratio: 1 / 1", source)
        self.assertIn("viewport-fit=cover", source)


if __name__ == "__main__":
    unittest.main()
