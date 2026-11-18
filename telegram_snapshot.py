"""Collect a sanitized Telegram snapshot for GitHub Actions.

This is intentionally a one-shot collector. GitHub Actions starts a fresh
runner for each scheduled run, so the Telegram StringSession is used to
authenticate without writing a session file to the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from drive_archive import DriveClient, credentials_from_env
from health import scrub, write_health
from link_resolver import LinkCache, extract_urls, resolve_links
from media_archive import Budget, MediaIndex, MediaTask, archive_media
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel, MessageMediaDocument, MessageMediaPhoto



AD_PREFIX_RE = re.compile(r"^(?:광고|협찬|제휴|sponsored|advertisement|promoted)(?:\s|:|：|#|-)", re.IGNORECASE)
AD_MARKERS = (
    "광고문의",
    "유료광고",
    "#광고",
    "#협찬",
    "#sponsored",
    "telegram ads",
)


def is_advertisement(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return bool(AD_PREFIX_RE.search(normalized)) or any(
        marker in normalized for marker in AD_MARKERS
    )


def media_info(message) -> list[dict]:
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return [{"type": "photo", "name": "", "mime_type": "image/jpeg"}]
    if isinstance(media, MessageMediaDocument):
        document = getattr(media, "document", None)
        mime_type = getattr(document, "mime_type", "") or ""
        name = ""
        for attribute in getattr(document, "attributes", ()) or ():
            candidate = getattr(attribute, "file_name", None)
            if candidate:
                name = candidate
                break
        kind = "pdf" if mime_type == "application/pdf" or name.lower().endswith(".pdf") else "document"
        return [{"type": kind, "name": name, "mime_type": mime_type}]
    return []


def media_size(message) -> int | None:
    """Best-effort size in bytes before downloading (None if unknown)."""
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaDocument):
        size = getattr(getattr(media, "document", None), "size", None)
        return int(size) if isinstance(size, int) else None
    if isinstance(media, MessageMediaPhoto):
        sizes = [getattr(s, "size", None) for s in getattr(getattr(media, "photo", None), "sizes", ()) or ()]
        sizes = [s for s in sizes if isinstance(s, int)]
        return max(sizes) if sizes else None
    return None


def entity_urls(message) -> list[str]:
    return [e.url for e in (getattr(message, "entities", None) or ())
            if isinstance(getattr(e, "url", None), str)]


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def configured_secrets() -> tuple[str, ...]:
    return tuple(
        value
        for value in (
            os.environ.get("TELEGRAM_API_HASH", ""),
            os.environ.get("TELEGRAM_SESSION_STRING", ""),
            os.environ.get("FEED_TOKEN", ""),
            os.environ.get("GOOGLE_CLIENT_ID", ""),
            os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            os.environ.get("GOOGLE_REFRESH_TOKEN", ""),
        )
        if value
    )


def contains_secret(item: dict, secrets: Iterable[str]) -> bool:
    public_text = " ".join(
        str(item.get(key) or "")
        for key in ("channel_name", "channel_username", "message", "url")
    )
    for link in item.get("links") or ():
        public_text += " " + " ".join(str(link.get(k) or "") for k in ("url", "final_url", "title"))
    return any(secret in public_text for secret in secrets)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


async def collect(output: Path, media_index_path: Path, link_cache_path: Path) -> dict:
    """Write the snapshot and return stats for the health file."""
    try:
        api_id = int(os.environ["TELEGRAM_API_ID"])
        api_hash = os.environ["TELEGRAM_API_HASH"]
        session_string = os.environ["TELEGRAM_SESSION_STRING"]
    except (KeyError, ValueError) as exc:
        raise RuntimeError(
            "Missing TELEGRAM_API_ID, TELEGRAM_API_HASH, or "
            "TELEGRAM_SESSION_STRING GitHub secret."
        ) from exc

    per_channel = int(os.getenv("BACKFILL_PER_CHANNEL", "100"))
    max_items = int(os.getenv("SNAPSHOT_MAX_ITEMS", "500"))
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    items: dict[tuple[int, int], dict] = {}
    messages: dict[tuple[int, int], object] = {}
    successful_channels = 0
    channel_errors = 0
    unsafe_items = 0
    advertisement_items = 0
    media_stats = {"configured": False}
    index = MediaIndex(media_index_path)

    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized.")

        dialogs = await client.get_dialogs()
        for dialog in dialogs:
            channel = dialog.entity
            if not isinstance(channel, Channel):
                continue
            try:
                async for message in client.iter_messages(channel, limit=per_channel):
                    text = message.message or ""
                    attachments = media_info(message)
                    if (not text.strip() and not attachments) or not message.date:
                        continue
                    if is_advertisement(text):
                        advertisement_items += 1
                        continue
                    username = getattr(channel, "username", None) or ""
                    item = {
                        "message_id": message.id,
                        "channel_id": channel.id,
                        "channel_name": getattr(channel, "title", "") or "",
                        "channel_username": username,
                        "date_utc": utc_iso(message.date),
                        "message": text,
                        "attachments": attachments,
                        "url": (
                            f"https://t.me/{username}/{message.id}"
                            if username
                            else ""
                        ),
                    }
                    if contains_secret(item, configured_secrets()):
                        unsafe_items += 1
                        continue
                    items[(channel.id, message.id)] = item
                    messages[(channel.id, message.id)] = message
                successful_channels += 1
            except Exception:
                channel_errors += 1

        if successful_channels == 0:
            raise RuntimeError("No Telegram channels could be read.")

        ordered = sorted(
            items.values(),
            key=lambda item: item["date_utc"],
            reverse=True,
        )[:max_items]

        # Media archive: strictly best effort, bounded, never fatal.
        try:
            media_stats = await archive_snapshot_media(ordered, messages, client, index)
        except Exception as exc:
            media_stats = {"configured": False, "error": scrub(
                f"{type(exc).__name__}: {exc}", configured_secrets())}
    finally:
        await client.disconnect()

    link_stats = {}
    try:
        link_stats = enrich_links(ordered, messages, link_cache_path)
    except Exception as exc:
        link_stats = {"error": scrub(f"{type(exc).__name__}: {exc}", configured_secrets())}

    # A secret that surfaced through links/titles must not be published.
    safe = [item for item in ordered if not contains_secret(item, configured_secrets())]
    unsafe_items += len(ordered) - len(safe)
    ordered = safe

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "count": len(ordered),
        "status": "running",
        "items": ordered,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(output)

    latest = ordered[0]["date_utc"] if ordered else None
    print(
        f"Collected {len(ordered)} items from {successful_channels} channels; "
        f"channel_errors={channel_errors}; unsafe_skipped={unsafe_items}; "
        f"advertisement_skipped={advertisement_items}; "
        f"latest_message_at={latest}; media={media_stats}; links={link_stats}"
    )
    return {"items_count": len(ordered), "drive": media_stats, "links": link_stats}


async def archive_snapshot_media(ordered: list[dict], messages: dict, client,
                                 index: MediaIndex) -> dict:
    tasks: list[MediaTask] = []
    for item in ordered:
        message = messages.get((item["channel_id"], item["message_id"]))
        for n, attachment in enumerate(item["attachments"]):
            tasks.append(MediaTask(
                key=f"{item['channel_id']}:{item['message_id']}:{n}",
                attachment=attachment, channel_id=item["channel_id"],
                channel_name=item["channel_name"], message_id=item["message_id"],
                date=datetime.fromisoformat(item["date_utc"]),
                size_hint=media_size(message), message=message))
    index.prune({t.key for t in tasks})

    creds = credentials_from_env()
    drive = DriveClient(*creds) if creds else None
    budget = Budget(
        max_files=env_int("MEDIA_MAX_FILES_PER_RUN", 20),
        max_bytes=env_int("MEDIA_MAX_MB_PER_RUN", 150) * 1024 * 1024,
        deadline=time.monotonic() + env_int("MEDIA_MAX_SECONDS_PER_RUN", 240))

    async def download(task: MediaTask) -> bytes:
        data = await client.download_media(task.message, file=bytes)
        return data or b""

    report = await archive_media(tasks, index, drive, budget, download)
    # Always (re)write the index: it only touches disk if something changed.
    index.save(datetime.now(timezone.utc))
    stats = {
        "configured": drive is not None,
        "uploaded_files": report.uploaded_files, "uploaded_bytes": report.uploaded_bytes,
        "failed": report.failed, "pending": report.pending, "skipped": report.skipped,
        "error": scrub(report.error, configured_secrets()) if report.error else None,
        "usage_bytes": None, "limit_bytes": None,
    }
    if drive is not None:
        try:
            stats.update(drive.storage_quota())
        except Exception:
            pass
    return stats


def enrich_links(ordered: list[dict], messages: dict, cache_path: Path) -> dict:
    cache = LinkCache(cache_path)
    now = datetime.now(timezone.utc)
    budget = {"lookups": env_int("LINK_MAX_LOOKUPS_PER_RUN", 40),
              "deadline": time.monotonic() + env_int("LINK_MAX_SECONDS_PER_RUN", 60)}
    seen: set[str] = set()
    for item in ordered:
        message = messages.get((item["channel_id"], item["message_id"]))
        urls = extract_urls(item["message"], entity_urls(message) if message else None)
        item["links"] = resolve_links(urls, cache, budget, now)
        seen.update(urls)
    cache.prune(seen, now)
    cache.save()
    statuses: dict[str, int] = {}
    for item in ordered:
        for link in item["links"]:
            statuses[link["status"]] = statuses.get(link["status"], 0) + 1
    return {"statuses": statuses, "lookups_used": env_int("LINK_MAX_LOOKUPS_PER_RUN", 40) - budget["lookups"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--media-index", type=Path)
    parser.add_argument("--link-cache", type=Path)
    parser.add_argument("--health", type=Path)
    args = parser.parse_args()
    root = args.output.parent
    media_index = args.media_index or root / "data/telegram/media_index.json"
    link_cache = args.link_cache or root / "data/telegram/link_cache.json"
    health_path = args.health or root / "data/health/telegram.json"
    trigger = os.getenv("COLLECT_TRIGGER") or os.getenv("GITHUB_EVENT_NAME") or "manual"
    try:
        stats = asyncio.run(collect(args.output, media_index, link_cache))
    except Exception as exc:
        message = scrub(f"{type(exc).__name__}: {exc}", configured_secrets())
        write_health(health_path, now=datetime.now(timezone.utc), trigger=trigger,
                     error=message, items_count=None)
        raise SystemExit(f"Collection failed: {message}")
    write_health(health_path, now=datetime.now(timezone.utc), trigger=trigger,
                 error=None, items_count=stats["items_count"],
                 drive=stats["drive"], links=stats["links"])


if __name__ == "__main__":
    main()
