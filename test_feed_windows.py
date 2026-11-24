import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_feed_windows import build_window
from health import is_stale, load, write_health


NOW = datetime(2026, 9, 13, 12, 30, tzinfo=timezone.utc)


class FeedWindowTests(unittest.TestCase):
    def snapshot(self, timestamp="2026-09-13T12:20:00+00:00", status="running"):
        return {
            "timestamp": timestamp,
            "count": 3,
            "status": status,
            "items": [
                {"channel_name": "old", "date_utc": "2026-09-12T11:00:00+00:00",
                 "message": "outside", "url": "https://t.me/old/1"},
                {"channel_name": "recent", "date_utc": "2026-09-13T11:30:00+00:00",
                 "message": "recent news", "url": "https://t.me/recent/2"},
                {"channel_name": "latest", "date_utc": "2026-09-13T12:25:00+00:00",
                 "message": "latest news", "url": "https://t.me/latest/3"},
            ],
        }

    def test_filters_projects_and_sorts(self):
        result = build_window(self.snapshot(), 6, NOW)
        self.assertEqual(result["freshness_status"], "ok")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["latest_message_at"], "2026-09-13T12:25:00+00:00")
        self.assertEqual(list(result["items"][0]),
                         ["channel_name", "published_at", "message", "attachments", "links", "url"])
        self.assertEqual(result["items"][0]["channel_name"], "latest")

    def test_stale_is_based_on_snapshot_generation(self):
        result = build_window(self.snapshot(timestamp="2026-09-13T11:00:00+00:00"), 6, NOW)
        self.assertEqual(result["freshness_status"], "stale")

    def test_initializing_is_unavailable(self):
        result = build_window(self.snapshot(status="initializing"), 24, NOW)
        self.assertEqual(result["freshness_status"], "unavailable")

    def test_empty_window_has_null_latest_message(self):
        snapshot = self.snapshot()
        snapshot["items"] = []
        result = build_window(snapshot, 6, NOW)
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["latest_message_at"])

    def test_rejects_naive_timestamp(self):
        with self.assertRaises(ValueError):
            build_window(self.snapshot(timestamp="2026-09-13T12:20:00"), 6, NOW)

    def test_rejects_non_string_public_field(self):
        snapshot = self.snapshot()
        snapshot["items"][0]["message"] = {"unexpected": "value"}
        with self.assertRaises(ValueError):
            build_window(snapshot, 24, NOW)

    def test_omits_items_without_text(self):
        snapshot = self.snapshot()
        snapshot["items"][1]["message"] = "   "
        result = build_window(snapshot, 6, NOW)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["channel_name"], "latest")

    def test_attachment_media_fields_are_allow_listed(self):
        snapshot = self.snapshot()
        snapshot["items"][2]["attachments"] = [{
            "type": "pdf", "name": "r.pdf", "mime_type": "application/pdf",
            "drive_file_id": "abc", "size": 12, "sha256": "f" * 64,
            "status": "uploaded", "local_path": "/secret/path"}]
        attachment = build_window(snapshot, 6, NOW)["items"][0]["attachments"][0]
        self.assertEqual(attachment, {
            "type": "pdf", "name": "r.pdf", "mime_type": "application/pdf",
            "drive_file_id": "abc", "sha256": "f" * 64, "status": "uploaded", "size": 12})

    def test_legacy_attachment_keeps_three_string_fields(self):
        snapshot = self.snapshot()
        snapshot["items"][2]["attachments"] = [
            {"type": "photo", "name": "", "mime_type": "image/jpeg"}]
        attachment = build_window(snapshot, 6, NOW)["items"][0]["attachments"][0]
        self.assertEqual(attachment, {"type": "photo", "name": "", "mime_type": "image/jpeg"})

    def test_rejects_bad_attachment_and_link_fields(self):
        for key, value in (
                ("attachments", [{"type": "photo", "name": None, "mime_type": "x"}]),
                ("attachments", [{"type": "pdf", "name": "a", "mime_type": "b", "size": "9"}]),
                ("attachments", [{"type": "pdf", "name": "a", "mime_type": "b", "size": True}]),
                ("attachments", [{"type": "pdf", "name": "a", "mime_type": "b", "drive_file_id": 1}]),
                ("links", "not a list"),
                ("links", [{"url": "u", "final_url": "f", "status": "ok"}])):
            with self.subTest(key=key, value=value):
                snapshot = self.snapshot()
                snapshot["items"][2][key] = value
                with self.assertRaises(ValueError):
                    build_window(snapshot, 6, NOW)

    def test_links_are_projected(self):
        snapshot = self.snapshot()
        snapshot["items"][2]["links"] = [{
            "url": "https://bit.ly/x", "final_url": "https://example.com/a",
            "status": "ok", "title": "Example", "extra": "dropped"}]
        link = build_window(snapshot, 6, NOW)["items"][0]["links"][0]
        self.assertEqual(link, {"url": "https://bit.ly/x", "final_url": "https://example.com/a",
                                "status": "ok", "title": "Example"})
        self.assertEqual(build_window(self.snapshot(), 6, NOW)["items"][0]["links"], [])


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data/health/telegram.json"

    def test_success_then_failure_keeps_last_success(self):
        write_health(self.path, now=NOW, trigger="schedule", error=None, items_count=7,
                     drive={"configured": True, "usage_bytes": 5, "limit_bytes": 10})
        health = load(self.path)
        self.assertEqual(set(health), {"last_run_utc", "last_success_utc", "trigger",
                                       "error", "items_count", "drive", "links", "collect"})
        self.assertEqual(health["last_success_utc"], NOW.isoformat())
        later = NOW + timedelta(minutes=15)
        write_health(self.path, now=later, trigger="watchdog", error="RuntimeError: boom",
                     items_count=None)
        health = load(self.path)
        self.assertEqual(health["last_run_utc"], later.isoformat())
        self.assertEqual(health["last_success_utc"], NOW.isoformat())
        self.assertEqual(health["items_count"], 7)
        self.assertEqual(health["error"], "RuntimeError: boom")
        self.assertEqual(health["trigger"], "watchdog")

    def test_first_failure_has_no_success(self):
        write_health(self.path, now=NOW, trigger="manual", error="x", items_count=None)
        self.assertIsNone(load(self.path)["last_success_utc"])

    def test_staleness_threshold_is_40_minutes(self):
        fresh = {"last_success_utc": (NOW - timedelta(minutes=39)).isoformat()}
        old = {"last_success_utc": (NOW - timedelta(minutes=41)).isoformat()}
        self.assertFalse(is_stale(fresh, NOW)[0])
        self.assertTrue(is_stale(old, NOW)[0])

    def test_missing_malformed_or_future_is_stale(self):
        for health in ({}, {"last_success_utc": None}, {"last_success_utc": "garbage"},
                       {"last_success_utc": "2026-09-13T12:00:00"},
                       {"last_success_utc": (NOW + timedelta(hours=3)).isoformat()}):
            with self.subTest(health=health):
                self.assertTrue(is_stale(health, NOW)[0])
        self.assertEqual(load(Path(self.temp.name) / "missing.json"), {})


if __name__ == "__main__":
    unittest.main()
