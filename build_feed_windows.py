"""Build lightweight public Telegram feed windows from a sanitized snapshot."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


STALE_AFTER = timedelta(minutes=45)
FUTURE_TOLERANCE = timedelta(minutes=5)


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def build_window(snapshot: dict, hours: int, now: datetime | None = None) -> dict:
    if hours <= 0:
        raise ValueError("hours must be positive")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("items"), list):
        raise ValueError("invalid snapshot")

    generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    snapshot_time = parse_utc(snapshot["timestamp"])
    snapshot_status = snapshot.get("status")
    snapshot_age = generated - snapshot_time

    if snapshot_status not in {"backfilled", "running"}:
        freshness = "unavailable"
    elif snapshot_age > STALE_AFTER or snapshot_age < -FUTURE_TOLERANCE:
        freshness = "stale"
    else:
        freshness = "ok"

    cutoff = generated - timedelta(hours=hours)
    selected = []
    for item in snapshot["items"]:
        if not isinstance(item, dict):
            raise ValueError("invalid snapshot item")
        published = parse_utc(item.get("date_utc"))
        for field in ("channel_name", "message", "url"):
            if not isinstance(item.get(field), (str, type(None))):
                raise ValueError(f"invalid {field}")
        message = item.get("message") or ""
        if not message.strip():
            continue
        if cutoff <= published <= generated + FUTURE_TOLERANCE:
            selected.append((published, {
                "channel_name": item.get("channel_name") or "",
                "published_at": iso_utc(published),
                "message": message,
                "url": item.get("url") or "",
            }))

    selected.sort(key=lambda pair: pair[0], reverse=True)
    items = [item for _, item in selected]
    return {
        "generated_utc": iso_utc(generated),
        "window_hours": hours,
        "freshness_status": freshness,
        "latest_message_at": items[0]["published_at"] if items else None,
        "count": len(items),
        "items": items,
    }


def write_windows(source: Path, output_dir: Path, hours: tuple[int, ...] = (6, 24)) -> None:
    snapshot = json.loads(source.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    for window_hours in hours:
        output = output_dir / f"latest_{window_hours}h.json"
        temporary = output.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(build_window(snapshot, window_hours, now), ensure_ascii=False,
                       separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    write_windows(args.source, args.output_dir)


if __name__ == "__main__":
    main()
