"""Archive Telegram photos/PDFs/documents to Google Drive.

The public repository only ever receives ``media_index.json``: Drive file id,
name, size, SHA-256 and status. File bodies and extracted PDF text stay in
Drive. Nothing here may stop the text feed from being produced.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from drive_archive import DriveAuthError, DriveClient

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
SKIPPED_MIME_PREFIXES = ("video/", "audio/")
KST = timezone(timedelta(hours=9))
# Statuses that are final: not retried on later runs.
FINAL = {"uploaded", "skipped_too_large", "skipped_type"}
TEXT_FINAL = {"uploaded", "no_text"}
ENTRY_KEYS = ("drive_file_id", "name", "size", "sha256", "status")


@dataclass
class Budget:
    max_files: int
    max_bytes: int
    deadline: float  # time.monotonic() value
    files: int = 0
    bytes: int = 0

    def exhausted(self) -> bool:
        return (self.files >= self.max_files or self.bytes >= self.max_bytes
                or time.monotonic() >= self.deadline)


@dataclass
class MediaTask:
    key: str                 # "<channel_id>:<message_id>:<n>"
    attachment: dict         # the feed attachment dict, enriched in place
    channel_id: int
    channel_name: str
    message_id: int
    date: datetime
    size_hint: int | None
    message: object = None   # Telethon message, only used by the downloader


@dataclass
class Report:
    uploaded_files: int = 0
    uploaded_bytes: int = 0
    failed: int = 0
    pending: int = 0
    skipped: int = 0
    error: str | None = None
    drive: dict = field(default_factory=dict)


def safe_segment(value: str, fallback: str, limit: int = 80) -> str:
    cleaned = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", " ", value or "")
    cleaned = " ".join(cleaned.split()).strip(". ")
    return cleaned[:limit].strip(". ") or fallback


def drive_name(message_id: int, attachment: dict) -> str:
    name = safe_segment(attachment.get("name", ""), "", 100)
    if not name:
        ext = mimetypes.guess_extension(attachment.get("mime_type", "")) or (
            ".jpg" if attachment.get("type") == "photo" else ".bin")
        name = f"{attachment.get('type', 'file')}{ext}"
    return f"{message_id}_{name}"


def folder_parts(date: datetime, channel_name: str, channel_id: int) -> list[str]:
    local = date.astimezone(KST)
    return [local.strftime("%Y-%m"), local.strftime("%d"),
            safe_segment(channel_name, f"channel_{channel_id}")]


def is_archivable(attachment: dict) -> str | None:
    """Return None if archivable, otherwise the skip reason status."""
    mime = (attachment.get("mime_type") or "").lower()
    if attachment.get("type") in ("photo", "pdf"):
        return None
    if mime.startswith(SKIPPED_MIME_PREFIXES):
        return "skipped_type"
    return None


def pdf_text(data: bytes, timeout: float = 30.0) -> str:
    """Extract text with poppler's pdftotext; '' if unavailable or on failure."""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "in.pdf"
        source.write_bytes(data)
        try:
            done = subprocess.run(
                ["pdftotext", "-layout", "-enc", "UTF-8", str(source), "-"],
                capture_output=True, timeout=timeout, check=True)
        except (OSError, subprocess.SubprocessError):
            return ""
    return done.stdout.decode("utf-8", "replace").strip()


# -- index ---------------------------------------------------------------------
class MediaIndex:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.files: dict[str, dict] = {}
        self._original: dict[str, dict] = {}
        if path and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                files = loaded.get("files") if isinstance(loaded, dict) else None
                if isinstance(files, dict):
                    self.files = {k: {f: v.get(f) for f in ENTRY_KEYS}
                                  for k, v in files.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                self.files = {}
        self._original = json.loads(json.dumps(self.files))

    def set(self, key: str, status: str, drive_file_id=None, name=None,
            size=None, sha256=None) -> None:
        self.files[key] = {"drive_file_id": drive_file_id, "name": name,
                           "size": size, "sha256": sha256, "status": status}

    def prune(self, live_keys: set[str]) -> None:
        """Keep every uploaded record; drop unresolved ones that left the snapshot."""
        for key in list(self.files):
            base = key.split(":text")[0]
            if base not in live_keys and self.files[key].get("status") != "uploaded":
                del self.files[key]

    def save(self, now: datetime) -> bool:
        if not self.path or self.files == self._original:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"version": 1, "generated_utc": now.isoformat(), "count": len(self.files),
             "files": self.files}, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)
        return True


def apply_entry(attachment: dict, entry: dict | None) -> None:
    """Add drive_file_id/size/sha256/status; keep type/name/mime_type untouched."""
    if not entry:
        return
    if entry.get("drive_file_id"):
        attachment["drive_file_id"] = entry["drive_file_id"]
    if isinstance(entry.get("size"), int):
        attachment["size"] = entry["size"]
    if entry.get("sha256"):
        attachment["sha256"] = entry["sha256"]
    attachment["status"] = entry.get("status") or "pending"


def _complete(index: MediaIndex, task: MediaTask) -> bool:
    entry = index.files.get(task.key)
    if not entry or entry.get("status") not in FINAL:
        return False
    if task.attachment.get("type") == "pdf" and entry["status"] == "uploaded":
        text = index.files.get(task.key + ":text")
        return bool(text and text.get("status") in TEXT_FINAL)
    return True


def _short_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


async def archive_media(tasks: list[MediaTask], index: MediaIndex,
                        drive: DriveClient | None, budget: Budget,
                        download: Callable[[MediaTask], Awaitable[bytes]],
                        extract_text: Callable[[bytes], str] = pdf_text) -> Report:
    """Process newest-first, bounded by ``budget``. Never raises."""
    report = Report()
    if drive is None:  # no credentials: leave the text feed exactly as it was
        for task in tasks:
            apply_entry(task.attachment, index.files.get(task.key))
        return report
    tasks = sorted(tasks, key=lambda t: t.date, reverse=True)
    stop_reason: str | None = None
    for task in tasks:
        attachment = task.attachment
        try:
            if _complete(index, task):
                apply_entry(attachment, index.files[task.key])
                continue
            skip = is_archivable(attachment)
            if skip or (task.size_hint or 0) > MAX_DOCUMENT_BYTES:
                index.set(task.key, skip or "skipped_too_large",
                          name=drive_name(task.message_id, attachment), size=task.size_hint)
                report.skipped += 1
                apply_entry(attachment, index.files[task.key])
                continue
            if stop_reason:
                apply_entry(attachment, index.files.get(task.key))
                continue
            if budget.exhausted():
                if task.key not in index.files:
                    index.set(task.key, "pending", name=drive_name(task.message_id, attachment))
                report.pending += 1
                apply_entry(attachment, index.files.get(task.key))
                continue
            await _archive_one(task, index, drive, budget, download, extract_text, report)
        except DriveAuthError as exc:
            stop_reason = report.error = f"drive auth: {_short_error(exc)}"
            report.failed += 1
            index.set(task.key, "failed", name=drive_name(task.message_id, attachment))
        except Exception as exc:  # one bad file must not affect the others
            report.failed += 1
            report.error = report.error or _short_error(exc)
            index.set(task.key, "failed", name=drive_name(task.message_id, attachment))
        apply_entry(attachment, index.files.get(task.key))
    return report


async def _archive_one(task, index, drive, budget, download, extract_text, report) -> None:
    attachment = task.attachment
    data = await download(task)
    if not data:
        raise RuntimeError("empty download")
    name = drive_name(task.message_id, attachment)
    if len(data) > MAX_DOCUMENT_BYTES:
        index.set(task.key, "skipped_too_large", name=name, size=len(data))
        report.skipped += 1
        return
    parent = drive.ensure_path(folder_parts(task.date, task.channel_name, task.channel_id))
    sha = hashlib.sha256(data).hexdigest()
    previous = index.files.get(task.key)
    if previous and previous.get("status") == "uploaded" and previous.get("sha256") == sha:
        file_id = previous["drive_file_id"]
    else:
        file_id = drive.upload(name, data, attachment.get("mime_type") or "", parent, task.key)
        report.uploaded_files += 1
        report.uploaded_bytes += len(data)
        budget.files += 1
        budget.bytes += len(data)
    index.set(task.key, "uploaded", file_id, name, len(data), sha)

    if attachment.get("type") == "pdf":
        text_key = task.key + ":text"
        text_entry = index.files.get(text_key)
        if text_entry and text_entry.get("status") in TEXT_FINAL:
            return
        text = extract_text(data)
        if not text:
            index.set(text_key, "no_text", name=name + ".txt")
            return
        body = text.encode("utf-8")
        try:
            text_id = drive.upload(name + ".txt", body, "text/plain", parent, text_key)
            index.set(text_key, "uploaded", text_id, name + ".txt", len(body),
                      hashlib.sha256(body).hexdigest())
            report.uploaded_files += 1
            report.uploaded_bytes += len(body)
            budget.bytes += len(body)
        except DriveAuthError:
            raise
        except Exception as exc:
            index.set(text_key, "failed", name=name + ".txt")
            report.error = report.error or _short_error(exc)
