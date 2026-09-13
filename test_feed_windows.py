import unittest
from datetime import datetime, timezone

from build_feed_windows import build_window


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
                         ["channel_name", "published_at", "message", "url"])
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


if __name__ == "__main__":
    unittest.main()
