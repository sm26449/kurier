"""Watchdogs: an SMS when a service stops checking in (docs/adr/0001-watchdogs.md).

A service pings ``POST /api/watchdogs/{name}/ping`` with its timeout; the
ping is the registration (every ping stores ``timeout_sec`` and ``to``).
Every ``CHECK_SECONDS``:

- up, no ping for ``timeout_sec`` → down, SMS "⚠ {name}: no check-in since {time}";
- a ``status: fail`` ping while up → down at once, SMS "⚠ {name}: {detail}";
- an ``ok`` ping while down → up, SMS "✅ {name} is back" only if the
  down SMS went out.

At most one down SMS per watchdog every ``RENOTIFY`` (flapping): an outage
that starts inside that window is alerted when the window ends, if it is
still down then. After a restart every timer counts from the restart.

Passive only — nothing is probed. The SMS go through the normal send path
(priority critical: outbox, limits, history).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import Settings
from src.errors import describe_error
from src.models.schemas import Priority
from src.services.sms_service import SendOptions, SMSService
from src.services.teltonika import TeltonikaClient
from src.storage import MemoryWatchdogStore, MySQLWatchdogStore, WatchdogRecord

logger = logging.getLogger(__name__)

WatchdogStore = MemoryWatchdogStore | MySQLWatchdogStore

CHECK_SECONDS = 15
RENOTIFY = timedelta(minutes=30)
# The ping answer carries the modem state from the status cache. The check
# loop refreshes it when older than this (only while watchdogs exist), so a
# ping never waits for the router.
STATUS_REFRESH_SECONDS = 60
STATUS_FRESH_SECONDS = 180


class TooManyWatchdogs(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WatchdogService:
    def __init__(self, sms: SMSService, client: TeltonikaClient, store: WatchdogStore,
                 settings: Settings, clock: Callable[[], datetime] = _utcnow) -> None:
        self.sms = sms
        self.client = client
        self.store = store
        self.settings = settings
        self.clock = clock
        self._dogs: dict[str, WatchdogRecord] = {}
        self._started = clock()
        self._task: asyncio.Task | None = None
        self._sending: set[asyncio.Task] = set()

    # ── lifecycle ────────────────────────────────────────────────
    async def load(self) -> None:
        """Read the stored watchdogs; timers count from now (restart grace)."""
        try:
            rows = await asyncio.to_thread(self.store.load_all)
        except Exception as e:
            logger.warning("watchdogs: loading failed (%s) — they re-register on their next ping",
                           describe_error(e))
            rows = []
        self._dogs = {r.name: r for r in rows}
        self._started = self.clock()
        logger.info("watchdogs ready (%s store): %d registered, %d down",
                    "MySQL" if self.store.durable else "memory", len(self._dogs),
                    sum(r.state == "down" for r in self._dogs.values()))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._sending) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception as e:
                logger.error("watchdogs: check failed: %s", describe_error(e))
            await asyncio.sleep(CHECK_SECONDS)

    # ── API ──────────────────────────────────────────────────────
    def list(self) -> list[WatchdogRecord]:
        return sorted(self._dogs.values(), key=lambda r: r.name)

    def get(self, name: str) -> WatchdogRecord | None:
        return self._dogs.get(name)

    async def ping(self, name: str, timeout_sec: int, status: str = "ok",
                   detail: str | None = None, to: list[str] | None = None) -> WatchdogRecord:
        """Register or refresh ``name``; may change its state (and send an SMS)."""
        now = self.clock()
        rec = self._dogs.get(name)
        if rec is None:
            if len(self._dogs) >= self.settings.sms_watchdog_max:
                raise TooManyWatchdogs(
                    f"at most {self.settings.sms_watchdog_max} watchdogs (SMS_WATCHDOG_MAX)")
            rec = WatchdogRecord(name=name, timeout_sec=timeout_sec, last_ping=now, created_at=now)
            self._dogs[name] = rec
            logger.info("watchdog %s registered (timeout %d s)", name, timeout_sec)
        rec.timeout_sec = timeout_sec
        rec.recipients = list(to or [])
        rec.last_ping = now
        rec.last_status = status
        rec.detail = detail

        if status == "fail" and rec.state == "up":
            self._go_down(rec, "fail", now)
        elif status == "ok" and rec.state == "down":
            announce = rec.down_sms_sent
            self._go_up(rec)
            if announce:
                self._spawn(self._notify(rec, f"✅ {name} is back", "watchdog_up"))
        await self._save(rec)
        self._maybe_alert_down(rec, now)
        return rec

    async def delete(self, name: str) -> bool:
        """Forget ``name`` (planned stop): no alert, now or later."""
        known = self._dogs.pop(name, None) is not None
        stored = await asyncio.to_thread(self.store.delete, name)
        if known:
            logger.info("watchdog %s removed", name)
        return known or stored

    def modem(self) -> dict:
        """Modem state from the status cache — never queries the router."""
        age = self.client.status_age_seconds()
        if age is None or age > STATUS_FRESH_SECONDS:
            return {"registered": False, "signal_strength": None, "sim_state": None, "operator": None}
        st = self.client.cached_status()
        signal = st.get("signal_strength")
        signal = int(signal) if isinstance(signal, int | float) else None
        operator = st.get("operator") or None
        sim = st.get("sim_state")
        return {"registered": bool(operator) and signal is not None,
                "signal_strength": signal, "sim_state": sim if isinstance(sim, str) else None,
                "operator": operator}

    # ── checks ───────────────────────────────────────────────────
    async def check_once(self, now: datetime | None = None) -> None:
        now = now or self.clock()
        for rec in list(self._dogs.values()):
            if rec.state == "up":
                seen = max(rec.last_ping, self._started)
                if now - seen > timedelta(seconds=rec.timeout_sec):
                    self._go_down(rec, "timeout", now)
                    await self._save(rec)
            self._maybe_alert_down(rec, now)
        await self._refresh_modem_status()

    async def _refresh_modem_status(self) -> None:
        if not self._dogs:
            return
        age = self.client.status_age_seconds()
        if age is not None and age < STATUS_REFRESH_SECONDS:
            return
        try:
            await self.client.get_status()
        except Exception as e:
            logger.debug("watchdogs: modem status refresh failed: %s", describe_error(e))

    # ── transitions ──────────────────────────────────────────────
    @staticmethod
    def _go_down(rec: WatchdogRecord, reason: str, now: datetime) -> None:
        rec.state, rec.down_since, rec.down_reason, rec.down_sms_sent = "down", now, reason, False
        logger.warning("watchdog %s down (%s)", rec.name, reason)

    @staticmethod
    def _go_up(rec: WatchdogRecord) -> None:
        logger.info("watchdog %s back up", rec.name)
        rec.state, rec.down_since, rec.down_reason, rec.down_sms_sent = "up", None, None, False

    def _maybe_alert_down(self, rec: WatchdogRecord, now: datetime) -> None:
        """Send the down SMS unless one went out less than RENOTIFY ago."""
        if rec.state != "down" or rec.down_sms_sent:
            return
        if rec.down_sms_at is not None and now - rec.down_sms_at < RENOTIFY:
            return
        if rec.down_reason == "fail":
            text = f"⚠ {rec.name}: {rec.detail or 'reported a failure'}"
        else:
            text = f"⚠ {rec.name}: no check-in since {self._local(rec.last_ping)}"
        # Marked before sending: one SMS per outage even while it is in flight.
        rec.down_sms_at, rec.down_sms_sent = now, True
        self._spawn(self._send_down(rec, text))

    async def _send_down(self, rec: WatchdogRecord, text: str) -> None:
        sent = await self._notify(rec, text, "watchdog_down")
        if not sent and rec.state == "down":
            rec.down_sms_sent = False  # retried once RENOTIFY has passed
        await self._save(rec)

    async def _notify(self, rec: WatchdogRecord, text: str, kind: str) -> bool:
        """Send ``text`` to the watchdog's recipients; True if any accepted it."""
        numbers = rec.recipients or self.settings.watchdog_recipients_list or [None]
        opts = SendOptions(source="kurier", type=kind)
        accepted = False
        for number in numbers:
            try:
                results = await self.sms.send(text, number, Priority.critical, options=opts)
                accepted |= any(r.success or r.queued for r in results)
            except Exception as e:
                logger.error("watchdog %s: SMS to %s not sent: %s", rec.name,
                             number or "default recipients", describe_error(e))
        return accepted

    # ── helpers ──────────────────────────────────────────────────
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)

    async def _save(self, rec: WatchdogRecord) -> None:
        if rec.name not in self._dogs:
            return  # deleted meanwhile
        try:
            await asyncio.to_thread(self.store.save, rec)
        except Exception as e:
            logger.warning("watchdog %s: not saved: %s", rec.name, describe_error(e))

    def _local(self, ts: datetime) -> str:
        try:
            tz = ZoneInfo(self.settings.tz)
        except Exception:
            tz = timezone.utc
        return ts.astimezone(tz).strftime("%Y-%m-%d %H:%M")
