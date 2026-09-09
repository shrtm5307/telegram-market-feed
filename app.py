
import os
import json
import asyncio
from collections import deque
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException, Query
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel
import uvicorn

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_STRING = os.environ["TELEGRAM_SESSION_STRING"]
FEED_TOKEN = os.environ["FEED_TOKEN"]

MAX_ITEMS = int(os.getenv("MAX_ITEMS", "5000"))
BACKFILL_PER_CHANNEL = int(os.getenv("BACKFILL_PER_CHANNEL", "100"))

# Sanitized, external-safe recent-feed snapshot. Written to disk so it can be
# consumed by external tooling (e.g. ChatGPT) for market-trend analysis
# without ever exposing credentials.
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "snapshot.json")
SNAPSHOT_MAX_ITEMS = int(os.getenv("SNAPSHOT_MAX_ITEMS", "500"))

feed = deque(maxlen=MAX_ITEMS)
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
app = FastAPI(title="Telegram Market Feed")

message_count = 0
status = "initializing"

def msg_to_dict(chat, msg):
    username = getattr(chat, "username", None)
    url = f"https://t.me/{username}/{msg.id}" if username else ""
    dt = msg.date
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return {
        "message_id": msg.id,
        "channel_id": chat.id,
        "channel_name": getattr(chat, "title", ""),
        "channel_username": username or "",
        "date_utc": dt.astimezone(timezone.utc).isoformat() if dt else "",
        "message": msg.message or "",
        "url": url,
    }

# The exact set of fields that are ever allowed to leave the process via the
# snapshot file. This is an explicit allow-list so that no credential or
# internal field can accidentally leak into external-facing output.
SNAPSHOT_SAFE_FIELDS = (
    "message_id",
    "channel_id",
    "channel_name",
    "channel_username",
    "date_utc",
    "message",
    "url",
)

def sanitize_item(item):
    """Return a copy of item containing only the whitelisted safe fields.

    This guarantees that no secrets (API_ID, API_HASH, SESSION_STRING,
    tokens, etc.) can ever end up in the snapshot, regardless of what other
    fields might exist on the source item.
    """
    return {field: item.get(field, "") for field in SNAPSHOT_SAFE_FIELDS}

def write_snapshot():
    """Atomically write a sanitized snapshot of the most recent messages.

    The snapshot is safe to expose externally (e.g. for ChatGPT analysis)
    because it only ever contains the whitelisted safe fields and no
    credentials or internal configuration.
    """
    try:
        recent_items = list(feed)[-SNAPSHOT_MAX_ITEMS:]
        sanitized_items = [sanitize_item(item) for item in recent_items]
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_count": message_count,
            "status": status,
            "snapshot_items_count": len(sanitized_items),
            "items": sanitized_items,
        }
        tmp_path = f"{SNAPSHOT_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp_path, SNAPSHOT_PATH)
    except Exception:
        pass

async def backfill():
    global message_count, status
    dialogs = await client.get_dialogs()
    channels = [d.entity for d in dialogs if isinstance(d.entity, Channel)]
    items = []
    for ch in channels:
        try:
            async for m in client.iter_messages(ch, limit=BACKFILL_PER_CHANNEL):
                if m.message:
                    items.append(msg_to_dict(ch, m))
        except Exception:
            pass
    items.sort(key=lambda x: x["date_utc"])
    seen = set()
    for item in items:
        key = (item["channel_id"], item["message_id"])
        if key not in seen:
            seen.add(key)
            feed.append(item)
            message_count += 1
    status = "backfilled"
    write_snapshot()

@client.on(events.NewMessage)
async def on_new_message(event):
    global message_count, status
    try:
        chat = await event.get_chat()
        if isinstance(chat, Channel):
            item = msg_to_dict(chat, event.message)
            feed.append(item)
            message_count += 1
            status = "running"
            write_snapshot()
    except Exception:
        pass

@app.on_event("startup")
async def startup():
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session string is not authorized.")
    await backfill()
    asyncio.create_task(client.run_until_disconnected())

@app.get("/")
async def root():
    return {"ok": True, "service": "telegram-market-feed", "items": len(feed)}

@app.get("/feed")
async def get_feed(
    token: str,
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(500, ge=1, le=2000),
):
    if token != FEED_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for item in reversed(feed):
        try:
            dt = datetime.fromisoformat(item["date_utc"])
        except Exception:
            continue
        if dt >= cutoff:
            out.append(item)
        if len(out) >= limit:
            break
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "hours": hours,
        "count": len(out),
        "items": out,
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
