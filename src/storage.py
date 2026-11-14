"""Persistent stores backed by MySQL (database ``MYSQL_DATABASE``).

- ``SendHistoryStore`` — every send attempt (``sms_log``), rehydrated
  into the in-memory history deque on startup.
- ``MySQLOutboxStore`` — SMS accepted while the router was unreachable
  (``sms_outbox``), retried until delivered or expired.
- ``MemoryOutboxStore`` — same interface, in-process only; used when
  MySQL is not configured or the outbox table does not exist yet.
- ``MySQLIdempotencyStore`` / ``MemoryIdempotencyStore`` — results of
  requests that carried an ``idempotency_key`` (``sms_idempotency``).

Columns added by later migrations are probed at startup, so the code
runs before and after ``migrations/003`` is applied.

Schemas live in ``migrations/``. MySQL rather than an embedded database so
the gateway's state lives with the rest of the stack's data and its backups.

Outbox stores are synchronous; async callers wrap them in
``asyncio.to_thread`` and get errors back so they can fall back to
memory. History writes go through a single background thread
(fire-and-forget, in order): a slow or down MySQL never blocks the
event loop, and errors are logged and swallowed.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

import pymysql
from pymysql.cursors import DictCursor

from src.models.schemas import SendHistoryEntry

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _utc(ts: datetime) -> datetime:
    """MySQL DATETIME is naive; values are stored as UTC."""
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _naive_utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc).replace(tzinfo=None)


def _has_columns(db: "MySQLConnection", table: str, columns: str) -> bool:
    """True when ``table`` has ``columns`` (comma-separated) — schema probe
    so the code runs before and after a migration is applied."""
    try:
        db.run(lambda cur: cur.execute(f"SELECT {columns} FROM {table} LIMIT 0"))
        return True
    except pymysql.MySQLError:
        return False


class MySQLConnection:
    """One lazily-opened, auto-reconnecting connection, serialised by a lock.

    Write volume is a few rows per minute, so one connection is plenty.
    """

    def __init__(self, *, host: str, port: int, user: str, password: str, database: str) -> None:
        self._dsn: dict[str, Any] = {
            "host": host, "port": port, "user": user, "password": password,
            "database": database, "charset": "utf8mb4", "cursorclass": DictCursor,
            "autocommit": True, "connect_timeout": 5,
        }
        self.label = f"{user}@{host}:{port}/{database}"
        self._lock = threading.Lock()
        self._conn: pymysql.connections.Connection | None = None

    def _ensure_conn(self) -> pymysql.connections.Connection:
        # ping(reconnect=True) survives MySQL's idle timeout (28800s).
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except pymysql.MySQLError:
                self.close()
        self._conn = pymysql.connect(**self._dsn)
        return self._conn

    def run(self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(cursor)`` with a live connection. MySQL errors propagate."""
        with self._lock:
            with self._ensure_conn().cursor() as cur:
                return fn(cur)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except pymysql.MySQLError:
                pass
            self._conn = None


# ── Send history ──────────────────────────────────────────────


class SendHistoryStore:
    """MySQL-backed log of every SMS send attempt."""

    def __init__(self, db: MySQLConnection) -> None:
        self._db = db
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sms-history")
        # source/type/segments arrive with migrations/003.
        self.extended = _has_columns(db, "sms_log", "source, type, segments")
        if not self.extended:
            logger.warning("sms_log has no source/type/segments columns — "
                           "apply migrations/003_extended_contract.sql")

    def record(self, entry: SendHistoryEntry) -> None:
        """Queue one row for insertion; returns immediately."""
        self._writer.submit(self._write, entry)

    def close(self) -> None:
        """Flush queued writes."""
        self._writer.shutdown(wait=True)

    def _write(self, entry: SendHistoryEntry) -> None:
        cols = ["ts", "number", "message", "priority", "success", "detail"]
        vals = [_naive_utc(entry.timestamp), entry.number, entry.message, entry.priority,
                1 if entry.success else 0, entry.detail]
        if self.extended:
            cols += ["source", "type", "segments"]
            vals += [entry.source, entry.type, entry.segments]
        sql = (f"INSERT INTO sms_log ({', '.join(cols)}) "
               f"VALUES ({', '.join(['%s'] * len(cols))})")
        try:
            self._db.run(lambda cur: cur.execute(sql, vals))
        except pymysql.MySQLError as e:
            logger.warning("sms history record failed: %s", e)

    def load_recent(self, limit: int = 100) -> list[SendHistoryEntry]:
        """Rehydrate the most recent N entries on startup."""
        extra = ", source, type, segments" if self.extended else ""

        def query(cur):
            cur.execute(
                f"SELECT ts, number, message, priority, success, detail{extra} "
                "FROM sms_log ORDER BY id DESC LIMIT %s", (limit,),
            )
            return cur.fetchall()

        try:
            rows = self._db.run(query)
        except pymysql.MySQLError as e:
            logger.warning("sms history load failed: %s", e)
            return []
        out: list[SendHistoryEntry] = []
        for r in rows:
            try:
                out.append(SendHistoryEntry(
                    number=r["number"], message=r["message"], priority=r["priority"],
                    success=bool(r["success"]), timestamp=_utc(r["ts"]), detail=r["detail"],
                    source=r.get("source"), type=r.get("type"), segments=r.get("segments"),
                ))
            except (KeyError, ValueError) as e:
                logger.debug("skipping malformed history row: %s", e)
        return out

    def rows_since(self, since: datetime) -> list[dict[str, Any]]:
        """Raw rows for statistics (``services/stats.py``)."""
        extra = ", source, type, segments" if self.extended else ""

        def query(cur):
            cur.execute(f"SELECT ts, success, detail, priority{extra} FROM sms_log "
                        "WHERE ts >= %s ORDER BY id", (_naive_utc(since),))
            return cur.fetchall()

        return [{**r, "ts": _utc(r["ts"]), "success": bool(r["success"])}
                for r in self._db.run(query)]

    # status filter → SQL condition (same classification as services/stats.py)
    _STATUS_SQL = {
        "sent": "success = 1",
        "blocked": "success = 0 AND detail LIKE 'Rate limit%%'",
        "duplicate": "success = 0 AND detail LIKE 'Duplicate%%'",
        "queued": "success = 0 AND detail LIKE 'queued%%'",
        "failed": ("success = 0 AND (detail IS NULL OR (detail NOT LIKE 'Rate limit%%' "
                   "AND detail NOT LIKE 'Duplicate%%' AND detail NOT LIKE 'queued%%'))"),
    }

    def search(self, *, q: str | None = None, type: str | None = None,
               source: str | None = None, status: str | None = None,
               priority: str | None = None, since: datetime | None = None,
               until: datetime | None = None, limit: int = 50,
               offset: int = 0) -> list[SendHistoryEntry]:
        where, params = [], []
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(message LIKE %s OR number LIKE %s)")
            params += [f"%{esc}%", f"%{esc}%"]
        if status in self._STATUS_SQL:
            where.append(self._STATUS_SQL[status])
        if priority:
            where.append("priority = %s")
            params.append(priority)
        if self.extended and type:
            where.append("type = %s")
            params.append(type)
        if self.extended and source:
            where.append("source = %s")
            params.append(source)
        if since:
            where.append("ts >= %s")
            params.append(_naive_utc(since))
        if until:
            where.append("ts < %s")
            params.append(_naive_utc(until))
        extra = ", source, type, segments" if self.extended else ""
        sql = (f"SELECT ts, number, message, priority, success, detail{extra} FROM sms_log "
               f"{'WHERE ' + ' AND '.join(where) if where else ''} "
               "ORDER BY id DESC LIMIT %s OFFSET %s")

        def query(cur):
            cur.execute(sql, (*params, limit, offset))
            return cur.fetchall()

        return [SendHistoryEntry(
            number=r["number"], message=r["message"], priority=r["priority"],
            success=bool(r["success"]), timestamp=_utc(r["ts"]), detail=r["detail"],
            source=r.get("source"), type=r.get("type"), segments=r.get("segments"),
        ) for r in self._db.run(query)]

    def prune_older_than_days(self, days: int = 90) -> int:
        """Delete rows older than ``days`` days. Returns rows deleted."""
        cutoff = _naive_utc(datetime.now(timezone.utc) - timedelta(days=days))

        def delete(cur):
            cur.execute("DELETE FROM sms_log WHERE ts < %s", (cutoff,))
            return cur.rowcount or 0

        try:
            return self._db.run(delete)
        except pymysql.MySQLError as e:
            logger.warning("sms history prune failed: %s", e)
            return 0


# ── Outbox ────────────────────────────────────────────────────


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


# ── Idempotency ───────────────────────────────────────────────


class MemoryIdempotencyStore:
    """Process-local idempotency records (lost on restart)."""

    durable = False

    def __init__(self) -> None:
        self._rows: dict[str, tuple[datetime, dict[str, Any]]] = {}

    def save(self, key: str, created_at: datetime, response: dict[str, Any]) -> None:
        self._rows[key] = (created_at, response)

    def load_since(self, since: datetime) -> list[tuple[str, datetime, dict[str, Any]]]:
        return [(k, ts, r) for k, (ts, r) in self._rows.items() if ts >= since]

    def prune_before(self, cutoff: datetime) -> int:
        old = [k for k, (ts, _) in self._rows.items() if ts < cutoff]
        for k in old:
            del self._rows[k]
        return len(old)


class MySQLIdempotencyStore:
    """Idempotency records in ``sms_idempotency`` (migrations/003)."""

    durable = True

    def __init__(self, db: MySQLConnection) -> None:
        self._db = db

    def check_table(self) -> None:
        self._db.run(lambda cur: cur.execute("SELECT idem_key FROM sms_idempotency LIMIT 0"))

    def save(self, key: str, created_at: datetime, response: dict[str, Any]) -> None:
        self._db.run(lambda cur: cur.execute(
            "INSERT INTO sms_idempotency (idem_key, created_at, response) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE idem_key = idem_key",
            (key, _naive_utc(created_at), json.dumps(response)),
        ))

    def load_since(self, since: datetime) -> list[tuple[str, datetime, dict[str, Any]]]:
        def query(cur):
            cur.execute("SELECT idem_key, created_at, response FROM sms_idempotency "
                        "WHERE created_at >= %s", (_naive_utc(since),))
            return cur.fetchall()

        out = []
        for r in self._db.run(query):
            resp = r["response"]
            out.append((r["idem_key"], _utc(r["created_at"]),
                        json.loads(resp) if isinstance(resp, str | bytes) else resp))
        return out

    def prune_before(self, cutoff: datetime) -> int:
        def delete(cur):
            cur.execute("DELETE FROM sms_idempotency WHERE created_at < %s", (_naive_utc(cutoff),))
            return cur.rowcount or 0

        return self._db.run(delete)


# ── Inbound SMS archive ───────────────────────────────────────


@dataclass
class InboxRecord:
    fingerprint: str
    received_at: datetime
    sender: str
    message: str
    authorized: bool
    stale: bool
    modem_date: datetime | None = None
    modem_date_raw: str | None = None
    published_at: datetime | None = None
    forwarded_at: datetime | None = None
    forward_attempts: int = 0
    forward_error: str | None = None
    id: int | None = None


class MemoryInboxStore:
    """Process-local archive (no MySQL): exactly-once within one run."""

    durable = False

    def __init__(self) -> None:
        self._rows: dict[int, InboxRecord] = {}
        self._by_fp: dict[str, int] = {}
        self._ids = itertools.count(1)

    def add(self, rec: InboxRecord) -> int | None:
        """Insert; returns the new id, or None if the fingerprint is known."""
        if rec.fingerprint in self._by_fp:
            return None
        rec_id = next(self._ids)
        self._rows[rec_id] = replace(rec, id=rec_id)
        self._by_fp[rec.fingerprint] = rec_id
        return rec_id

    def mark_published(self, rec_id: int, at: datetime) -> None:
        if rec_id in self._rows:
            self._rows[rec_id].published_at = at

    @staticmethod
    def _sent(r: InboxRecord) -> datetime:
        return r.modem_date or r.received_at

    def unpublished(self, since: datetime, limit: int = 50) -> list[InboxRecord]:
        rows = [r for r in self._rows.values()
                if r.published_at is None and not r.stale and self._sent(r) >= since]
        return [replace(r) for r in sorted(rows, key=lambda r: r.id)[:limit]]

    def unforwarded(self, since: datetime, limit: int = 50) -> list[InboxRecord]:
        rows = [r for r in self._rows.values()
                if r.forwarded_at is None and not r.stale and self._sent(r) >= since]
        return [replace(r) for r in sorted(rows, key=lambda r: r.id)[:limit]]

    def expire_undelivered(self, before: datetime, webhook: bool) -> int:
        """Mark stale what left the delivery window undelivered; returns count."""
        n = 0
        for r in self._rows.values():
            pending = r.published_at is None or (webhook and r.forwarded_at is None)
            if not r.stale and pending and self._sent(r) < before:
                r.stale, n = True, n + 1
        return n

    def mark_forwarded(self, rec_id: int, at: datetime | None, attempts: int,
                       error: str | None) -> None:
        if rec_id in self._rows:
            r = self._rows[rec_id]
            r.forwarded_at, r.forward_attempts, r.forward_error = at, attempts, error

    def recent(self, limit: int) -> list[InboxRecord]:
        return [replace(r) for r in sorted(self._rows.values(), key=lambda r: -r.id)[:limit]]

    def get(self, rec_id: int) -> InboxRecord | None:
        r = self._rows.get(rec_id)
        return replace(r) if r else None

    def delete(self, rec_id: int) -> bool:
        r = self._rows.pop(rec_id, None)
        if r:
            self._by_fp.pop(r.fingerprint, None)
        return r is not None

    def prune_older_than_days(self, days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        old = [k for k, r in self._rows.items() if r.received_at < cutoff]
        for k in old:
            self.delete(k)
        return len(old)


class MySQLInboxStore:
    """Archive in ``sms_inbox`` (migrations/004)."""

    durable = True
    _COLS = ("id, fingerprint, received_at, modem_date, modem_date_raw, sender, message, "
             "authorized, stale, published_at, forwarded_at, forward_attempts, forward_error")

    def __init__(self, db: MySQLConnection) -> None:
        self._db = db

    def check_table(self) -> None:
        self._db.run(lambda cur: cur.execute("SELECT id FROM sms_inbox LIMIT 0"))

    def add(self, rec: InboxRecord) -> int | None:
        def insert(cur):
            cur.execute(
                "INSERT IGNORE INTO sms_inbox (fingerprint, received_at, modem_date, modem_date_raw, "
                "sender, message, authorized, stale) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (rec.fingerprint, _naive_utc(rec.received_at),
                 _naive_utc(rec.modem_date) if rec.modem_date else None, rec.modem_date_raw,
                 rec.sender, rec.message, int(rec.authorized), int(rec.stale)),
            )
            return cur.lastrowid if cur.rowcount else None

        return self._db.run(insert)

    def mark_published(self, rec_id: int, at: datetime) -> None:
        self._db.run(lambda cur: cur.execute(
            "UPDATE sms_inbox SET published_at=%s WHERE id=%s", (_naive_utc(at), rec_id)))

    def _rows(self, where: str, params: tuple, order: str, limit: int) -> list[InboxRecord]:
        def query(cur):
            cur.execute(f"SELECT {self._COLS} FROM sms_inbox {where} ORDER BY {order} LIMIT %s",
                        (*params, limit))
            return cur.fetchall()

        return [
            InboxRecord(
                id=r["id"], fingerprint=r["fingerprint"], received_at=_utc(r["received_at"]),
                modem_date=_utc(r["modem_date"]) if r["modem_date"] else None,
                modem_date_raw=r["modem_date_raw"], sender=r["sender"], message=r["message"],
                authorized=bool(r["authorized"]), stale=bool(r["stale"]),
                published_at=_utc(r["published_at"]) if r["published_at"] else None,
                forwarded_at=_utc(r["forwarded_at"]) if r["forwarded_at"] else None,
                forward_attempts=r["forward_attempts"], forward_error=r["forward_error"],
            )
            for r in self._db.run(query)
        ]

    _SENT = "COALESCE(modem_date, received_at)"

    def unpublished(self, since: datetime, limit: int = 50) -> list[InboxRecord]:
        return self._rows(f"WHERE published_at IS NULL AND stale = 0 AND {self._SENT} >= %s",
                          (_naive_utc(since),), "id", limit)

    def unforwarded(self, since: datetime, limit: int = 50) -> list[InboxRecord]:
        return self._rows(f"WHERE forwarded_at IS NULL AND stale = 0 AND {self._SENT} >= %s",
                          (_naive_utc(since),), "id", limit)

    def expire_undelivered(self, before: datetime, webhook: bool) -> int:
        pending = "(published_at IS NULL OR forwarded_at IS NULL)" if webhook \
            else "published_at IS NULL"

        def update(cur):
            cur.execute(f"UPDATE sms_inbox SET stale = 1 WHERE stale = 0 AND {pending} "
                        f"AND {self._SENT} < %s", (_naive_utc(before),))
            return cur.rowcount or 0

        return self._db.run(update)

    def mark_forwarded(self, rec_id: int, at: datetime | None, attempts: int,
                       error: str | None) -> None:
        self._db.run(lambda cur: cur.execute(
            "UPDATE sms_inbox SET forwarded_at=%s, forward_attempts=%s, forward_error=%s "
            "WHERE id=%s",
            (_naive_utc(at) if at else None, attempts, (error or "")[:255] or None, rec_id)))

    def recent(self, limit: int) -> list[InboxRecord]:
        return self._rows("", (), "id DESC", limit)

    def get(self, rec_id: int) -> InboxRecord | None:
        rows = self._rows("WHERE id = %s", (rec_id,), "id", 1)
        return rows[0] if rows else None

    def delete(self, rec_id: int) -> bool:
        def delete(cur):
            cur.execute("DELETE FROM sms_inbox WHERE id=%s", (rec_id,))
            return bool(cur.rowcount)

        return self._db.run(delete)

    def prune_older_than_days(self, days: int) -> int:
        cutoff = _naive_utc(datetime.now(timezone.utc) - timedelta(days=days))

        def delete(cur):
            cur.execute("DELETE FROM sms_inbox WHERE received_at < %s", (cutoff,))
            return cur.rowcount or 0

        return self._db.run(delete)
