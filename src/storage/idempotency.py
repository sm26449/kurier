"""Idempotency stores: results of requests that carried an
``idempotency_key`` (``sms_idempotency``, migrations/003), in MySQL or
in process memory.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.storage.db import MySQLConnection, _naive_utc, _utc


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
