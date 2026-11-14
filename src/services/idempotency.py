"""Idempotent sends: a retried request with the same key gets the first result.

Producers (Klopot) retry a delivery after a timeout or an HTTP error. If
the first attempt was in fact sent or queued, the retry must not send a
second SMS or consume another rate-limit slot — it gets the stored result
back, marked ``replayed``.

Only accepted outcomes are remembered (sent, queued, or a mix with
per-recipient blocks). A request that failed as a whole (rate-limited,
duplicate, router error) raises and is NOT stored: retrying it is exactly
what the producer should do, and a blocked attempt costs nothing.

Concurrent requests with the same key share one execution: the second
waits for the first instead of racing it.

A key reused with a *different* request (number, text or priority) is a
producer bug — e.g. one key for both an alert and its escalation — and is
rejected (``IdempotencyConflict`` → HTTP 422) instead of silently
replaying the wrong result.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from src.errors import describe_error
from src.models.schemas import SendHistoryEntry
from src.storage import MemoryIdempotencyStore, MySQLIdempotencyStore

logger = logging.getLogger(__name__)

IdempotencyStore = MemoryIdempotencyStore | MySQLIdempotencyStore


class IdempotencyConflict(ValueError):
    """Same idempotency key, different request."""


def fingerprint(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:16]


class IdempotencyCache:
    def __init__(self, store: IdempotencyStore, ttl_seconds: int) -> None:
        self.store = store
        self.ttl = timedelta(seconds=ttl_seconds)
        # key → (created, request fingerprint, results)
        self._done: dict[str, tuple[datetime, str, list[SendHistoryEntry]]] = {}
        self._inflight: dict[str, tuple[str, asyncio.Future]] = {}

    async def load(self) -> None:
        since = datetime.now(timezone.utc) - self.ttl
        try:
            rows = await asyncio.to_thread(self.store.load_since, since)
        except Exception as e:
            logger.warning("idempotency: loading keys failed: %s", describe_error(e))
            return
        for key, ts, response in rows:
            try:
                fp, results = response["fp"], response["results"]
                self._done[key] = (ts, fp, [SendHistoryEntry(**r) for r in results])
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("idempotency: skipping malformed record %s: %s", key, e)
        logger.info("idempotency ready (%s store): %d key(s)",
                    "MySQL" if self.store.durable else "in-memory", len(self._done))

    @staticmethod
    def _check(key: str, stored_fp: str, fp: str) -> None:
        if stored_fp != fp:
            raise IdempotencyConflict(
                f"idempotency_key {key!r} was already used for a different request "
                "(number, message or priority differ) — use one key per delivery"
            )

    def _lookup(self, key: str, fp: str, now: datetime) -> list[SendHistoryEntry] | None:
        hit = self._done.get(key)
        if hit is None:
            return None
        ts, stored_fp, results = hit
        if now - ts > self.ttl:
            del self._done[key]
            return None
        self._check(key, stored_fp, fp)
        return [r.model_copy(update={"replayed": True}) for r in results]

    async def run(
        self, key: str, fp: str, execute: Callable[[], Awaitable[list[SendHistoryEntry]]],
    ) -> list[SendHistoryEntry]:
        """Return the stored result for ``key`` or execute once and store it.
        ``fp`` fingerprints the request; a different one under the same key
        raises ``IdempotencyConflict``."""
        cached = self._lookup(key, fp, datetime.now(timezone.utc))
        if cached is not None:
            logger.info("idempotent replay: %s", key)
            return cached

        if key in self._inflight:
            running_fp, running = self._inflight[key]
            self._check(key, running_fp, fp)
            results = await asyncio.shield(running)
            return [r.model_copy(update={"replayed": True}) for r in results]

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = (fp, future)
        try:
            results = await execute()
        except BaseException as e:
            future.set_exception(e)
            future.exception()  # mark retrieved: no "never retrieved" warning without waiters
            raise
        finally:
            self._inflight.pop(key, None)

        now = datetime.now(timezone.utc)
        self._done[key] = (now, fp, results)
        future.set_result(results)
        record = {"fp": fp, "results": [r.model_dump(mode="json") for r in results]}
        try:
            await asyncio.to_thread(self.store.save, key, now, record)
        except Exception as e:
            logger.warning("idempotency: persisting %s failed (kept in memory): %s",
                           key, describe_error(e))
        return results

    async def prune(self) -> int:
        """Drop expired keys: memory on the event loop, the store in a thread."""
        cutoff = datetime.now(timezone.utc) - self.ttl
        for key in [k for k, (ts, _, _) in self._done.items() if ts < cutoff]:
            del self._done[key]
        return await asyncio.to_thread(self.store.prune_before, cutoff)
