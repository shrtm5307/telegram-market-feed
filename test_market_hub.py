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
        return [{"id": "n1", "title": "Memory demand", "site": "Example",
                 "time": int(self.clock.timestamp() * 1000), "tickers": ["mu"],
                 "url": "https://example.com/news/n1?utm_source=test", "_category": "watchlist"}]

    def chain(self):
        return {"symbol": "MU", "as_of": hub.iso(self.clock), "spot_price": 100,
                "contracts": [{"expiration": hub.iso(self.clock + timedelta(days=7)),
                    "strike": 100, "side": side, "volume": volume,
                    "open_interest": oi, "implied_volatility": .4,
                    "bid": 2, "ask": 3} for side, volume, oi in
                    (("call", 300, 100), ("put", 150, 200))]}

    def test_news_dedup_id_url_and_story(self):
        data = self.news()
        duplicate = copy.deepcopy(data[0])
        duplicate.update(id="n2", url="https://example.com/news/n2")
        data += [copy.deepcopy(data[0]), duplicate]
        result = hub.parse_tickertick(data, self.clock)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["window_hours"], 24)
        self.assertEqual(result["items"][0]["tickers"], ["MU"])
        self.assertNotIn("?", result["items"][0]["url"])
        self.assertIsNone(result["items"][0]["summary"])

    def test_news_bad_schema_empty_stale_timezone(self):
        for mutation in (lambda x: x.clear(),
                         lambda x: x[0].update(time="bad"),
                         lambda x: x[0].update(url="http://example.com/news/n1"),
                         lambda x: x[0].update(id=None)):
            data = self.news()
            mutation(data)
            with self.assertRaises(hub.DataError):
                hub.parse_tickertick(data, self.clock)

    def test_news_retains_only_latest_24_hours(self):
        data = self.news()
        old = copy.deepcopy(data[0])
        old.update(id="old", title="Old story", url="https://example.com/news/old",
                   time=int((self.clock - timedelta(hours=24, seconds=1)).timestamp() * 1000))
        data.append(old)
        result = hub.parse_tickertick(data, self.clock)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["news_id"], "n1")

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
        path = self.root / "data/news/latest_news.json"
        hub.write_json(path, hub.parse_tickertick(self.news(), self.clock))
        original = path.read_bytes()
        result = hub.collect("tickertick", self.root, {"tickertick": {"enabled": False}}, self.clock)
        self.assertEqual(result, 2)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(hub.build_market(self.root, self.clock)["news"]["status"], "unavailable")

    @patch("market_hub.fetch_tickertick")
    def test_authorized_collection_writes_24_hour_snapshot(self, fetch):
        fetch.return_value = self.news()
        config = {"tickertick": {
            "enabled": True,
            "automated_collection_permitted": True,
            "public_redistribution_permitted": True,
            "policy_evidence_url": "https://github.com/hczhu/TickerTick-API#terms-of-use"}}
        self.assertEqual(hub.collect("tickertick", self.root, config, self.clock), 0)
        data = hub.read_json(self.root / "data/news/latest_news.json")
        self.assertEqual(data["window_hours"], 24)
        self.assertEqual(data["count"], 1)
        self.assertEqual(hub.read_json(self.root / "data/health/tickertick.json")["status"], "ok")

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
