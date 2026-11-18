"""Health file for the Telegram collector, and the staleness check the watchdog uses."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_MAX_AGE_MINUTES = 40
FUTURE_TOLERANCE = timedelta(minutes=5)


def _parse(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def load(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def scrub(text: str, secrets) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:300]


def write_health(path: Path, *, now: datetime, trigger: str, error: str | None,
                 items_count: int | None, drive: dict | None = None,
                 links: dict | None = None) -> dict:
    previous = load(path)
    health = {
        "last_run_utc": now.astimezone(timezone.utc).isoformat(),
        "last_success_utc": (now.astimezone(timezone.utc).isoformat() if error is None
                             else previous.get("last_success_utc")),
        "trigger": trigger,
        "error": error,
        "items_count": items_count if items_count is not None else previous.get("items_count"),
        "drive": drive or {"configured": False},
        "links": links or {},
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(health, ensure_ascii=False, sort_keys=True, indent=1),
                   encoding="utf-8")
    tmp.replace(path)
    return health


def is_stale(health: dict, now: datetime,
             max_age: timedelta = timedelta(minutes=DEFAULT_MAX_AGE_MINUTES)) -> tuple[bool, str]:
    """Missing, unparsable, too old or implausibly future => stale."""
    last = _parse(health.get("last_success_utc"))
    if last is None:
        return True, "no last_success_utc"
    age = now - last
    if age < -FUTURE_TOLERANCE:
        return True, "last_success_utc is in the future"
    if age > max_age:
        return True, f"last success {int(age.total_seconds() // 60)} minutes ago"
    return False, f"last success {max(int(age.total_seconds() // 60), 0)} minutes ago"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("health_file", type=Path)
    parser.add_argument("--max-age-minutes", type=int, default=DEFAULT_MAX_AGE_MINUTES)
    args = parser.parse_args()
    stale, reason = is_stale(load(args.health_file), datetime.now(timezone.utc),
                             timedelta(minutes=args.max_age_minutes))
    print(f"stale={str(stale).lower()} ({reason})")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"stale={str(stale).lower()}\nreason={reason}\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
