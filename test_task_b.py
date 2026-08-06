#!/usr/bin/env python3
"""Task B security/config/backup tests; all state is temporary and network is local/mock."""

import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


class MockResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class TaskBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "data", "menu.db")
        self.photo_path = os.path.join(self.temp.name, "photos")
        os.makedirs(os.path.dirname(self.db_path))
        os.makedirs(self.photo_path)
        os.environ.update({
            "APP_ENV": "development",
            "PUSH_ENABLED": "false",
            "FAMILY_MENU_DB_PATH": self.db_path,
            "PHOTO_DIR": self.photo_path,
            "H5_BASE_URL": "http://localhost:8090",
            "MAX_UPLOAD_BYTES": "64",
        })
        import runtime_config, db, push_service, photo_security, app, photo_manager, backup_data
        for module in (runtime_config, db, push_service, photo_security, app, photo_manager, backup_data):
            importlib.reload(module)
        self.runtime_config = runtime_config
        self.db = db
        self.push_service = push_service
        self.photo_security = photo_security
        self.app = app
        self.photo_manager = photo_manager
        self.backup_data = backup_data
        db.init_db()
        conn = db.get_db()
        conn.execute("INSERT INTO categories(id,label_cn) VALUES('test','测试')")
        conn.execute("INSERT INTO dishes(id,name_cn,category_id,is_active) VALUES('dish_test','测试菜','test',1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def _request(self, handler, path):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                response = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=3)
                return response.status, response.headers.get_content_type(), response.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.headers.get_content_type(), exc.read()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_01_development_push_disabled(self):
        self.assertFalse(self.push_service.push_is_enabled())

    def test_02_only_production_true_allows_mock_send(self):
        os.environ["APP_ENV"] = "production"
        os.environ["PUSH_ENABLED"] = "true"
        os.environ["H5_BASE_URL"] = "https://menu.example.test"
        self.assertTrue(self.push_service.push_is_enabled())
        with mock.patch("urllib.request.urlopen", return_value=MockResponse({"code": 200, "data": "mock"})):
            result = self.push_service.PushPlusClient("fake-token", "fake-topic").send("title", "body")
        self.assertEqual(result["code"], 200)

    def test_03_production_h5_url_required_and_https_only(self):
        os.environ["APP_ENV"] = "production"
        for value in ("", "http://menu.example.test", "https://localhost", "https://127.0.0.1"):
            os.environ["H5_BASE_URL"] = value
            with self.assertRaises(self.push_service.PushError):
                self.push_service.get_h5_base_url()
        os.environ["H5_BASE_URL"] = "https://menu.example.test"
        self.assertEqual(self.push_service.get_h5_base_url(), "https://menu.example.test")

    def test_04_database_and_photo_paths_are_environment_driven(self):
        self.assertEqual(self.db.DB_PATH, self.db_path)
        self.assertEqual(self.photo_manager.PHOTOS_DIR, self.photo_path)
        self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM dishes").fetchone()[0], 1)

    def test_05_health_ok_for_app_and_manager(self):
        for check in (self.app.health_result, self.photo_manager.health_result):
            status, body = check()
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "ok")

    def test_06_health_returns_503_for_bad_database(self):
        original = self.db.DB_PATH
        corrupt = os.path.join(self.temp.name, "corrupt.db")
        with open(corrupt, "wb") as file:
            file.write(b"not sqlite")
        self.db.DB_PATH = corrupt
        try:
            status, body = self.app.health_result()
            self.assertEqual(status, 503)
            self.assertEqual(body["status"], "error")
        finally:
            self.db.DB_PATH = original

    def test_07_backup_restore_and_counts(self):
        backup_dir = os.path.join(self.temp.name, "backups")
        backup = self.backup_data.backup_database(self.db_path, backup_dir)
        restored = self.backup_data.restore_database(backup, os.path.join(self.temp.name, "restored.db"))
        self.assertEqual(self.backup_data.quick_check(restored), "ok")
        source = sqlite3.connect(self.db_path).execute("SELECT COUNT(*) FROM dishes").fetchone()[0]
        target = sqlite3.connect(restored).execute("SELECT COUNT(*) FROM dishes").fetchone()[0]
        self.assertEqual(source, target)

    def test_08_photo_path_traversal_is_rejected(self):
        for value in ("../menu.db", "%2e%2e%2fmenu.db", "..\\menu.db", ""):
            with self.assertRaises(self.photo_security.PhotoValidationError):
                self.photo_security.resolve_photo_path(self.photo_path, value)

    def test_09_illegal_and_oversized_uploads_rejected(self):
        with self.assertRaises(self.photo_security.PhotoValidationError):
            self.photo_security.validate_image_bytes(b"<html>bad</html>", 64)
        with self.assertRaises(self.photo_security.PhotoValidationError):
            self.photo_security.validate_image_bytes(b"\xff\xd8\xff" + b"x" * 100, 64)
        self.assertEqual(self.photo_security.validate_image_bytes(b"\xff\xd8\xffok", 64), ".jpg")

    def test_10_push_error_redacts_token(self):
        token = "TOP-SECRET-TOKEN"
        response = MockResponse({"code": 500, "msg": f"bad credential {token}"})
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(self.push_service.PushError) as caught:
                self.push_service.PushPlusClient(token, "topic").send("title", "body")
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_11_photo_archive_and_remote_default_off(self):
        with open(os.path.join(self.photo_path, "sample.jpg"), "wb") as image:
            image.write(b"\xff\xd8\xffsample")
        artifact = self.backup_data.backup_photos(
            self.photo_path, os.path.join(self.temp.name, "backups")
        )
        self.assertTrue(os.path.isfile(artifact))
        os.environ["BACKUP_REMOTE_ENABLED"] = "false"
        self.assertFalse(self.backup_data.remote_copy(artifact))


if __name__ == "__main__":
    unittest.main(verbosity=2)
