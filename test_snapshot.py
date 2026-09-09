import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # No real credentials or Telegram connection are used.
        with patch.dict(os.environ, {
            "TELEGRAM_API_ID": "987654321",
            "TELEGRAM_API_HASH": "test-api-hash",
            "TELEGRAM_SESSION_STRING": "",
            "FEED_TOKEN": "test-feed-token",
        }):
            cls.module = importlib.import_module("app")
        cls.module.SESSION_STRING = "test-session-string"
        cls.http = TestClient(cls.module.app)  # Do not run Telegram startup.

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "feed_snapshot.json"
        self.path_patch = patch.object(self.module, "SNAPSHOT_PATH", str(self.path))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.data = {
            "timestamp": "2026-09-09T12:00:00+00:00", "count": 1,
            "status": "running", "items": [{
                "message_id": 42, "channel_id": 123, "channel_name": "뉴스",
                "channel_username": "news", "date_utc": "2026-09-09T12:00:00+00:00",
                "message": "Market update", "url": "https://t.me/news/42",
            }],
        }

    def write(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def assert_unavailable(self):
        response = self.http.get("/snapshot")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            "timestamp": None, "count": 0, "status": "unavailable", "items": [],
        })
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_public_snapshot_and_refresh(self):
        self.write()
        response = self.http.get("/snapshot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.data)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.data.update(items=[], count=0, status="initializing")
        self.write()
        self.assertEqual(self.http.get("/snapshot").json(), self.data)

    def test_allowlist_and_count(self):
        expected = json.loads(json.dumps(self.data))
        for key in ("FEED_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH",
                    "TELEGRAM_SESSION_STRING", "debug", "environment"):
            self.data[key] = "private"
            self.data["items"][0][key] = {"nested": "private"}
        self.data["count"] = 999
        self.write()
        self.assertEqual(self.http.get("/snapshot").json(), expected)

    def test_credentials_inside_public_fields_fail_closed(self):
        for value in (str(self.module.API_ID), self.module.API_HASH,
                      self.module.SESSION_STRING, self.module.FEED_TOKEN):
            for field in ("message", "url", "channel_name"):
                with self.subTest(value=value, field=field):
                    self.data["items"][0][field] = "prefix " + value
                    self.write()
                    self.assert_unavailable()
                    self.data["items"][0][field] = ""

    def test_escaped_credentials_fail_closed(self):
        for secret in ('test-"quoted"-token', 'test-\\backslash-token', 'test-\nnewline-token'):
            with self.subTest(secret=secret), patch.object(self.module, "FEED_TOKEN", secret):
                self.data["items"][0]["message"] = "prefix " + secret
                self.write()
                self.assert_unavailable()

    def test_missing_corrupt_and_invalid_snapshot(self):
        self.assert_unavailable()
        for raw in ("{", "[]", "null", '{"items": []}', "\\ud800"):
            self.path.write_bytes(raw.encode("utf-8", errors="surrogatepass"))
            self.assert_unavailable()
        for key, value in (("timestamp", {}), ("status", "secret error"),
                           ("items", {}), ("items", [None]),
                           ("items", [{"message": {"FEED_TOKEN": "private"}}])):
            with self.subTest(key=key, value=value):
                original = self.data[key]
                self.data[key] = value
                self.write()
                self.assert_unavailable()
                self.data[key] = original

    def test_read_error_does_not_leak(self):
        with patch("builtins.open", side_effect=PermissionError("private path and token")):
            self.assert_unavailable()

    def test_feed_auth_unchanged(self):
        self.assertEqual(self.http.get("/feed").status_code, 422)
        self.assertEqual(self.http.get("/feed?token=wrong").status_code, 403)
        response = self.http.get("/feed", params={"token": self.module.FEED_TOKEN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])


if __name__ == "__main__":
    unittest.main()
