import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import market_hub as hub


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 9, 10, 14, tzinfo=timezone.utc)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def news(self):
        return {"timestamp": hub.iso(self.clock), "items": [{
            "news_id": "n1", "title": "Memory demand", "source": "Example",
            "published_at": hub.iso(self.clock), "tickers": ["MU"],
            "url": "https://saveticker.com/news/n1"}]}

    def chain(self):
        return {"symbol": "MU", "as_of": hub.iso(self.clock), "spot_price": 100,
                "contracts": [{"expiration": hub.iso(self.clock + timedelta(days=7)),
                    "strike": 100, "side": side, "volume": volume,
                    "open_interest": oi, "implied_volatility": .4,
                    "bid": 2, "ask": 3} for side, volume, oi in
                    (("call", 300, 100), ("put", 150, 200))]}

    def test_news_dedup_id_url_and_story(self):
        data = self.news()
        duplicate = copy.deepcopy(data["items"][0])
        duplicate.update(news_id="n2", url="https://saveticker.com/news/n2")
        data["items"] += [copy.deepcopy(data["items"][0]), duplicate]
        result = hub.parse_saveticker(data, self.clock)
        self.assertEqual(result["count"], 1)
        self.assertIsNone(result["items"][0]["summary"])

    def test_news_bad_schema_empty_stale_timezone(self):
        for mutation in (lambda x: x.update(items=[]),
                         lambda x: x.update(timestamp="2020-01-01T00:00:00Z"),
                         lambda x: x["items"][0].update(published_at="2026-09-10T14:00:00"),
                         lambda x: x["items"][0].update(news_id=None)):
            data = self.news()
            mutation(data)
            with self.assertRaises(hub.DataError):
                hub.parse_saveticker(data, self.clock)

    def test_options_calculations(self):
        summary = hub.summarize_options(hub.normalize_options(self.chain(), "MU", self.clock), self.clock)
        self.assertEqual(summary["put_call_volume_ratio"], .5)
        self.assertEqual(summary["put_call_oi_ratio"], 2)
        self.assertAlmostEqual(summary["expected_move_pct"], 100 * .4 * (7 / 365) ** .5)
        self.assertEqual(summary["unusual_strikes"][0]["side"], "call")
        self.assertEqual(summary["oi_concentration"][0]["share"], 1)

    def test_options_missing_is_not_zero(self):
        data = self.chain()
        data["contracts"][0]["volume"] = None
        summary = hub.summarize_options(hub.normalize_options(data, "MU", self.clock), self.clock)
        self.assertIsNone(summary["total_call_volume"])
        self.assertIsNone(summary["put_call_volume_ratio"])

    def test_options_invalid_contracts(self):
        for mutation in (lambda x: x["contracts"].append(copy.deepcopy(x["contracts"][0])),
                         lambda x: x["contracts"][0].update(bid=10),
                         lambda x: x["contracts"][0].update(volume=True),
                         lambda x: x.update(contracts=[])):
            data = self.chain()
            mutation(data)
            with self.assertRaises(hub.DataError):
                hub.normalize_options(data, "MU", self.clock)

    def test_unavailable_preserves_previous_data_and_fails(self):
        path = self.root / "data/saveticker/latest_saveticker.json"
        hub.write_json(path, hub.parse_saveticker(self.news(), self.clock))
        original = path.read_bytes()
        result = hub.collect("saveticker", self.root, {"saveticker": {"enabled": False}}, self.clock)
        self.assertEqual(result, 2)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(hub.build_market(self.root, self.clock)["saveticker"]["status"], "unavailable")

    def test_builder_telegram_and_degraded_sources(self):
        hub.write_json(self.root / "latest_snapshot.json", {
            "timestamp": hub.iso(self.clock), "status": "running", "count": 1,
            "items": [{"date_utc": hub.iso(self.clock)}]})
        result = hub.build_market(self.root, self.clock)
        self.assertEqual(result["telegram"]["item_count"], 1)
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(all(v is None for v in result["signals"].values()))

    def test_unchanged_does_not_rewrite(self):
        path = self.root / "output.json"
        self.assertTrue(hub.write_json(path, {"timestamp": "one", "status": "ok"}))
        self.assertFalse(hub.write_json(path, {"timestamp": "two", "status": "ok"}))

    def test_secrets_decoded_and_malformed_json(self):
        with patch.dict(os.environ, {"EXAMPLE_TOKEN": 'test-quote-"-secret'}):
            with self.assertRaises(hub.DataError):
                hub.secret_scan(json.loads('{"title":"test-quote-\\\"-secret"}'))
        path = self.root / "broken.json"
        path.write_text('{"a":NaN}')
        with self.assertRaises(hub.DataError):
            hub.read_json(path)


if __name__ == "__main__":
    unittest.main()
