import asyncio
import hashlib
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import Settings
from src.errors import describe_error  # noqa: F401 — re-exported for routers/mqtt
from src.gsm7 import segments_used, transliterate, truncate_to_segments
from src.models.schemas import Priority, SendHistoryEntry, SendSMSRequest
from src.phone import normalize_phone
from src.services.idempotency import IdempotencyCache, IdempotencyConflict, fingerprint  # noqa: F401
from src.services.outbox import Outbox, OutboxStore
from src.services.stats import classify, daily_stats
from src.services.teltonika import TeltonikaClient
from src.storage import MemoryOutboxStore, SendHistoryStore

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class DuplicateMessage(Exception):
    pass


@dataclass(frozen=True)
class SendOptions:
    """Optional fields of the send contract (see docs/API.md)."""

    dedup_key: str | None = None
    idempotency_key: str | None = None
    ttl_seconds: int | None = None
    max_segments: int | None = None
    source: str | None = None
    type: str | None = None


_NO_OPTIONS = SendOptions()


def options_from_request(req: SendSMSRequest) -> SendOptions:
    """Map the API/MQTT request onto service options (one place for both)."""
    return SendOptions(dedup_key=req.dedup_key, idempotency_key=req.idempotency_key,
                       ttl_seconds=req.ttl_seconds, max_segments=req.max_segments,
                       source=req.source, type=req.type)


class SlidingWindow:
    """Timestamps of sends within the last hour (monotonic clock)."""

    SPAN = 3600

    def __init__(self) -> None:
        self._ts: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._ts and self._ts[0] <= now - self.SPAN:
            self._ts.popleft()

    def check(self, now: float, limit: int, span: int, label: str) -> None:
        """Raise ``RateLimitExceeded`` if ``limit`` sends happened in the last ``span`` seconds."""
        self._prune(now)
        recent = [t for t in self._ts if t > now - span]
        if len(recent) >= limit:
            retry_after = max(1, int(recent[0] + span - now) + 1)
            raise RateLimitExceeded(f"Rate limit: {limit}/{label} exceeded", retry_after=retry_after)

    def add(self, now: float) -> None:
        self._ts.append(now)

    def usage(self, now: float, span: int) -> tuple[int, int]:
        """(sends in the last ``span`` s, seconds until the oldest of them leaves)."""
        self._prune(now)
        recent = [t for t in self._ts if t > now - span]
        frees_in = max(1, int(recent[0] + span - now) + 1) if recent else 0
        return len(recent), frees_in


class SMSService:
    def __init__(self, client: TeltonikaClient, settings: Settings,
                 history_store: SendHistoryStore | None = None,
                 outbox_store: OutboxStore | None = None,
                 idempotency: IdempotencyCache | None = None):
        self.client = client
        self.settings = settings
        self.idempotency = idempotency
        self.inbox = None  # InboxService, set by wiring.build_services (archive API + metrics)
        self.mqtt = None   # MQTTService, set by wiring.build_services (connection state)

        # Rate limiting — normal traffic and critical traffic have
        # separate windows. Critical bypasses the normal limits but
        # still has a hard hourly ceiling to protect the SIM credit.
        self._normal_window = SlidingWindow()
        self._critical_window = SlidingWindow()

        # Dedup — hash → expiry timestamp
        self._dedup_cache: dict[str, float] = {}

        # Counters since process start (published as metrics).
        self.counters: Counter[str] = Counter()
        self._started = time.monotonic()

        # Send history (most recent first) — in-memory cache, mirrored
        # write-through to ``history_store`` for durability across
        # container restarts.
        self.history: deque[SendHistoryEntry] = deque(maxlen=100)
        self._history_store = history_store
        if history_store is not None:
            try:
                self.history.extend(history_store.load_recent(limit=100))
                logger.info("sms history rehydrated: %d entries", len(self.history))
            except Exception as e:
                logger.warning("history rehydrate failed: %s", describe_error(e))

        self.outbox: Outbox | None = None
        if settings.sms_outbox_enabled:
            self.outbox = Outbox(client, outbox_store or MemoryOutboxStore(), settings,
                                 record_history=self._record)

    def _normalize_phone(self, number: str) -> str:
        return normalize_phone(number, self.settings.sms_default_country_code)

    @staticmethod
    def _dedup_key(number: str, message: str, alert_key: str | None = None) -> str:
        """Per-recipient dedup bucket: the producer's alert key if given,
        else the text (so "SOC 14.1 %" / "SOC 14.8 %" differ without a key)."""
        basis = f"key:{alert_key}" if alert_key else f"text:{message}"
        return hashlib.sha256(f"{number}|{basis}".encode()).hexdigest()[:16]

    def _prepare_text(self, message: str, max_segments: int | None) -> str:
        """What actually goes to the modem: GSM-7 transliteration + segment cap."""
        text = transliterate(message) if self.settings.sms_gsm7_transliterate else message
        cap = max_segments or self.settings.sms_max_segments
        return truncate_to_segments(text, cap) if cap else text

    def _reserve(self, number: str, message: str, priority: Priority,
                 alert_key: str | None = None) -> str | None:
        """Check rate limit + dedup and reserve the slot in one step.

        There is no ``await`` between the check and the reservation, so
        concurrent requests can't both pass the check (asyncio is
        single-threaded). Returns the dedup key that was reserved (None
        for critical, which bypasses dedup).
        """
        now = time.monotonic()
        s = self.settings

        if priority == Priority.critical:
            self._critical_window.check(now, s.sms_rate_limit_critical_hour, 3600, "hour (critical)")
            self._critical_window.add(now)
            return None

        self._normal_window.check(now, s.sms_rate_limit_minute, 60, "min")
        self._normal_window.check(now, s.sms_rate_limit_hour, 3600, "hour")

        self._dedup_cache = {k: v for k, v in self._dedup_cache.items() if v > now}
        key = self._dedup_key(number, message, alert_key)
        if key in self._dedup_cache:
            raise DuplicateMessage(
                f"Duplicate SMS to {number} within {s.sms_dedup_seconds}s — skipped"
            )
        if self.outbox is not None and self.outbox.has_pending(key):
            raise DuplicateMessage(f"Duplicate SMS to {number} already queued in outbox — skipped")

        self._normal_window.add(now)
        self._dedup_cache[key] = now + s.sms_dedup_seconds
        return key

    def _resolve_numbers(self, number: str | None) -> list[str]:
        """Resolve and normalize target numbers. Falls back to default recipients."""
        if number:
            return [self._normalize_phone(number)]
        recipients = self.settings.default_recipients_list
        if not recipients:
            raise ValueError("No phone number provided and SMS_DEFAULT_RECIPIENTS not configured")
        return [self._normalize_phone(r) for r in recipients]

    def _record(self, entry: SendHistoryEntry) -> None:
        self.history.appendleft(entry)
        if self._history_store is not None:
            self._history_store.record(entry)

    async def send(
        self, message: str, number: str | None = None, priority: Priority = Priority.normal,
        options: SendOptions | None = None,
    ) -> list[SendHistoryEntry]:
        """Send SMS to one or more numbers.

        Every recipient is attempted even if an earlier one fails. If
        all recipients failed, the first error is raised (so the HTTP /
        MQTT layer can map it); otherwise the per-recipient results are
        returned, including failures. With ``options.idempotency_key`` a
        retry returns the first accepted result instead of sending again.
        """
        opts = options or _NO_OPTIONS
        if opts.idempotency_key and self.idempotency is not None:
            fp = fingerprint(number or "", message, priority.value)
            results = await self.idempotency.run(
                opts.idempotency_key, fp, lambda: self._send(message, number, priority, opts))
            if results and results[0].replayed:
                self.counters["replays"] += 1
            return results
        return await self._send(message, number, priority, opts)

    async def _send(self, message: str, number: str | None, priority: Priority,
                    opts: SendOptions) -> list[SendHistoryEntry]:
        numbers = self._resolve_numbers(number)
        text = self._prepare_text(message, opts.max_segments)
        results: list[SendHistoryEntry] = []
        errors: list[Exception] = []

        for num in numbers:
            entry, error = await self._send_single(num, text, priority, opts)
            results.append(entry)
            if error is not None:
                errors.append(error)

        if len(errors) == len(numbers):
            raise errors[0]
        return results

    async def _send_single(
        self, number: str, message: str, priority: Priority, opts: SendOptions = _NO_OPTIONS,
    ) -> tuple[SendHistoryEntry, Exception | None]:
        """Send to a single (normalized) number. Never raises; returns the error instead."""
        now = datetime.now(timezone.utc)

        def entry(success: bool, detail: str | None, segments: int | None = None) -> SendHistoryEntry:
            return SendHistoryEntry(
                number=number, message=message, priority=priority.value,
                success=success, timestamp=now, detail=detail,
                source=opts.source, type=opts.type, segments=segments,
            )

        try:
            dedup_key = self._reserve(number, message, priority, opts.dedup_key)
        except (RateLimitExceeded, DuplicateMessage) as e:
            blocked = entry(False, str(e))
            self.counters["blocked_rate_limit" if isinstance(e, RateLimitExceeded)
                          else "blocked_duplicate"] += 1
            self._record(blocked)
            logger.info("SMS blocked: %s → %s — %s", number, message[:40], e)
            return blocked, e

        if self.outbox is not None and self.outbox.pending_count:
            # Router was recently unreachable: queue behind the earlier
            # messages (keeps order, answers the caller immediately).
            return await self._queue(number, message, priority, opts,
                                     "queued behind earlier undelivered messages",
                                     attempted=False), None

        try:
            result = await self.client.send_sms(number, message)
        except Exception as e:
            if self.outbox is not None:
                return await self._queue(number, message, priority, opts, describe_error(e),
                                         attempted=True), None
            # The rate-limit slot stays consumed so a broken caller can't
            # retry-storm uncapped during a modem outage. The dedup entry
            # is released so a legitimate retry can land after recovery.
            if dedup_key is not None:
                self._dedup_cache.pop(dedup_key, None)
            failed = entry(False, describe_error(e))
            self.counters["failed"] += 1
            self._record(failed)
            logger.error("SMS failed: %s → %s — %s", number, message[:40], describe_error(e))
            return failed, e

        segments = segments_used(result, message)
        sent = entry(True, str(result) if result else None, segments)
        self.counters["sent"] += 1
        self.counters["segments"] += segments
        self._record(sent)
        logger.info("SMS sent: %s → %s [%s, %d part(s)]", number, message[:40],
                    priority.value, segments)
        return sent, None

    async def _queue(self, number: str, message: str, priority: Priority, opts: SendOptions,
                     reason: str, attempted: bool) -> SendHistoryEntry:
        """Hand the SMS to the outbox. The rate slot and dedup entry stay
        reserved: the message is accepted, just delivered later."""
        item = await self.outbox.enqueue(
            number, message, priority.value, self._dedup_key(number, message, opts.dedup_key),
            reason, attempted, ttl_seconds=opts.ttl_seconds, source=opts.source, type=opts.type,
        )
        queued = SendHistoryEntry(
            number=number, message=message, priority=priority.value, success=False,
            timestamp=datetime.now(timezone.utc), queued=True, outbox_id=item.id,
            detail=f"queued in outbox #{item.id}: {reason}", source=opts.source, type=opts.type,
        )
        self.counters["queued"] += 1
        self._record(queued)
        return queued

    def budget(self) -> dict:
        """Live rate-limit usage per window, plus the dedup window."""
        now, s = time.monotonic(), self.settings

        def window(win: SlidingWindow, limit: int, span: int) -> dict:
            used, frees_in = win.usage(now, span)
            return {"used": used, "limit": limit,
                    "frees_in_seconds": frees_in if used >= limit else 0,
                    "oldest_leaves_in_seconds": frees_in}

        return {
            "normal_minute": window(self._normal_window, s.sms_rate_limit_minute, 60),
            "normal_hour": window(self._normal_window, s.sms_rate_limit_hour, 3600),
            "critical_hour": window(self._critical_window, s.sms_rate_limit_critical_hour, 3600),
            "dedup_seconds": s.sms_dedup_seconds,
            "dedup_active": sum(1 for v in self._dedup_cache.values() if v > now),
        }

    def connections(self) -> dict:
        s = self.settings
        inbox = self.inbox.metrics() if self.inbox is not None else {}
        return {
            "mqtt": {"enabled": bool(s.mqtt_broker.strip()),
                     "connected": bool(self.mqtt and self.mqtt.connected),
                     "broker": f"{s.mqtt_broker}:{s.mqtt_port}" if s.mqtt_broker.strip() else ""},
            "webhook": {"enabled": bool(s.sms_inbox_webhook_url),
                        "forwarded": inbox.get("inbox_forwarded", 0),
                        "failures": inbox.get("inbox_forward_failures", 0)},
            "history_store": "MySQL" if self._history_store is not None else "memory",
        }

    def _local_day_start(self, days: int) -> datetime:
        zone = ZoneInfo(self.settings.tz)
        first = datetime.now(zone).date() - timedelta(days=days - 1)
        return datetime(first.year, first.month, first.day, tzinfo=zone).astimezone(timezone.utc)

    async def stats(self, days: int) -> dict:
        """Per-day outcomes and segments + per-type totals (MySQL; else the
        in-memory last 100, flagged ``partial``)."""
        since = self._local_day_start(days)
        if self._history_store is not None:
            rows = await asyncio.to_thread(self._history_store.rows_since, since)
            partial = False
        else:
            rows = [{"ts": e.timestamp, "success": e.success, "detail": e.detail,
                     "segments": e.segments, "type": e.type, "source": e.source,
                     "priority": e.priority} for e in self.history if e.timestamp >= since]
            partial = True
        return {**daily_stats(rows, days, self.settings.tz), "partial": partial}

    async def search(self, *, q: str | None = None, type: str | None = None,
                     source: str | None = None, status: str | None = None,
                     priority: str | None = None, since: datetime | None = None,
                     until: datetime | None = None, limit: int = 50,
                     offset: int = 0) -> list[SendHistoryEntry]:
        if self._history_store is not None:
            return await asyncio.to_thread(
                self._history_store.search, q=q, type=type, source=source, status=status,
                priority=priority, since=since, until=until, limit=limit, offset=offset)
        ql = (q or "").lower()
        hits = [e for e in self.history
                if (not ql or ql in e.message.lower() or ql in e.number)
                and (not type or e.type == type) and (not source or e.source == source)
                and (not status or classify(e.success, e.detail) == status)
                and (not priority or e.priority == priority)
                and (not since or e.timestamp >= since) and (not until or e.timestamp < until)]
        return hits[offset:offset + limit]

    def metrics(self) -> dict[str, int | float]:
        """Flat numeric snapshot: counters since start, outbox, last known signal."""
        out: dict[str, int | float] = {
            "uptime_seconds": round(time.monotonic() - self._started),
            **{k: self.counters[k] for k in
               ("sent", "failed", "queued", "blocked_rate_limit", "blocked_duplicate")},
            "segments_sent": self.counters["segments"]
            + (self.outbox.counters["segments"] if self.outbox is not None else 0),
            "idempotent_replays": self.counters["replays"],
            "mqtt_connected": int(bool(self.mqtt and self.mqtt.connected)),
        }
        b = self.budget()
        out.update({"rate_normal_minute_used": b["normal_minute"]["used"],
                    "rate_normal_hour_used": b["normal_hour"]["used"],
                    "rate_critical_hour_used": b["critical_hour"]["used"]})
        if self.outbox is not None:
            out["outbox_pending"] = self.outbox.pending_count
            out["outbox_oldest_age_seconds"] = self.outbox.oldest_age_seconds()
            out.update({f"outbox_{k}": self.outbox.counters[k]
                        for k in ("delivered", "expired", "cancelled")})
        if self.inbox is not None:
            out.update(self.inbox.metrics())
        status = self.client.cached_status()
        out.update({k: status[k] for k in ("rssi", "rsrp", "sinr", "rsrq", "modem_temperature")
                    if isinstance(status.get(k), int | float)})
        return out

    async def get_status(self) -> dict:
        return await self.client.get_status()
