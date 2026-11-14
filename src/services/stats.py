"""Daily send statistics from the send history (pure functions, no I/O).

Every send attempt is one history row; this classifies rows into outcomes
and aggregates them per local day and per alert type. ``queued`` rows are
not final — the outbox later records the same SMS as delivered or expired —
so they are counted on their own and never as sent or failed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

OUTCOMES = ("sent", "blocked", "duplicate", "failed", "queued")


def classify(success: bool, detail: str | None) -> str:
    d = detail or ""
    if success:
        return "sent"
    if d.startswith("Rate limit"):
        return "blocked"
    if d.startswith("Duplicate"):
        return "duplicate"
    if d.startswith("queued"):
        return "queued"
    return "failed"  # router error, expired in the outbox, …


def daily_stats(rows: list[dict], days: int, tz: str, today: date | None = None) -> dict:
    """``rows``: dicts with ts (aware), success, detail, segments, type, source, priority."""
    zone = ZoneInfo(tz)
    today = today or datetime.now(zone).date()
    first = today - timedelta(days=days - 1)

    per_day = {first + timedelta(days=i): dict.fromkeys(OUTCOMES, 0) | {"segments": 0}
               for i in range(days)}
    per_type: dict[str, dict] = defaultdict(lambda: dict.fromkeys(OUTCOMES, 0) | {"segments": 0})
    unknown_segments = 0

    for r in rows:
        day = r["ts"].astimezone(zone).date()
        if day not in per_day:
            continue
        outcome = classify(r["success"], r.get("detail"))
        bucket_day, bucket_type = per_day[day], per_type[r.get("type") or ""]
        bucket_day[outcome] += 1
        bucket_type[outcome] += 1
        if outcome == "sent":
            seg = r.get("segments")
            if isinstance(seg, int):
                bucket_day["segments"] += seg
                bucket_type["segments"] += seg
            else:
                unknown_segments += 1  # sent before segments were recorded (< 1.6.0)

    totals = dict.fromkeys(OUTCOMES, 0) | {"segments": 0}
    for v in per_day.values():
        for k in totals:
            totals[k] += v[k]

    types = sorted(
        ({"type": t or None, **v} for t, v in per_type.items()),
        key=lambda x: (x["sent"] + x["blocked"] + x["failed"], x["segments"]), reverse=True,
    )
    return {
        "days": days,
        "timezone": tz,
        "per_day": [{"date": d.isoformat(), **v} for d, v in per_day.items()],
        "types": types,
        "totals": totals,
        "unknown_segments": unknown_segments,
    }
