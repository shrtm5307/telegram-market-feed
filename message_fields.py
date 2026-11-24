"""Turn a Telegram channel message into public feed fields.

Shared by the GitHub Actions snapshot collector (telegram_snapshot.py) and the
live server (app.py) so both apply the same rule: keep every channel post
except advertisements. Service events (pins, title changes) are not posts and
are skipped. A post whose text is empty and whose media cannot be shown as
text (poll, story, paid media, an unknown type) is still kept, with an
attachment entry that says what kind of post it was.

Media types are matched by class name so this module does not import
telethon and works with the test stubs.

"Rich" channel posts (long text with inline photos, headings, lists and
tables) carry their content in message.rich_message instead of
message.message. Telethon releases older than 1.45 (TL layer < 229) do not
know that field, so such posts arrive with empty text; message_text() turns
the rich blocks into plain text.
"""

from __future__ import annotations

import re

AD_PREFIX_RE = re.compile(r"^(?:광고|협찬|제휴|sponsored|advertisement|promoted)(?:\s|:|：|#|-)", re.IGNORECASE)
AD_MARKERS = (
    "광고문의",
    "유료광고",
    "#광고",
    "#협찬",
    "#sponsored",
    "telegram ads",
)

# Attachment types whose bytes the media archiver may download.
DOWNLOADABLE_TYPES = frozenset({"photo", "pdf", "document"})

# Media that has no file to download; the value is the attachment "type".
_NON_FILE_MEDIA = {
    "MessageMediaGeo": "geo",
    "MessageMediaGeoLive": "geo",
    "MessageMediaVenue": "venue",
    "MessageMediaContact": "contact",
    "MessageMediaDice": "dice",
    "MessageMediaGame": "game",
    "MessageMediaInvoice": "invoice",
    "MessageMediaStory": "story",
    "MessageMediaPaidMedia": "paid_media",
    "MessageMediaGiveaway": "giveaway",
    "MessageMediaGiveawayResults": "giveaway",
    "MessageMediaToDo": "todo",
    "MessageMediaVideoStream": "video_stream",
    "MessageMediaUnsupported": "unsupported",
}

MAX_NAME_CHARS = 1000


def is_advertisement(text: str | None) -> bool:
    normalized = " ".join((text or "").lower().split())
    return bool(AD_PREFIX_RE.search(normalized)) or any(
        marker in normalized for marker in AD_MARKERS
    )


def _plain(value) -> str:
    """str for plain strings, .text for TextWithEntities, '' otherwise."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", "")
    return text if isinstance(text, str) else ""


def _poll_summary(media) -> str:
    poll = getattr(media, "poll", None)
    lines = [_plain(getattr(poll, "question", None))]
    for answer in getattr(poll, "answers", None) or ():
        text = _plain(getattr(answer, "text", None))
        if text:
            lines.append(f"- {text}")
    return "\n".join(line for line in lines if line)[:MAX_NAME_CHARS]


def _todo_summary(media) -> str:
    todo = getattr(media, "todo", None)
    lines = [_plain(getattr(todo, "title", None))]
    for entry in getattr(todo, "list", None) or ():
        text = _plain(getattr(entry, "title", None))
        if text:
            lines.append(f"- {text}")
    return "\n".join(line for line in lines if line)[:MAX_NAME_CHARS]


def media_info(message) -> list[dict]:
    media = getattr(message, "media", None)
    if media is None:
        return []
    kind = type(media).__name__
    if kind == "MessageMediaEmpty":
        return []
    if kind == "MessageMediaPhoto":
        return [{"type": "photo", "name": "", "mime_type": "image/jpeg"}]
    if kind == "MessageMediaDocument":
        document = getattr(media, "document", None)
        mime_type = getattr(document, "mime_type", "") or ""
        name = ""
        for attribute in getattr(document, "attributes", ()) or ():
            candidate = getattr(attribute, "file_name", None)
            if candidate:
                name = candidate
                break
        pdf = mime_type == "application/pdf" or name.lower().endswith(".pdf")
        return [{"type": "pdf" if pdf else "document", "name": name, "mime_type": mime_type}]
    if kind == "MessageMediaWebPage":
        page = getattr(media, "webpage", None)
        title = _plain(getattr(page, "title", None))
        return [{"type": "webpage", "name": title[:MAX_NAME_CHARS], "mime_type": ""}]
    if kind == "MessageMediaPoll":
        return [{"type": "poll", "name": _poll_summary(media), "mime_type": ""}]
    if kind == "MessageMediaToDo":
        return [{"type": "todo", "name": _todo_summary(media), "mime_type": ""}]
    if kind in _NON_FILE_MEDIA:
        # Contact/geo details stay out of the public feed; only the kind is kept.
        return [{"type": _NON_FILE_MEDIA[kind], "name": "", "mime_type": ""}]
    return [{"type": "other", "name": kind[:100], "mime_type": ""}]


MAX_TEXT_CHARS = 20000
_PHOTO_BLOCKS = ("PageBlockPhoto", "PageBlockVideo", "PageBlockCollage", "PageBlockSlideshow")


def _rich_plain(value) -> str:
    """Flatten a RichText tree (TextPlain/TextBold/TextConcat/TextUrl...)."""
    if value is None or isinstance(value, (bool, int, float)):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_rich_plain(item) for item in value)
    texts = getattr(value, "texts", None)
    if texts is not None:
        return "".join(_rich_plain(item) for item in texts)
    return _rich_plain(getattr(value, "text", None))


def _rich_item(item) -> str:
    if getattr(item, "text", None) is not None:
        return _rich_plain(item.text)
    if getattr(item, "blocks", None) is not None:
        return _rich_blocks(item.blocks)
    return _rich_block(item)


def _rich_block(block) -> str:
    parts = []
    kind = type(block).__name__
    if kind in _PHOTO_BLOCKS:
        parts.append("[사진]" if kind != "PageBlockVideo" else "[영상]")
    for attr in ("title", "text"):
        text = _rich_plain(getattr(block, attr, None))
        if text:
            parts.append(text)
    for item in getattr(block, "items", None) or ():
        text = _rich_item(item)
        if text:
            parts.append(text if type(item).__name__.startswith("PageBlock") else f"- {text}")
    for row in getattr(block, "rows", None) or ():
        cells = [_rich_plain(getattr(cell, "text", None)) for cell in getattr(row, "cells", None) or ()]
        if any(cells):
            parts.append(" | ".join(cells))
    if getattr(block, "blocks", None):
        parts.append(_rich_blocks(block.blocks))
    caption = getattr(block, "caption", None)
    if caption is not None:
        text = _rich_plain(getattr(caption, "text", None))
        if text:
            parts.append(text)
    return "\n".join(part for part in parts if part)


def _rich_blocks(blocks) -> str:
    return "\n".join(text for text in (_rich_block(b) for b in blocks or ()) if text)


def message_text(message) -> str:
    """Plain text of a post: message.message, else the rich-message blocks."""
    text = getattr(message, "message", None) or ""
    if not text.strip():
        rich = getattr(message, "rich_message", None)
        if rich is not None:
            text = _rich_blocks(getattr(rich, "blocks", None))
    return text[:MAX_TEXT_CHARS]


def rich_attachments(message) -> list[dict]:
    """Summarize photos/documents embedded in a rich post (not downloaded)."""
    rich = getattr(message, "rich_message", None)
    if rich is None:
        return []
    out = []
    photos = len(getattr(rich, "photos", None) or ())
    documents = len(getattr(rich, "documents", None) or ())
    if photos:
        out.append({"type": "rich_photo", "name": f"{photos} photo(s)", "mime_type": ""})
    if documents:
        out.append({"type": "rich_document", "name": f"{documents} document(s)", "mime_type": ""})
    return out


def classify(message) -> tuple[str, list[dict]]:
    """Return (reason, attachments). reason is "keep" or why it is dropped."""
    if getattr(message, "action", None) is not None:
        return "service", []
    if not getattr(message, "date", None):
        return "no_date", []
    text = message_text(message)
    if is_advertisement(text):
        return "advertisement", []
    attachments = media_info(message) + rich_attachments(message)
    if not text.strip() and not attachments:
        # Nothing we can render, but the post exists: keep it visibly.
        attachments = [{"type": "empty", "name": "", "mime_type": ""}]
    return "keep", attachments
