"""MySQL plumbing shared by the stores: one connection, UTC helpers, and
the column probe that lets the code run before and after a migration.

MySQL rather than an embedded database so the gateway's state lives with
the rest of the stack's data and its backups. Schemas live in
``migrations/``.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar

import pymysql
from pymysql.cursors import DictCursor

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
