from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def spread_allowed(
    ask: float,
    bid: float,
    *,
    max_spread_points: int,
    point: float,
) -> bool:
    """Spread wider than max_spread_points * point rejects the entry."""
    if max_spread_points <= 0:
        return True
    threshold = max_spread_points * point
    return (ask - bid) <= threshold + 1e-9


def margin_allowed(margin_required: float | None, margin_free: float | None) -> bool:
    """Reject when the trade would require more free margin than available."""
    if margin_required is None or margin_free is None:
        return False
    if margin_required < 0 or margin_free < 0:
        return False
    return margin_required <= margin_free + 1e-9


def load_news_events(path: Path) -> list[dict[str, Any]]:
    """Read [{start_utc: "YYYY-MM-DDTHH:MM", title: "..."}] from JSON file."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    events: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start_text = str(item.get("start_utc", "")).strip()
        try:
            start = datetime.fromisoformat(start_text)
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        events.append(
            {
                "start_msc": int(start.timestamp() * 1000),
                "title": str(item.get("title", "")).strip(),
            }
        )
    return events


def news_blocked(
    events: list[dict[str, Any]],
    *,
    now_msc: int,
    window_before_minutes: float,
    window_after_minutes: float,
) -> tuple[bool, str]:
    """Return (blocked, reason) when now falls inside an event window."""
    if window_before_minutes <= 0 and window_after_minutes <= 0:
        return False, ""
    before_ms = window_before_minutes * 60_000
    after_ms = window_after_minutes * 60_000
    for event in events:
        start = int(event["start_msc"])
        if start - before_ms <= now_msc <= start + after_ms:
            return True, event["title"]
    return False, ""
