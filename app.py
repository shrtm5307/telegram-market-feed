
import os
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

feed = deque(maxlen=MAX_ITEMS)
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
app = FastAPI(title="Telegram Market Feed")

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

async def backfill():
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

@client.on(events.NewMessage)
async def on_new_message(event):
    try:
        chat = await event.get_chat()
        if isinstance(chat, Channel):
            item = msg_to_dict(chat, event.message)
            feed.append(item)
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
