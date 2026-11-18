"""Expand shortened links and record final_url / status / title.

Politeness rules: honest User-Agent, 5 second hard limit per link, no cookies,
no header spoofing. Sites that refuse automated clients are not worked around;
only their ``status`` is recorded.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

USER_AGENT = "telegram-market-feed-linkcheck/1.0 (+public feed; no scraping)"
LINK_TIMEOUT = 5.0
MAX_REDIRECTS = 5
READ_LIMIT = 65536
MAX_URL_LENGTH = 2000
SKIP_HOSTS = {"t.me", "telegram.me", "telegram.org"}
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
BLOCKED_CODES = {401, 403, 407, 429, 451, 999}

# status -> how long a cached answer stays valid
CACHE_TTL = {
    "ok": timedelta(days=30),
    "not_found": timedelta(days=7),
    "blocked": timedelta(days=7),
    "invalid": timedelta(days=30),
    "timeout": timedelta(hours=1),
    "error": timedelta(hours=1),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


def extract_urls(text: str, extra: list[str] | None = None, limit: int = 3) -> list[str]:
    seen: list[str] = []
    for raw in list(URL_RE.findall(text or "")) + list(extra or []):
        url = raw.rstrip(".,;:!?)]}>»”’")
        host = (urlsplit(url).hostname or "").lower()
        if not host or host in SKIP_HOSTS or url in seen or len(url) > MAX_URL_LENGTH:
            continue
        seen.append(url)
        if len(seen) >= limit:
            break
    return seen


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def _title(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    try:
        text = body.decode(encoding, "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    found = TITLE_RE.search(text)
    return " ".join(html.unescape(found.group(1)).split())[:200] if found else ""


def _result(url, final, status, title=""):
    return {"url": url, "final_url": final, "status": status, "title": title}


def resolve(url: str, opener=None, host_check=_public_host,
            clock=time.monotonic) -> dict:
    """Resolve one URL within LINK_TIMEOUT seconds. Never raises."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    deadline = clock() + LINK_TIMEOUT
    current = urldefrag(url)[0]
    try:
        for _ in range(MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username or parts.password):
                return _result(url, current, "invalid")
            remaining = deadline - clock()
            if remaining <= 0:
                return _result(url, current, "timeout")
            if not host_check(parts.hostname):
                return _result(url, current, "invalid")
            request = urllib.request.Request(current, headers={
                "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5",
                "Range": f"bytes=0-{READ_LIMIT - 1}"})
            try:
                with opener.open(request, timeout=max(remaining, 0.1)) as response:
                    content_type = response.headers.get("Content-Type", "")
                    body = response.read(READ_LIMIT) if "html" in content_type.lower() else b""
                    if clock() > deadline:
                        return _result(url, current, "timeout")
                    return _result(url, current, "ok", _title(body, content_type))
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400 and exc.headers.get("Location"):
                    current = urldefrag(urljoin(current, exc.headers["Location"]))[0]
                    continue
                if exc.code in BLOCKED_CODES:
                    return _result(url, current, "blocked")
                if exc.code in (404, 410):
                    return _result(url, current, "not_found")
                return _result(url, current, "error")
        return _result(url, current, "error")
    except (TimeoutError, socket.timeout):
        return _result(url, current, "timeout")
    except Exception:  # network failures must never break feed generation
        return _result(url, current, "error")


class LinkCache:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.entries: dict[str, dict] = {}
        self.dirty = False
        if path and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("links"), dict):
                    self.entries = {k: v for k, v in loaded["links"].items()
                                    if isinstance(v, dict)}
            except (OSError, ValueError):
                self.entries = {}

    def get(self, url: str, now: datetime) -> dict | None:
        entry = self.entries.get(url)
        if not entry:
            return None
        try:
            checked = datetime.fromisoformat(entry["checked_utc"])
            ttl = CACHE_TTL.get(entry["status"], timedelta(hours=1))
        except (KeyError, ValueError):
            return None
        return entry if now - checked < ttl else None

    def put(self, result: dict, now: datetime) -> None:
        self.entries[result["url"]] = {
            "final_url": result["final_url"], "status": result["status"],
            "title": result["title"], "checked_utc": now.isoformat()}
        self.dirty = True

    def prune(self, keep: set[str], now: datetime, max_age=timedelta(days=45)) -> None:
        for url in list(self.entries):
            try:
                age = now - datetime.fromisoformat(self.entries[url]["checked_utc"])
            except (KeyError, ValueError):
                age = max_age
            if url not in keep and age >= max_age:
                del self.entries[url]
                self.dirty = True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "links": self.entries},
                                  ensure_ascii=False, sort_keys=True, indent=0),
                       encoding="utf-8")
        tmp.replace(self.path)


def resolve_links(urls: list[str], cache: LinkCache, budget: dict,
                  now: datetime | None = None, resolver=resolve) -> list[dict]:
    """Return links[] records. ``budget`` = {"lookups": int, "deadline": monotonic}."""
    now = now or datetime.now(timezone.utc)
    records = []
    for url in urls:
        cached = cache.get(url, now)
        if cached:
            records.append(_result(url, cached["final_url"], cached["status"], cached["title"]))
            continue
        if budget["lookups"] <= 0 or time.monotonic() >= budget["deadline"]:
            records.append(_result(url, "", "pending"))
            continue
        budget["lookups"] -= 1
        result = resolver(url)
        cache.put(result, now)
        records.append(result)
    return records
