"""Inbound SMS transport: modem → archive → MQTT / webhook, exactly once.

Every ``SMS_INBOX_POLL_SECONDS`` the modem inbox is read. Each message is
archived once (unique fingerprint = sender | modem date | text), then
delivered:

- MQTT ``sms-gateway/inbox`` (QoS 1) — retried every poll;
- an optional webhook (``SMS_INBOX_WEBHOOK_URL``, e.g. Klopot's
  ``sms_inbox`` input) — retried every poll;

both only within the delivery window (see below).

After archiving, the message is deleted from the modem
(``SMS_INBOX_DELETE_AFTER_ARCHIVE``): the SIM never fills up and modem
indexes are never reused under our feet. With the in-memory store (no
MySQL) a message is deleted only once delivered, so a restart cannot lose it.

Flags for consumers — the gateway only transports, it never acts on text:
``authorized`` = sender is one of ``SMS_DEFAULT_RECIPIENTS``;
``age_seconds`` = time since the SMS was sent, at delivery.

Delivery window: a message is delivered (MQTT and webhook) only while
younger than ``SMS_INBOX_STALE_SECONDS`` (10 min) since it was *sent*.
Whatever delays it — modem outage, broker down, webhook failing, a webhook
enabled later — once outside the window it is marked ``stale``: kept in
the archive / API, never delivered. Nothing downstream acts on an old
command.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from src.config import Settings
from src.errors import describe_error
from src.phone import normalize_phone
from src.services.teltonika import TeltonikaClient
from src.storage import InboxRecord, MemoryInboxStore, MySQLInboxStore

logger = logging.getLogger(__name__)

InboxStore = MemoryInboxStore | MySQLInboxStore
Publisher = Callable[[InboxRecord], Awaitable[bool]]

_MODEM_DATE_FMT = "%a %b %d %H:%M:%S %Y"  # gsmctl: "Tue Mar  3 20:13:00 2026"


def parse_modem_date(raw: str | None, tz: str) -> datetime | None:
    """Modem local time → UTC; None if the format is unknown."""
    if not raw:
        return None
    try:
        local = datetime.strptime(" ".join(raw.split()), _MODEM_DATE_FMT)
    except ValueError:
        return None
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def inbox_payload(rec: InboxRecord, modem: str, now: datetime | None = None) -> dict:
    """One shape for MQTT and webhook. ``from``/``body``/``received_at``/
    ``modem`` match Klopot's ``sms_inbox`` input; ``message`` and
    ``timestamp`` keep the pre-1.7 MQTT fields."""
    sent = rec.modem_date or rec.received_at
    now = now or datetime.now(timezone.utc)
    sent_at = sent.isoformat()
    return {
        "id": rec.id,
        "from": rec.sender,
        "body": rec.message,
        "message": rec.message,
        "received_at": sent_at,
        "timestamp": sent_at,
        "archived_at": rec.received_at.isoformat(),
        "authorized": rec.authorized,
        "stale": rec.stale,
        "age_seconds": max(0, int((now - sent).total_seconds())),
        "modem": modem,
    }


class InboxService:
    def __init__(self, client: TeltonikaClient, store: InboxStore, settings: Settings,
                 http: httpx.AsyncClient | None = None) -> None:
        self.client = client
        self.store = store
        self.settings = settings
        self._http = http
        self._publishers: list[Publisher] = []
        self._first_poll = True
        self._task: asyncio.Task | None = None
        self.counters: Counter[str] = Counter()

    # ── Wiring / lifecycle ────────────────────────────────────

    def add_publisher(self, publisher: Publisher) -> None:
        """``publisher(record) -> bool``: True once the message is handed over."""
        self._publishers.append(publisher)

    async def start(self) -> None:
        if self.settings.sms_inbox_webhook_url and self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._task = asyncio.create_task(self._run())
        logger.info("inbox ready (%s store), poll every %ss, delete after archive: %s, webhook: %s",
                    "MySQL" if self.store.durable else "in-memory",
                    self.settings.sms_inbox_poll_seconds,
                    self.settings.sms_inbox_delete_after_archive,
                    "on" if self.settings.sms_inbox_webhook_url else "off")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._http is not None:
            await self._http.aclose()

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:
                logger.warning("Inbox poll failed: %s", describe_error(e))
            await asyncio.sleep(self.settings.sms_inbox_poll_seconds)

    # ── Poll: modem → archive ─────────────────────────────────

    def _authorized(self, sender: str) -> bool:
        cc = self.settings.sms_default_country_code
        try:
            number = normalize_phone(sender, cc)
        except ValueError:
            return False  # alphanumeric sender ("Orange") or garbage
        allowed = set()
        for r in self.settings.default_recipients_list:
            with contextlib.suppress(ValueError):
                allowed.add(normalize_phone(r, cc))
        return number in allowed

    def _record(self, msg: dict, now: datetime) -> InboxRecord:
        sender = str(msg.get("sender") or msg.get("number") or "unknown")
        text = str(msg.get("message") or msg.get("text") or "")
        raw_date = msg.get("date") or None
        modem_date = parse_modem_date(raw_date, self.settings.sms_modem_timezone)
        if modem_date is not None and modem_date > now + timedelta(minutes=5):
            # Future date → wrong modem clock or SMS_MODEM_TIMEZONE: don't trust it.
            logger.warning("inbox: modem date %r is in the future — check SMS_MODEM_TIMEZONE",
                           raw_date)
            modem_date = None
        if modem_date is not None:
            stale = modem_date < now - timedelta(seconds=self.settings.sms_inbox_stale_seconds)
        else:
            stale = self._first_poll  # unknown age: only trust what arrives while we run
        fp = hashlib.sha256(f"{sender}|{raw_date}|{text}".encode()).hexdigest()[:32]
        return InboxRecord(fingerprint=fp, received_at=now, sender=sender, message=text,
                           authorized=self._authorized(sender), stale=stale,
                           modem_date=modem_date, modem_date_raw=raw_date)

    async def poll_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        messages = await self.client.get_inbox()
        archived_indexes: dict[str, InboxRecord] = {}
        for msg in messages:
            rec = self._record(msg, now)
            try:
                rec.id = await asyncio.to_thread(self.store.add, rec)
            except Exception as e:
                logger.warning("inbox archive failed (kept on modem): %s", describe_error(e))
                continue
            if rec.id is not None:
                self.counters["received"] += 1
                self.counters["unauthorized"] += not rec.authorized
                self.counters["stale"] += rec.stale
                logger.info("SMS received #%s from %s%s%s: %s", rec.id, rec.sender,
                            "" if rec.authorized else " (unauthorized)",
                            " (stale)" if rec.stale else "", rec.message[:40])
            index = str(msg.get("id", msg.get("index", "")))
            if index:
                archived_indexes[index] = rec
        self._first_poll = False

        await self.deliver_pending(now)

        if self.settings.sms_inbox_delete_after_archive:
            await self._delete_from_modem(archived_indexes)

    async def _delete_from_modem(self, archived: dict[str, InboxRecord]) -> None:
        for index, rec in archived.items():
            if not self.store.durable and rec.id is not None and not self._delivered(rec):
                continue  # memory store: keep it on the modem until handed over
            try:
                if await self.client.delete_sms(index):
                    self.counters["deleted"] += 1
            except Exception as e:
                logger.warning("inbox: deleting modem index %s failed: %s", index, describe_error(e))
                return  # router trouble: try again next poll

    def _delivered(self, rec: InboxRecord) -> bool:
        fresh = self.store.get(rec.id) if rec.id is not None else None
        if fresh is None or fresh.stale:
            return True
        mqtt_ok = fresh.published_at is not None or not self._publishers
        hook_ok = fresh.forwarded_at is not None or not self.settings.sms_inbox_webhook_url
        return mqtt_ok and hook_ok

    # ── Deliver: archive → MQTT / webhook ─────────────────────

    async def deliver_pending(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(seconds=self.settings.sms_inbox_stale_seconds)
        webhook = bool(self.settings.sms_inbox_webhook_url)
        expired = await asyncio.to_thread(self.store.expire_undelivered, since, webhook)
        if expired:
            self.counters["expired"] += expired
            logger.warning("inbox: %d message(s) left the %ss delivery window undelivered — "
                           "kept in the archive, marked stale", expired,
                           self.settings.sms_inbox_stale_seconds)
        if self._publishers:
            for rec in await asyncio.to_thread(self.store.unpublished, since):
                if all([await p(rec) for p in self._publishers]):
                    await asyncio.to_thread(self.store.mark_published, rec.id, now)
                    self.counters["published"] += 1
                else:
                    break  # broker down: keep order, retry next poll
        if webhook:
            for rec in await asyncio.to_thread(self.store.unforwarded, since):
                if not await self._forward(rec, now):
                    break

    async def _forward(self, rec: InboxRecord, now: datetime) -> bool:
        s = self.settings
        headers = {"Content-Type": "application/json"}
        if s.sms_inbox_webhook_token:
            name = s.sms_inbox_webhook_auth_header
            headers[name] = (f"Bearer {s.sms_inbox_webhook_token}"
                             if name.lower() == "authorization" else s.sms_inbox_webhook_token)
        attempts = rec.forward_attempts + 1
        try:
            r = await self._http.post(s.sms_inbox_webhook_url, headers=headers,
                                      json=inbox_payload(rec, s.teltonika_modem, now))
            ok = 200 <= r.status_code < 300
            error = None if ok else f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            ok, error = False, describe_error(e)
        await asyncio.to_thread(self.store.mark_forwarded, rec.id, now if ok else None,
                                attempts, error)
        if ok:
            self.counters["forwarded"] += 1
        else:
            self.counters["forward_failures"] += 1
            log = logger.warning if attempts == 1 or attempts % 30 == 0 else logger.debug
            log("inbox #%s webhook attempt %d failed: %s", rec.id, attempts, error)
        return ok

    # ── Archive API ───────────────────────────────────────────

    async def recent(self, limit: int) -> list[InboxRecord]:
        return await asyncio.to_thread(self.store.recent, limit)

    async def get(self, rec_id: int) -> InboxRecord | None:
        return await asyncio.to_thread(self.store.get, rec_id)

    async def delete(self, rec_id: int) -> bool:
        return await asyncio.to_thread(self.store.delete, rec_id)

    def metrics(self) -> dict[str, int]:
        return {f"inbox_{k}": self.counters[k] for k in
                ("received", "unauthorized", "stale", "expired", "published", "forwarded",
                 "forward_failures", "deleted")}
