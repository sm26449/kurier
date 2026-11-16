"""Send history: the last 100 attempts in memory, written through to
``sms_log`` when MySQL is configured, plus the read side (daily
statistics, search).

Iterating or indexing a ``SendHistory`` gives the in-memory entries,
most recent first. ``stats`` and ``search`` use MySQL when it is there,
else the in-memory entries (``stats`` then flags ``partial``).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.errors import describe_error
from src.models.schemas import SendHistoryEntry
from src.services.stats import classify, daily_stats
from src.storage import SendHistoryStore

logger = logging.getLogger(__name__)

RECENT = 100


class SendHistory:
    def __init__(self, store: SendHistoryStore | None, tz: str) -> None:
        self.store = store
        self.tz = tz
        self._recent: deque[SendHistoryEntry] = deque(maxlen=RECENT)
        if store is not None:
            try:
                self._recent.extend(store.load_recent(limit=RECENT))
                logger.info("sms history rehydrated: %d entries", len(self._recent))
            except Exception as e:
                logger.warning("history rehydrate failed: %s", describe_error(e))

    def __iter__(self) -> Iterator[SendHistoryEntry]:
        return iter(self._recent)

    def __len__(self) -> int:
        return len(self._recent)

    def __getitem__(self, i: int) -> SendHistoryEntry:
        return self._recent[i]

    @property
    def durable(self) -> bool:
        return self.store is not None

    def record(self, entry: SendHistoryEntry) -> None:
        """Newest first in memory; queued for MySQL (non-blocking)."""
        self._recent.appendleft(entry)
        if self.store is not None:
            self.store.record(entry)

    def _local_day_start(self, days: int) -> datetime:
        zone = ZoneInfo(self.tz)
        first = datetime.now(zone).date() - timedelta(days=days - 1)
        return datetime(first.year, first.month, first.day, tzinfo=zone).astimezone(timezone.utc)

    async def stats(self, days: int) -> dict:
        """Per-day outcomes and segments + per-type totals (MySQL; else the
        in-memory last 100, flagged ``partial``)."""
        since = self._local_day_start(days)
        if self.store is not None:
            rows = await asyncio.to_thread(self.store.rows_since, since)
            partial = False
        else:
            rows = [{"ts": e.timestamp, "success": e.success, "detail": e.detail,
                     "segments": e.segments, "type": e.type, "source": e.source,
                     "priority": e.priority} for e in self._recent if e.timestamp >= since]
            partial = True
        return {**daily_stats(rows, days, self.tz), "partial": partial}

    async def search(self, *, q: str | None = None, type: str | None = None,
                     source: str | None = None, status: str | None = None,
                     priority: str | None = None, since: datetime | None = None,
                     until: datetime | None = None, limit: int = 50,
                     offset: int = 0) -> list[SendHistoryEntry]:
        if self.store is not None:
            return await asyncio.to_thread(
                self.store.search, q=q, type=type, source=source, status=status,
                priority=priority, since=since, until=until, limit=limit, offset=offset)
        ql = (q or "").lower()
        hits = [e for e in self._recent
                if (not ql or ql in e.message.lower() or ql in e.number)
                and (not type or e.type == type) and (not source or e.source == source)
                and (not status or classify(e.success, e.detail) == status)
                and (not priority or e.priority == priority)
                and (not since or e.timestamp >= since) and (not until or e.timestamp < until)]
        return hits[offset:offset + limit]
