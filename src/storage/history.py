"""Send history: every send attempt in ``sms_log`` (migrations/001, 003).

Writes go through a single background thread (fire-and-forget, in
order): a slow or down MySQL never blocks the event loop, and errors are
logged and swallowed. Rows are rehydrated into the in-memory history
deque on startup and feed statistics and search.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pymysql

from src.models.schemas import SendHistoryEntry
from src.storage.db import MySQLConnection, _has_columns, _naive_utc, _utc

logger = logging.getLogger(__name__)


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
