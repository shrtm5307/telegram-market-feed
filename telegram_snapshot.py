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
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

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
        )
        if value
    )


def contains_secret(item: dict, secrets: Iterable[str]) -> bool:
    public_text = " ".join(
        str(item.get(key) or "")
        for key in ("channel_name", "channel_username", "message", "url")
    )
    return any(secret in public_text for secret in secrets)


async def collect(output: Path) -> None:
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
    successful_channels = 0
    channel_errors = 0
    unsafe_items = 0
    advertisement_items = 0

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
                successful_channels += 1
            except Exception:
                channel_errors += 1
    finally:
        await client.disconnect()

    if successful_channels == 0:
        raise RuntimeError("No Telegram channels could be read.")

    ordered = sorted(
        items.values(),
        key=lambda item: item["date_utc"],
        reverse=True,
    )[:max_items]
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
        f"latest_message_at={latest}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    asyncio.run(collect(args.output))


if __name__ == "__main__":
    main()
