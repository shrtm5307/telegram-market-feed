import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import drive_archive
import link_resolver
import media_archive


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
                "message": "Market update", "attachments": [],
                "url": "https://t.me/news/42",
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


NOW = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)  # 12:00 KST


class FakeDriveHttp:
    """Records requests; emulates just enough of the Drive API."""

    def __init__(self, fail_upload=False, reject_token=False):
        self.calls, self.folders, self.files = [], {}, {}
        self.fail_upload, self.reject_token = fail_upload, reject_token

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        if url == drive_archive.TOKEN_URL:
            if self.reject_token:
                return 400, {}, b'{"error":"invalid_grant"}'
            return 200, {}, b'{"access_token":"at","expires_in":3600}'
        if "/upload/drive/v3/files" in url:
            if self.fail_upload:
                return 500, {}, b"{}"
            return 200, {"Location": "https://upload.example/session"}, b""
        if url == "https://upload.example/session":
            file_id = f"file{len(self.files) + 1}"
            self.files[file_id] = body
            return 200, {}, json.dumps({"id": file_id}).encode()
        if method == "GET" and "/files?" in url:
            return 200, {}, b'{"files":[]}'
        if method == "POST" and url.endswith("/files?fields=id"):
            folder_id = f"folder{len(self.folders) + 1}"
            self.folders[folder_id] = json.loads(body)
            return 200, {}, json.dumps({"id": folder_id}).encode()
        if "/about" in url:
            return 200, {}, b'{"storageQuota":{"usage":"123","limit":"1000"}}'
        return 404, {}, b"{}"


def make_task(message_id=1, kind="pdf", name="r.pdf", mime="application/pdf", size=10):
    attachment = {"type": kind, "name": name, "mime_type": mime}
    return media_archive.MediaTask(
        key=f"5:{message_id}:0", attachment=attachment, channel_id=5,
        channel_name="Alpha/Research: 채널", message_id=message_id, date=NOW,
        size_hint=size)


def budget(files=10, size=10 ** 9, seconds=60):
    return media_archive.Budget(files, size, time.monotonic() + seconds)


class DriveClientTests(unittest.TestCase):
    def test_credentials_require_all_three_secrets(self):
        full = {"GOOGLE_CLIENT_ID": "a", "GOOGLE_CLIENT_SECRET": "b", "GOOGLE_REFRESH_TOKEN": "c"}
        self.assertEqual(drive_archive.credentials_from_env(full), ("a", "b", "c"))
        for missing in full:
            partial = {k: v for k, v in full.items() if k != missing}
            self.assertIsNone(drive_archive.credentials_from_env(partial))
        self.assertIsNone(drive_archive.credentials_from_env({}))

    def test_folder_tree_is_created_under_app_root_and_upload_is_tagged(self):
        http = FakeDriveHttp()
        client = drive_archive.DriveClient("id", "secret", "r-token-xyz", http=http)
        leaf = client.ensure_path(["2026-11", "18", "채널"])
        self.assertEqual([f["name"] for f in http.folders.values()],
                         ["Telegram_Media_Archive", "2026-11", "18", "채널"])
        self.assertEqual(http.folders["folder2"]["parents"], ["folder1"])
        self.assertEqual(leaf, "folder4")
        self.assertEqual(client.ensure_path(["2026-11", "18", "채널"]), leaf)  # cached
        self.assertEqual(client.upload("a.txt", b"hello", "text/plain", leaf, "k1"), "file1")
        self.assertEqual(http.files["file1"], b"hello")
        start = next(c for c in http.calls if "uploadType=resumable" in c[1])
        self.assertEqual(json.loads(start[3])["appProperties"], {"tg_key": "k1"})
        self.assertEqual(client.storage_quota(), {"usage_bytes": 123, "limit_bytes": 1000})

    def test_rejected_refresh_token_is_an_auth_error_without_leaking(self):
        client = drive_archive.DriveClient("id", "secret", "r-token-xyz", http=FakeDriveHttp(reject_token=True))
        with self.assertRaises(drive_archive.DriveAuthError) as caught:
            client.ensure_folder("x")
        self.assertNotIn("r-token-xyz", str(caught.exception))


class MediaArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.index_path = Path(self.temp.name) / "data/telegram/media_index.json"
        self.http = FakeDriveHttp()
        self.drive = drive_archive.DriveClient("id", "secret", "r-token-xyz", http=self.http)

    def run_archive(self, tasks, drive="default", bud=None, data=b"%PDF-1.4 body", text="추출 텍스트"):
        index = media_archive.MediaIndex(self.index_path)
        downloads = []

        async def download(task):
            downloads.append(task.key)
            return data
        report = asyncio.run(media_archive.archive_media(
            tasks, index, self.drive if drive == "default" else drive,
            bud or budget(), download, extract_text=lambda _: text))
        index.save(NOW)
        return index, report, downloads

    def test_pdf_uploads_file_and_text_and_index_has_only_public_fields(self):
        task = make_task()
        index, report, _ = self.run_archive([task])
        entry = index.files["5:1:0"]
        self.assertEqual(set(entry), {"drive_file_id", "name", "size", "sha256", "status"})
        self.assertEqual(entry["status"], "uploaded")
        self.assertEqual(entry["name"], "1_r.pdf")
        self.assertEqual(entry["size"], len(b"%PDF-1.4 body"))
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(index.files["5:1:0:text"]["name"], "1_r.pdf.txt")
        self.assertEqual(report.uploaded_files, 2)
        # Feed attachment keeps its three strings and gains the new fields.
        self.assertEqual(task.attachment["type"], "pdf")
        self.assertEqual(task.attachment["name"], "r.pdf")
        self.assertEqual(task.attachment["mime_type"], "application/pdf")
        self.assertEqual(task.attachment["status"], "uploaded")
        self.assertEqual(task.attachment["drive_file_id"], entry["drive_file_id"])
        # Text lands in the same folder as the PDF; folder is KST date based and sanitized.
        names = [f["name"] for f in self.http.folders.values()]
        self.assertEqual(names, ["Telegram_Media_Archive", "2026-11", "18", "Alpha Research 채널"])
        # The public index never holds bodies or extracted text.
        raw = self.index_path.read_text(encoding="utf-8")
        self.assertNotIn("추출 텍스트", raw)
        self.assertNotIn("%PDF", raw)

    def test_second_run_does_not_download_or_upload_again(self):
        self.run_archive([make_task()])
        uploads = len(self.http.files)
        task = make_task()
        _, report, downloads = self.run_archive([task])
        self.assertEqual(downloads, [])
        self.assertEqual(len(self.http.files), uploads)
        self.assertEqual(task.attachment["status"], "uploaded")

    def test_no_credentials_means_no_drive_work_and_untouched_attachments(self):
        task = make_task()
        index, report, downloads = self.run_archive([task], drive=None)
        self.assertEqual(downloads, [])
        self.assertEqual(task.attachment, {"type": "pdf", "name": "r.pdf",
                                           "mime_type": "application/pdf"})
        self.assertEqual(index.files, {})
        self.assertFalse(self.index_path.exists())

    def test_upload_failure_is_recorded_and_retried_next_run(self):
        self.http.fail_upload = True
        task = make_task()
        index, report, _ = self.run_archive([task])
        self.assertEqual(index.files["5:1:0"]["status"], "failed")
        self.assertEqual(task.attachment["status"], "failed")
        self.assertEqual(report.failed, 1)
        self.http.fail_upload = False
        task = make_task()
        index, report, downloads = self.run_archive([task])
        self.assertEqual(downloads, ["5:1:0"])
        self.assertEqual(index.files["5:1:0"]["status"], "uploaded")

    def test_download_error_does_not_stop_other_files(self):
        index = media_archive.MediaIndex(self.index_path)
        tasks = [make_task(1), make_task(2, "photo", "", "image/jpeg")]
        tasks[1].date = NOW.replace(hour=4)  # processed first (newest)

        async def download(task):
            if task.message_id == 2:
                raise ConnectionError("telegram hiccup")
            return b"data"
        report = asyncio.run(media_archive.archive_media(
            tasks, index, self.drive, budget(), download, extract_text=lambda _: ""))
        self.assertEqual(index.files["5:2:0"]["status"], "failed")
        self.assertEqual(index.files["5:1:0"]["status"], "uploaded")
        self.assertEqual(index.files["5:1:0:text"]["status"], "no_text")

    def test_per_run_file_cap_leaves_pending_for_next_run(self):
        tasks = [make_task(i, "photo", "", "image/jpeg") for i in (1, 2, 3)]
        for i, task in enumerate(tasks):
            task.date = NOW.replace(minute=i)
        index, report, downloads = self.run_archive([*tasks], bud=budget(files=1))
        statuses = sorted(e["status"] for e in index.files.values())
        self.assertEqual(statuses, ["pending", "pending", "uploaded"])
        self.assertEqual(len(downloads), 1)
        self.assertEqual(report.pending, 2)
        self.assertEqual(index.files["5:3:0"]["status"], "uploaded")  # newest first

    def test_time_and_byte_budgets_stop_work(self):
        index, report, downloads = self.run_archive([make_task()], bud=budget(seconds=-1))
        self.assertEqual(downloads, [])
        self.assertEqual(index.files["5:1:0"]["status"], "pending")
        index, report, downloads = self.run_archive([make_task(2)], bud=budget(size=0))
        self.assertEqual(downloads, [])

    def test_large_and_unsupported_documents_are_skipped_not_downloaded(self):
        big = make_task(1, "document", "x.zip", "application/zip", size=51 * 1024 * 1024)
        video = make_task(2, "document", "v.mp4", "video/mp4", size=1000)
        audio = make_task(4, "rich_document", "a.ogg", "audio/ogg", size=1000)
        index, report, downloads = self.run_archive([big, video, audio])
        self.assertEqual(downloads, [])
        self.assertEqual(index.files["5:1:0"]["status"], "skipped_too_large")
        self.assertEqual(index.files["5:2:0"]["status"], "skipped_type")
        self.assertEqual(index.files["5:4:0"]["status"], "skipped_type")
        ok = make_task(3, "document", "x.xlsx", "application/vnd.ms-excel", size=1000)
        index, _, downloads = self.run_archive([ok])
        self.assertEqual(downloads, ["5:3:0"])
        self.assertEqual(index.files["5:3:0"]["status"], "uploaded")

    def test_auth_failure_stops_run_but_returns_report(self):
        self.http.reject_token = True
        tasks = [make_task(1), make_task(2)]
        tasks[1].date = NOW.replace(hour=4)
        index, report, downloads = self.run_archive(tasks)
        self.assertIn("drive auth", report.error)
        self.assertEqual(report.failed, 1)  # second task is not even attempted
        self.assertNotIn("r-token-xyz", report.error)

    def test_prune_keeps_uploaded_and_drops_stale_unresolved(self):
        index = media_archive.MediaIndex(self.index_path)
        index.set("1:1:0", "uploaded", "f", "n", 1, "h")
        index.set("1:2:0", "failed")
        index.set("1:3:0", "pending")
        index.prune({"1:3:0"})
        self.assertEqual(sorted(index.files), ["1:1:0", "1:3:0"])

    def test_corrupt_index_is_treated_as_empty(self):
        self.index_path.parent.mkdir(parents=True)
        self.index_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(media_archive.MediaIndex(self.index_path).files, {})

    def test_unchanged_index_is_not_rewritten(self):
        self.run_archive([make_task()])
        before = self.index_path.stat().st_mtime_ns
        index = media_archive.MediaIndex(self.index_path)
        self.assertFalse(index.save(NOW))
        self.assertEqual(self.index_path.stat().st_mtime_ns, before)

    def test_file_names_are_safe(self):
        name = media_archive.drive_name(9, {"type": "pdf", "name": "../../etc/pa:ss*wd.pdf", "mime_type": ""})
        self.assertNotRegex(name, r"[/\\:*]")
        self.assertTrue(name.startswith("9_"))
        self.assertEqual(media_archive.drive_name(9, {"type": "photo", "name": "", "mime_type": "image/jpeg"})[:8], "9_photo.")


    def test_rich_photo_archived_and_rich_pdf_gets_text(self):
        photo = make_task(7, "rich_photo", "photo_1.jpg", "image/jpeg", size=1000)
        pdf = make_task(8, "rich_document", "note.pdf", "application/pdf", size=1000)
        index, report, downloads = self.run_archive([photo, pdf])
        self.assertEqual(sorted(downloads), ["5:7:0", "5:8:0"])
        self.assertEqual(index.files["5:7:0"]["status"], "uploaded")
        self.assertEqual(index.files["5:8:0:text"]["status"], "uploaded")


class LinkResolverTests(unittest.TestCase):
    class Response:
        def __init__(self, body=b"", content_type="text/html; charset=utf-8"):
            self.body = body
            self.headers = {"Content-Type": content_type}

        def read(self, n):
            return self.body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def __init__(self, script):
            self.script, self.seen = script, []

        def open(self, request, timeout):
            self.seen.append((request.full_url, dict(request.header_items()), timeout))
            result = self.script[request.full_url]
            if isinstance(result, Exception):
                raise result
            return result

    @staticmethod
    def redirect(location, code=301):
        return urllib.error.HTTPError("u", code, "r", {"Location": location}, None)

    @staticmethod
    def status(code):
        return urllib.error.HTTPError("u", code, "e", {}, None)

    def resolve(self, script, url="https://bit.ly/x"):
        opener = self.Opener(script)
        return link_resolver.resolve(url, opener, host_check=lambda h: True), opener

    def test_short_link_is_expanded_with_title(self):
        page = self.Response(b"<html><title> A &amp; B \n news </title>")
        result, opener = self.resolve({
            "https://bit.ly/x": self.redirect("https://t.co/y"),
            "https://t.co/y": self.redirect("/real?a=1#frag"),
            "https://t.co/real?a=1": page})
        self.assertEqual(result, {"url": "https://bit.ly/x", "final_url": "https://t.co/real?a=1",
                                  "status": "ok", "title": "A & B news"})
        self.assertTrue(all(h["User-agent"].startswith("telegram-market-feed")
                            for _, h, _ in opener.seen))
        self.assertTrue(all(t <= 5.0 for _, _, t in opener.seen))

    def test_blocked_sites_are_reported_not_bypassed(self):
        for code in (401, 403, 429):
            result, opener = self.resolve({"https://bit.ly/x": self.status(code)})
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["title"], "")
            self.assertEqual(len(opener.seen), 1)  # no retry with other headers

    def test_not_found_timeout_error_and_loops(self):
        self.assertEqual(self.resolve({"https://bit.ly/x": self.status(404)})[0]["status"], "not_found")
        self.assertEqual(self.resolve({"https://bit.ly/x": self.status(500)})[0]["status"], "error")
        self.assertEqual(self.resolve({"https://bit.ly/x": TimeoutError()})[0]["status"], "timeout")
        self.assertEqual(self.resolve({"https://bit.ly/x": OSError("dns")})[0]["status"], "error")
        loop = {"https://bit.ly/x": self.redirect("https://bit.ly/x")}
        self.assertEqual(self.resolve(loop)[0]["status"], "error")

    def test_private_hosts_and_odd_schemes_are_never_fetched(self):
        opener = self.Opener({})
        for url in ("http://127.0.0.1/x", "ftp://example.com/f", "https://u:p@example.com/"):
            result = link_resolver.resolve(url, opener)
            self.assertEqual(result["status"], "invalid", url)
        self.assertEqual(opener.seen, [])

    def test_overall_deadline_is_five_seconds(self):
        ticks = iter([0.0, 0.1, 6.0, 6.0, 6.0])
        opener = self.Opener({"https://a.example/": self.redirect("https://b.example/")})
        result = link_resolver.resolve("https://a.example/", opener, host_check=lambda h: True,
                                       clock=lambda: next(ticks))
        self.assertEqual(result["status"], "timeout")

    def test_extract_urls_skips_telegram_and_dedupes(self):
        text = "see https://bit.ly/a, https://t.me/x/1 and (https://bit.ly/a) https://e.com/b."
        self.assertEqual(link_resolver.extract_urls(text, ["https://e.com/c"]),
                         ["https://bit.ly/a", "https://e.com/b", "https://e.com/c"])
        self.assertEqual(len(link_resolver.extract_urls(" ".join(f"https://e.com/{i}" for i in range(9)))), 3)

    def test_cache_budget_and_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = link_resolver.LinkCache(Path(tmp) / "c.json")
            calls = []

            def resolver(url):
                calls.append(url)
                return {"url": url, "final_url": url + "/f", "status": "ok", "title": "T"}
            bud = {"lookups": 1, "deadline": time.monotonic() + 60}
            first = link_resolver.resolve_links(["https://a.e/1", "https://a.e/2"], cache, bud, NOW, resolver)
            self.assertEqual([r["status"] for r in first], ["ok", "pending"])
            cache.save()
            reloaded = link_resolver.LinkCache(Path(tmp) / "c.json")
            bud = {"lookups": 5, "deadline": time.monotonic() + 60}
            again = link_resolver.resolve_links(["https://a.e/1", "https://a.e/2"], reloaded, bud, NOW, resolver)
            self.assertEqual(calls, ["https://a.e/1", "https://a.e/2"])  # /1 served from cache
            self.assertEqual(again[0]["title"], "T")
            later = NOW.replace(year=2027)
            self.assertIsNone(reloaded.get("https://a.e/1", later))  # expired


def import_collector():
    """telegram_snapshot needs telethon; stub it only if it is not installed."""
    try:
        import telethon  # noqa: F401
    except ImportError:
        telethon = types.ModuleType("telethon")
        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def on(self, *args, **kwargs):
                return lambda func: func
        telethon.TelegramClient = FakeClient
        telethon.events = types.SimpleNamespace(NewMessage=object())
        sessions = types.ModuleType("telethon.sessions")
        sessions.StringSession = FakeClient
        tl = types.ModuleType("telethon.tl")
        types_mod = types.ModuleType("telethon.tl.types")
        for name in ("Channel", "MessageMediaDocument", "MessageMediaPhoto"):
            setattr(types_mod, name, type(name, (), {}))
        sys.modules.update({"telethon": telethon, "telethon.sessions": sessions,
                            "telethon.tl": tl, "telethon.tl.types": types_mod})
    return importlib.import_module("telegram_snapshot")


class CollectorHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = import_collector()

    def test_media_info_strings_are_unchanged(self):
        message = types.SimpleNamespace(media=self.module.MessageMediaPhoto())
        self.assertEqual(self.module.media_info(message),
                         [{"type": "photo", "name": "", "mime_type": "image/jpeg"}])

    def test_google_credentials_count_as_secrets(self):
        env = {"GOOGLE_CLIENT_ID": "cid-123", "GOOGLE_CLIENT_SECRET": "csecret-456",
               "GOOGLE_REFRESH_TOKEN": "1//refresh-789"}
        with patch.dict(os.environ, env):
            secrets = self.module.configured_secrets()
            for value in env.values():
                self.assertIn(value, secrets)
                item = {"message": "x", "links": [{"url": "u", "final_url": "https://e.com/?t=" + value,
                                                   "status": "ok", "title": ""}]}
                self.assertTrue(self.module.contains_secret(item, secrets))

    def test_links_enrichment_never_raises_for_cache_problems_and_sets_schema(self):
        item = {"channel_id": 1, "message_id": 2, "message": "no links here", "attachments": []}
        with tempfile.TemporaryDirectory() as tmp:
            stats = self.module.enrich_links([item], {}, Path(tmp) / "c.json")
        self.assertEqual(item["links"], [])
        self.assertEqual(stats["statuses"], {})


class MessageFieldsTests(unittest.TestCase):
    """Every channel post except ads must be kept (message_fields.classify)."""

    @classmethod
    def setUpClass(cls):
        cls.mf = importlib.import_module("message_fields")

    @staticmethod
    def media(kind, **fields):
        return type(kind, (), {})() if not fields else type(kind, (), fields)()

    def msg(self, text="", media=None, action=None, date=NOW):
        return types.SimpleNamespace(id=7, message=text, media=media, action=action, date=date)

    def test_text_post_kept_without_attachments(self):
        self.assertEqual(self.mf.classify(self.msg("hello")), ("keep", []))

    def test_advertisement_and_service_are_dropped(self):
        self.assertEqual(self.mf.classify(self.msg("광고: 신규 채널"))[0], "advertisement")
        self.assertEqual(self.mf.classify(self.msg(action=object()))[0], "service")
        self.assertEqual(self.mf.classify(self.msg("x", date=None))[0], "no_date")

    def test_empty_text_with_unknown_media_is_kept_and_labelled(self):
        for kind, label in (("MessageMediaStory", "story"),
                            ("MessageMediaPaidMedia", "paid_media"),
                            ("MessageMediaUnsupported", "unsupported")):
            reason, attachments = self.mf.classify(self.msg("", self.media(kind)))
            self.assertEqual(reason, "keep")
            self.assertEqual(attachments, [{"type": label, "name": "", "mime_type": ""}])
        reason, attachments = self.mf.classify(self.msg("", self.media("MessageMediaBrandNew")))
        self.assertEqual(attachments[0]["type"], "other")
        self.assertEqual(attachments[0]["name"], "MessageMediaBrandNew")

    def test_completely_empty_post_is_kept_as_empty(self):
        reason, attachments = self.mf.classify(self.msg("", None))
        self.assertEqual(reason, "keep")
        self.assertEqual(attachments, [{"type": "empty", "name": "", "mime_type": ""}])

    def test_poll_question_and_answers_become_the_name(self):
        text = lambda t: types.SimpleNamespace(text=t)  # TextWithEntities
        poll = types.SimpleNamespace(question=text("Huang returns?"),
                                     answers=[types.SimpleNamespace(text=text("Yes")),
                                              types.SimpleNamespace(text="No")])
        media = type("MessageMediaPoll", (), {"poll": poll})()
        _, attachments = self.mf.classify(self.msg("", media))
        self.assertEqual(attachments, [{"type": "poll", "name": "Huang returns?\n- Yes\n- No",
                                        "mime_type": ""}])

    def test_webpage_title_and_contact_privacy(self):
        page = types.SimpleNamespace(title="JPM note", url="https://example.com")
        media = type("MessageMediaWebPage", (), {"webpage": page})()
        self.assertEqual(self.mf.media_info(self.msg("", media)),
                         [{"type": "webpage", "name": "JPM note", "mime_type": ""}])
        contact = type("MessageMediaContact", (), {"phone_number": "+82 10"})()
        self.assertEqual(self.mf.media_info(self.msg("", contact)),
                         [{"type": "contact", "name": "", "mime_type": ""}])

    def test_new_types_pass_the_public_feed_schema(self):
        build_feed_windows = importlib.import_module("build_feed_windows")
        for attachment in ({"type": "poll", "name": "q", "mime_type": ""},
                           {"type": "empty", "name": "", "mime_type": ""}):
            self.assertEqual(build_feed_windows.clean_attachment(attachment), attachment)

    def test_only_files_are_downloadable(self):
        self.assertEqual(self.mf.DOWNLOADABLE_TYPES,
                         {"photo", "pdf", "document", "rich_photo", "rich_document"})

    def test_collect_keeps_all_but_ads_and_reports_skips(self):
        collector = import_collector()

        class FakeChannel:
            id, username, title = 1, "chan", "Chan"

        def m(i, text="", media=None, action=None):
            return types.SimpleNamespace(id=i, message=text, media=media, action=action,
                                         date=NOW, entities=None)

        posts = [m(5, "plain text"), m(4, "", self.media("MessageMediaStory")),
                 m(3, "광고: buy"), m(2, action=object()), m(1, "", None)]

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def is_user_authorized(self):
                return True

            async def get_dialogs(self):
                return [types.SimpleNamespace(entity=FakeChannel())]

            async def iter_messages(self, channel, limit):
                for post in posts:
                    yield post

        env = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "hash-0f9e8d",
               "TELEGRAM_SESSION_STRING": "session-1a2b3c"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env), \
                patch.object(collector, "TelegramClient", FakeClient), \
                patch.object(collector, "StringSession", lambda s: s), \
                patch.object(collector, "Channel", FakeChannel):
            out = Path(tmp) / "snap.json"
            stats = asyncio.run(collector.collect(out, Path(tmp) / "mi.json", Path(tmp) / "lc.json"))
            snap = json.loads(out.read_text(encoding="utf-8"))
        kept = {item["message_id"]: item["attachments"] for item in snap["items"]}
        self.assertEqual(set(kept), {5, 4, 1})
        self.assertEqual(kept[4][0]["type"], "story")
        self.assertEqual(kept[1][0]["type"], "empty")
        self.assertEqual(stats["collect"]["skipped_counts"], {"advertisement": 1, "service": 1})
        self.assertEqual(stats["collect"]["skipped_posts"], [])

    def test_rich_message_text_is_flattened(self):
        def _mk(kind, fields):
            o = type(kind, (), {})()
            o.__dict__.update(fields)
            return o

        plain = lambda t: _mk("TextPlain", {"text": t})
        rich = _mk("RichMessage", {
            "blocks": [
                _mk("PageBlockParagraph", {"text": _mk("TextConcat", {"texts": [
                    _mk("TextBold", {"text": plain("JPM:")}), plain(" capex up")]})}),
                _mk("PageBlockPhoto", {"photo_id": 1, "caption": _mk("PageCaption", {
                    "text": plain("chart"), "credit": plain("")})}),
                _mk("PageBlockList", {"items": [_mk("PageListItemText", {"text": plain("2026 +105%")})]}),
                _mk("PageBlockTable", {"title": plain("T"), "rows": [_mk("PageTableRow", {"cells": [
                    _mk("PageTableCell", {"text": plain("a")}), _mk("PageTableCell", {"text": plain("b")})]})]}),
            ],
            "photos": [object(), object()], "documents": []})
        msg = types.SimpleNamespace(id=9, message="", media=None, action=None, date=NOW, rich_message=rich)
        self.assertEqual(self.mf.message_text(msg),
                         "JPM: capex up\n[사진]\nchart\n- 2026 +105%\nT\na | b")
        reason, attachments = self.mf.classify(msg)
        self.assertEqual(reason, "keep")
        self.assertEqual(attachments, [
            {"type": "rich_photo", "name": "photo_1.jpg", "mime_type": "image/jpeg"},
            {"type": "rich_photo", "name": "photo_2.jpg", "mime_type": "image/jpeg"}])
        # Plain text still wins when present; missing field is harmless.
        self.assertEqual(self.mf.message_text(types.SimpleNamespace(message="hi", rich_message=rich)), "hi")
        self.assertEqual(self.mf.message_text(types.SimpleNamespace(message=None)), "")

    def test_rich_text_is_full_and_photo_markers_numbered(self):
        def _mk(kind, fields):
            o = type(kind, (), {})()
            o.__dict__.update(fields)
            return o
        plain = lambda t: _mk("TextPlain", {"text": t})
        long_para = "가" * 30000
        rich = _mk("RichMessage", {
            "blocks": [_mk("PageBlockParagraph", {"text": plain(long_para)}),
                       _mk("PageBlockPhoto", {"photo_id": 22, "caption": None}),
                       _mk("PageBlockPhoto", {"photo_id": 11, "caption": None})],
            "photos": [types.SimpleNamespace(id=11), types.SimpleNamespace(id=22)],
            "documents": []})
        msg = types.SimpleNamespace(id=1, message="", media=None, action=None, date=NOW, rich_message=rich)
        text = self.mf.message_text(msg)
        self.assertTrue(text.startswith(long_para))          # not cut at 20,000
        self.assertTrue(text.endswith("[사진 2]\n[사진 1]"))  # matches photo_2.jpg / photo_1.jpg

    def test_download_targets_map_rich_files_in_order(self):
        collector = import_collector()
        p1, p2, doc = object(), object(), types.SimpleNamespace(size=42, mime_type="application/pdf",
                                                                attributes=())
        rich = types.SimpleNamespace(photos=[p1, p2], documents=[doc])
        msg = types.SimpleNamespace(media=None, rich_message=rich)
        attachments = [{"type": "webpage", "name": "x", "mime_type": ""},
                       {"type": "rich_photo", "name": "photo_1.jpg", "mime_type": "image/jpeg"},
                       {"type": "rich_photo", "name": "photo_2.jpg", "mime_type": "image/jpeg"},
                       {"type": "rich_document", "name": "document_1", "mime_type": "application/pdf"}]
        targets = collector.download_targets(msg, attachments)
        self.assertEqual(sorted(targets), [1, 2, 3])
        self.assertIs(targets[1][0], p1)
        self.assertIs(targets[2][0], p2)
        self.assertEqual(targets[3], (doc, 42))
        # Normal media still downloads the message itself.
        plain = types.SimpleNamespace(media=None)
        self.assertEqual(collector.download_targets(plain, [{"type": "photo", "name": "", "mime_type": "image/jpeg"}]),
                         {0: (plain, None)})

    def test_secret_in_attachment_name_fails_closed(self):
        collector = import_collector()
        item = {"message": "", "attachments": [{"type": "poll", "name": "tok-SECRET-1",
                                                "mime_type": ""}]}
        self.assertTrue(collector.contains_secret(item, ("tok-SECRET-1",)))


if __name__ == "__main__":
    unittest.main()
