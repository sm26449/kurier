"""Outbox stores: SMS the router didn't take, retried until delivered or
expired (``sms_outbox``, migrations/002 + 003).

``MemoryOutboxStore`` has the same interface, in-process only; used when
MySQL is not configured or the table does not exist yet. Stores are
synchronous; async callers wrap them in ``asyncio.to_thread`` and get
errors back so they can fall back to memory.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from src.storage.db import MySQLConnection, _has_columns, _naive_utc, _utc

logger = logging.getLogger(__name__)


@dataclass
class OutboxItem:
    number: str
    message: str
    priority: str
    dedup_key: str
    created_at: datetime
    next_attempt_at: datetime
    status: str = "pending"  # pending | sent | expired | cancelled
    attempts: int = 0
    last_error: str | None = None
    id: int | None = None
    expires_at: datetime | None = None  # None → created_at + SMS_OUTBOX_TTL_SECONDS
    source: str | None = None
    type: str | None = None


class MemoryOutboxStore:
    """In-process outbox. Survives a router outage, not a restart."""

    durable = False

    def __init__(self) -> None:
        self._items: dict[int, OutboxItem] = {}
        self._ids = itertools.count(1)

    def add(self, item: OutboxItem) -> int:
        item_id = next(self._ids)
        self._items[item_id] = replace(item, id=item_id)
        return item_id

    def update(self, item: OutboxItem) -> None:
        self._items[item.id] = replace(item)

    def prune_finished(self, days: int) -> int:
        done = [k for k, i in self._items.items() if i.status != "pending"]
        for k in done:
            del self._items[k]
        return len(done)

    def load_pending(self) -> list[OutboxItem]:
        return [replace(i) for i in self._items.values() if i.status == "pending"]


class MySQLOutboxStore:
    """Outbox persisted in ``sms_outbox`` (migrations/002)."""

    durable = True
    _BASE = ["created_at", "updated_at", "number", "message", "priority", "dedup_key",
             "status", "attempts", "next_attempt_at", "last_error"]
    _EXTENDED = ["expires_at", "source", "type"]  # migrations/003

    def __init__(self, db: MySQLConnection) -> None:
        self._db = db
        self.extended = False

    def check_table(self) -> None:
        """Raise ``pymysql.MySQLError`` if the table is missing / not readable."""
        self._db.run(lambda cur: cur.execute("SELECT id FROM sms_outbox LIMIT 0"))
        self.extended = _has_columns(self._db, "sms_outbox", ", ".join(self._EXTENDED))
        if not self.extended:
            logger.warning("sms_outbox has no expires_at/source/type columns — per-message "
                           "TTL is kept in memory only; apply migrations/003_extended_contract.sql")

    @property
    def _columns(self) -> list[str]:
        return self._BASE + (self._EXTENDED if self.extended else [])

    def add(self, item: OutboxItem) -> int:
        now = _naive_utc(datetime.now(timezone.utc))
        vals = [_naive_utc(item.created_at), now, item.number, item.message, item.priority,
                item.dedup_key, item.status, item.attempts,
                _naive_utc(item.next_attempt_at), item.last_error]
        if self.extended:
            vals += [_naive_utc(item.expires_at) if item.expires_at else None,
                     item.source, item.type]
        cols = self._columns

        def insert(cur):
            cur.execute(
                f"INSERT INTO sms_outbox ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})", vals,
            )
            return cur.lastrowid

        return self._db.run(insert)

    def update(self, item: OutboxItem) -> None:
        self._db.run(lambda cur: cur.execute(
            "UPDATE sms_outbox SET status=%s, attempts=%s, next_attempt_at=%s, "
            "last_error=%s, updated_at=%s WHERE id=%s",
            (item.status, item.attempts, _naive_utc(item.next_attempt_at), item.last_error,
             _naive_utc(datetime.now(timezone.utc)), item.id),
        ))

    def prune_finished(self, days: int) -> int:
        """Delete delivered/expired/cancelled rows not touched for ``days`` days."""
        cutoff = _naive_utc(datetime.now(timezone.utc) - timedelta(days=days))

        def delete(cur):
            cur.execute("DELETE FROM sms_outbox WHERE status <> 'pending' AND updated_at < %s",
                        (cutoff,))
            return cur.rowcount or 0

        return self._db.run(delete)

    def load_pending(self) -> list[OutboxItem]:
        cols = ", ".join(self._columns)

        def query(cur):
            cur.execute(f"SELECT id, {cols} FROM sms_outbox "
                        "WHERE status = 'pending' ORDER BY id")
            return cur.fetchall()

        return [
            OutboxItem(
                id=r["id"], number=r["number"], message=r["message"], priority=r["priority"],
                dedup_key=r["dedup_key"], created_at=_utc(r["created_at"]),
                next_attempt_at=_utc(r["next_attempt_at"]), status=r["status"],
                attempts=r["attempts"], last_error=r["last_error"],
                expires_at=_utc(r["expires_at"]) if r.get("expires_at") else None,
                source=r.get("source"), type=r.get("type"),
            )
            for r in self._db.run(query)
        ]
