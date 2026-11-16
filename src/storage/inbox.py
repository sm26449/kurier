"""Inbound SMS archive (``sms_inbox``, migrations/004): each message is
archived once by fingerprint, then marked published / forwarded or stale.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from src.storage.db import MySQLConnection, _naive_utc, _utc


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
