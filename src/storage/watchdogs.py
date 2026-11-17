"""Watchdog stores (``sms_watchdogs``, migrations/005): one row per
watchdog, written on every ping and every state change, read once at
startup.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from src.storage.db import MySQLConnection, _naive_utc, _utc


@dataclass
class WatchdogRecord:
    name: str
    timeout_sec: int
    last_ping: datetime
    created_at: datetime
    recipients: list[str] = field(default_factory=list)  # empty = SMS_WATCHDOG_TO
    state: str = "up"            # up | down
    last_status: str = "ok"      # ok | fail
    detail: str | None = None
    down_since: datetime | None = None
    down_reason: str | None = None   # timeout | fail
    down_sms_at: datetime | None = None
    down_sms_sent: bool = False


class MemoryWatchdogStore:
    """Process-local watchdogs (lost on restart; the next ping re-registers)."""

    durable = False

    def __init__(self) -> None:
        self._rows: dict[str, WatchdogRecord] = {}

    def save(self, rec: WatchdogRecord) -> None:
        self._rows[rec.name] = replace(rec, recipients=list(rec.recipients))

    def delete(self, name: str) -> bool:
        return self._rows.pop(name, None) is not None

    def load_all(self) -> list[WatchdogRecord]:
        return [replace(r, recipients=list(r.recipients)) for r in self._rows.values()]


class MySQLWatchdogStore:
    """Watchdogs in ``sms_watchdogs`` (migrations/005)."""

    durable = True

    def __init__(self, db: MySQLConnection) -> None:
        self._db = db

    def check_table(self) -> None:
        self._db.run(lambda cur: cur.execute("SELECT name FROM sms_watchdogs LIMIT 0"))

    def save(self, rec: WatchdogRecord) -> None:
        def naive(ts: datetime | None):
            return _naive_utc(ts) if ts is not None else None

        self._db.run(lambda cur: cur.execute(
            "INSERT INTO sms_watchdogs (name, timeout_sec, recipients, state, last_ping, "
            "last_status, detail, down_since, down_reason, down_sms_at, down_sms_sent, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE timeout_sec = VALUES(timeout_sec), "
            "recipients = VALUES(recipients), state = VALUES(state), "
            "last_ping = VALUES(last_ping), last_status = VALUES(last_status), "
            "detail = VALUES(detail), down_since = VALUES(down_since), "
            "down_reason = VALUES(down_reason), down_sms_at = VALUES(down_sms_at), "
            "down_sms_sent = VALUES(down_sms_sent)",
            (rec.name, rec.timeout_sec, ",".join(rec.recipients) or None, rec.state,
             _naive_utc(rec.last_ping), rec.last_status, rec.detail, naive(rec.down_since),
             rec.down_reason, naive(rec.down_sms_at), int(rec.down_sms_sent),
             _naive_utc(rec.created_at)),
        ))

    def delete(self, name: str) -> bool:
        def run(cur):
            cur.execute("DELETE FROM sms_watchdogs WHERE name = %s", (name,))
            return bool(cur.rowcount)

        return self._db.run(run)

    def load_all(self) -> list[WatchdogRecord]:
        def query(cur):
            cur.execute("SELECT * FROM sms_watchdogs")
            return cur.fetchall()

        def opt(ts):
            return _utc(ts) if ts is not None else None

        return [WatchdogRecord(
            name=r["name"], timeout_sec=int(r["timeout_sec"]),
            last_ping=_utc(r["last_ping"]), created_at=_utc(r["created_at"]),
            recipients=[x for x in (r["recipients"] or "").split(",") if x],
            state=r["state"], last_status=r["last_status"], detail=r["detail"],
            down_since=opt(r["down_since"]), down_reason=r["down_reason"],
            down_sms_at=opt(r["down_sms_at"]), down_sms_sent=bool(r["down_sms_sent"]),
        ) for r in self._db.run(query)]
