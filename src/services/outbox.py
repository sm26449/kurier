"""Durable outbox: SMS that could not reach the router are retried later.

An item enters the outbox when the live send fails (router unreachable,
timeout, rejection) or when older items are already waiting (keeps order
and answers callers fast during an outage). A background worker retries
with exponential backoff until the SMS is delivered or older than the
TTL, after which it is marked ``expired`` — a 6-hour-old alert is noise.

Pending items are mirrored in memory, so lookups on the send path
(``has_pending``, ``pending_count``) never touch MySQL. The store is
write-through; if it fails, the item still lives in memory and is
retried, just not across a restart.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.config import Settings
from src.errors import describe_error
from src.gsm7 import segments_used
from src.models.schemas import SendHistoryEntry
from src.services.teltonika import TeltonikaClient, TeltonikaError
from src.storage import MemoryOutboxStore, MySQLOutboxStore, OutboxItem

logger = logging.getLogger(__name__)

OutboxStore = MemoryOutboxStore | MySQLOutboxStore
Listener = Callable[[OutboxItem], Awaitable[None]]


def _is_rejection(e: Exception) -> bool:
    """The router answered and refused this particular message."""
    return isinstance(e, TeltonikaError) and str(e).startswith("Router rejected")


class Outbox:
    def __init__(
        self,
        client: TeltonikaClient,
        store: OutboxStore,
        settings: Settings,
        record_history: Callable[[SendHistoryEntry], None],
    ) -> None:
        self.client = client
        self.store = store
        self.settings = settings
        self._record_history = record_history
        self._pending: dict[int, OutboxItem] = {}
        self.counters: Counter[str] = Counter()  # delivered / expired / cancelled
        self._listeners: list[Listener] = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Ids for items the store failed to persist (negative, never clash).
        self._local_ids = itertools.count(-1, -1)

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        try:
            for item in await asyncio.to_thread(self.store.load_pending):
                self._pending[item.id] = item
        except Exception as e:
            logger.error("outbox: loading pending items failed: %s", describe_error(e))
        logger.info("outbox ready (%s store): %d pending",
                    "MySQL" if self.store.durable else "in-memory", len(self._pending))
        self._task = asyncio.create_task(self._run())
        if self._pending:
            self._wake.set()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def add_listener(self, listener: Listener) -> None:
        """Called with the item after it was delivered or expired."""
        self._listeners.append(listener)

    # ── Send-path API (synchronous lookups) ───────────────────

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def oldest_age_seconds(self, now: datetime | None = None) -> int:
        """Age of the oldest undelivered item; 0 when the outbox is empty."""
        if not self._pending:
            return 0
        now = now or datetime.now(timezone.utc)
        oldest = min(i.created_at for i in self._pending.values())
        return max(0, int((now - oldest).total_seconds()))

    def has_pending(self, dedup_key: str) -> bool:
        return any(i.dedup_key == dedup_key for i in self._pending.values())

    def pending(self) -> list[OutboxItem]:
        return [replace(i) for i in self._ordered()]

    async def enqueue(self, number: str, message: str, priority: str,
                      dedup_key: str, reason: str, attempted: bool, *,
                      ttl_seconds: int | None = None, source: str | None = None,
                      type: str | None = None) -> OutboxItem:
        """Add an item. ``attempted`` = the live send just failed, so the
        first retry waits for the backoff instead of firing immediately.
        ``ttl_seconds`` overrides SMS_OUTBOX_TTL_SECONDS for this message."""
        now = datetime.now(timezone.utc)
        attempts = 1 if attempted else 0
        next_at = now + timedelta(seconds=self._backoff(attempts)) if attempted else now
        ttl = ttl_seconds or self.settings.sms_outbox_ttl_seconds
        item = OutboxItem(number=number, message=message, priority=priority,
                          dedup_key=dedup_key, created_at=now, next_attempt_at=next_at,
                          attempts=attempts, last_error=reason,
                          expires_at=now + timedelta(seconds=ttl), source=source, type=type)
        await self._persist_new(item)
        self._pending[item.id] = item
        logger.warning("SMS queued in outbox #%s: %s → %s — %s",
                       item.id, number, message[:40], reason)
        self._wake.set()
        return item

    async def cancel(self, item_id: int) -> bool:
        item = self._pending.pop(item_id, None)
        if item is None:
            return False
        item.status = "cancelled"
        self.counters["cancelled"] += 1
        await self._persist(item)
        logger.info("outbox #%s cancelled", item_id)
        return True

    # ── Worker ────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.sms_outbox_poll_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.process_due()
            except Exception:
                logger.exception("outbox worker iteration failed")

    async def process_due(self, now: datetime | None = None) -> None:
        """Expire stale items, then try due items (critical first, oldest first).

        The round stops at the first connection-level failure: if the
        router is down, trying the remaining items would only stack up
        timeouts. A rejection of one message (``TeltonikaError`` from the
        router) does not stop the others.
        """

        now = now or datetime.now(timezone.utc)
        default_ttl = timedelta(seconds=self.settings.sms_outbox_ttl_seconds)
        for item in self._ordered():
            if item.id not in self._pending:
                continue  # cancelled meanwhile
            if now > (item.expires_at or item.created_at + default_ttl):
                await self._finish(item, "expired", success=False,
                                   detail=f"expired in outbox after {item.attempts} attempt(s); "
                                          f"last error: {item.last_error}")
            elif item.next_attempt_at <= now:
                error = await self._attempt(item, now)
                if error is not None and not _is_rejection(error):
                    break

    async def _attempt(self, item: OutboxItem, now: datetime) -> Exception | None:
        item.attempts += 1
        try:
            result = await self.client.send_sms(item.number, item.message)
        except Exception as e:
            item.last_error = describe_error(e)
            item.next_attempt_at = now + timedelta(seconds=self._backoff(item.attempts))
            await self._persist(item)
            logger.info("outbox #%s attempt %d failed: %s — next at %s", item.id, item.attempts,
                        item.last_error, item.next_attempt_at.isoformat(timespec="seconds"))
            return e
        delay = int((now - item.created_at).total_seconds())
        segments = segments_used(result, item.message)
        self.counters["segments"] += segments
        await self._finish(item, "sent", success=True, segments=segments,
                           detail=f"delivered from outbox after {item.attempts} attempt(s), "
                                  f"{delay}s late")
        return None

    def _backoff(self, attempts: int) -> int:
        base = self.settings.sms_outbox_poll_seconds
        return min(base * 2 ** max(attempts - 1, 0), self.settings.sms_outbox_max_backoff_seconds)

    async def _finish(self, item: OutboxItem, status: str, *, success: bool, detail: str,
                      segments: int | None = None) -> None:
        self._pending.pop(item.id, None)
        item.status = status
        self.counters["delivered" if status == "sent" else status] += 1
        await self._persist(item)
        self._record_history(SendHistoryEntry(
            number=item.number, message=item.message, priority=item.priority,
            success=success, timestamp=datetime.now(timezone.utc), detail=detail,
            source=item.source, type=item.type, segments=segments,
        ))
        log = logger.info if success else logger.error
        log("outbox #%s %s: %s → %s — %s", item.id, status, item.number, item.message[:40], detail)
        for listener in self._listeners:
            try:
                await listener(item)
            except Exception as e:
                logger.warning("outbox listener failed: %s", describe_error(e))

    # ── Persistence helpers ───────────────────────────────────

    def _ordered(self) -> list[OutboxItem]:
        return sorted(self._pending.values(),
                      key=lambda i: (i.priority != "critical", i.created_at))

    async def _persist_new(self, item: OutboxItem) -> None:
        try:
            item.id = await asyncio.to_thread(self.store.add, item)
        except Exception as e:
            item.id = next(self._local_ids)
            logger.error("outbox store add failed (%s) — #%s kept in memory only",
                         describe_error(e), item.id)

    async def _persist(self, item: OutboxItem) -> None:
        if item.id is None or item.id < 0:
            return
        try:
            await asyncio.to_thread(self.store.update, item)
        except Exception as e:
            logger.warning("outbox store update #%s failed: %s", item.id, describe_error(e))
