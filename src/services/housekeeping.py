"""Daily cleanup: old send history, finished outbox rows, expired idempotency keys, old inbox."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from src.config import Settings
from src.errors import describe_error
from src.services.idempotency import IdempotencyCache
from src.storage import (
    MemoryInboxStore,
    MemoryOutboxStore,
    MySQLInboxStore,
    MySQLOutboxStore,
    SendHistoryStore,
)

logger = logging.getLogger(__name__)

FIRST_RUN_DELAY_SECONDS = 300
INTERVAL_SECONDS = 24 * 3600


class Housekeeping:
    def __init__(self, settings: Settings, history: SendHistoryStore | None,
                 outbox: MySQLOutboxStore | MemoryOutboxStore | None,
                 idempotency: IdempotencyCache | None = None,
                 inbox: MySQLInboxStore | MemoryInboxStore | None = None) -> None:
        self.settings = settings
        self.history = history
        self.outbox = outbox
        self.idempotency = idempotency
        self.inbox = inbox
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)
        while True:
            await self.run_once()
            await asyncio.sleep(INTERVAL_SECONDS)

    async def run_once(self) -> dict[str, int]:
        result: dict[str, int] = {}
        jobs = []
        if self.history is not None:
            days = self.settings.sms_history_retention_days
            jobs.append(("sms_log", lambda: asyncio.to_thread(
                self.history.prune_older_than_days, days)))
        if self.outbox is not None:
            days_ob = self.settings.sms_outbox_retention_days
            jobs.append(("sms_outbox", lambda: asyncio.to_thread(
                self.outbox.prune_finished, days_ob)))
        if self.idempotency is not None:
            jobs.append(("sms_idempotency", self.idempotency.prune))
        if self.inbox is not None:
            days_in = self.settings.sms_inbox_retention_days
            jobs.append(("sms_inbox", lambda: asyncio.to_thread(
                self.inbox.prune_older_than_days, days_in)))
        for name, job in jobs:
            try:
                result[name] = await job()
            except Exception as e:
                logger.warning("housekeeping: pruning %s failed: %s", name, describe_error(e))
        if any(result.values()):
            logger.info("housekeeping: pruned %s", result)
        return result
