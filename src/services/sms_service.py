"""Send pipeline: options → text → reserve (limits) → router, or the
outbox when the router is down; every attempt goes to the history.

Limits live in ``send_limits.py``, the history and its queries in
``send_history.py``; this module wires them around one send and builds
the status/metrics snapshot.
"""
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import Settings
from src.errors import describe_error
from src.gsm7 import segments_used, transliterate, truncate_to_segments
from src.models.schemas import Priority, SendHistoryEntry, SendSMSRequest
from src.phone import normalize_phone
from src.services.idempotency import IdempotencyCache, fingerprint
from src.services.inbox import InboxService
from src.services.mqtt_status import MQTTStatus
from src.services.outbox import Outbox, OutboxStore
from src.services.send_history import SendHistory
from src.services.send_limits import DuplicateMessage, RateLimitExceeded, SendLimits
from src.services.teltonika import TeltonikaClient
from src.storage import MemoryOutboxStore, SendHistoryStore

logger = logging.getLogger(__name__)


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


class SMSService:
    def __init__(self, client: TeltonikaClient, settings: Settings,
                 history_store: SendHistoryStore | None = None,
                 outbox_store: OutboxStore | None = None,
                 idempotency: IdempotencyCache | None = None,
                 inbox: InboxService | None = None,
                 mqtt_status: MQTTStatus | None = None):
        self.client = client
        self.settings = settings
        self.idempotency = idempotency
        self.inbox = inbox  # archive API + inbox metrics; None = inbox not running
        # Written by MQTTService; a fresh one reads "not connected".
        self.mqtt_status = mqtt_status or MQTTStatus()

        self.limits = SendLimits(settings)
        # Most recent first; also stats() and search() for the API.
        self.history = SendHistory(history_store, settings.tz)

        # Counters since process start (published as metrics).
        self.counters: Counter[str] = Counter()
        self._started = time.monotonic()

        self.outbox: Outbox | None = None
        if settings.sms_outbox_enabled:
            self.outbox = Outbox(client, outbox_store or MemoryOutboxStore(), settings,
                                 record_history=self.history.record)

    def _normalize_phone(self, number: str) -> str:
        return normalize_phone(number, self.settings.sms_default_country_code)

    def _prepare_text(self, message: str, max_segments: int | None) -> str:
        """What actually goes to the modem: GSM-7 transliteration + segment cap."""
        text = transliterate(message) if self.settings.sms_gsm7_transliterate else message
        cap = max_segments or self.settings.sms_max_segments
        return truncate_to_segments(text, cap) if cap else text

    def _resolve_numbers(self, number: str | None) -> list[str]:
        """Resolve and normalize target numbers. Falls back to default recipients."""
        if number:
            return [self._normalize_phone(number)]
        recipients = self.settings.default_recipients_list
        if not recipients:
            raise ValueError("No phone number provided and SMS_DEFAULT_RECIPIENTS not configured")
        return [self._normalize_phone(r) for r in recipients]

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
            dedup_key = self.limits.reserve(
                number, message, priority, opts.dedup_key,
                is_queued=self.outbox.has_pending if self.outbox is not None else None)
        except (RateLimitExceeded, DuplicateMessage) as e:
            blocked = entry(False, str(e))
            self.counters["blocked_rate_limit" if isinstance(e, RateLimitExceeded)
                          else "blocked_duplicate"] += 1
            self.history.record(blocked)
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
            if dedup_key is not None:
                self.limits.release_dedup(dedup_key)
            failed = entry(False, describe_error(e))
            self.counters["failed"] += 1
            self.history.record(failed)
            logger.error("SMS failed: %s → %s — %s", number, message[:40], describe_error(e))
            return failed, e

        segments = segments_used(result, message)
        sent = entry(True, str(result) if result else None, segments)
        self.counters["sent"] += 1
        self.counters["segments"] += segments
        self.history.record(sent)
        logger.info("SMS sent: %s → %s [%s, %d part(s)]", number, message[:40],
                    priority.value, segments)
        return sent, None

    async def _queue(self, number: str, message: str, priority: Priority, opts: SendOptions,
                     reason: str, attempted: bool) -> SendHistoryEntry:
        """Hand the SMS to the outbox. The rate slot and dedup entry stay
        reserved: the message is accepted, just delivered later."""
        item = await self.outbox.enqueue(
            number, message, priority.value, self.limits.dedup_key(number, message, opts.dedup_key),
            reason, attempted, ttl_seconds=opts.ttl_seconds, source=opts.source, type=opts.type,
        )
        queued = SendHistoryEntry(
            number=number, message=message, priority=priority.value, success=False,
            timestamp=datetime.now(timezone.utc), queued=True, outbox_id=item.id,
            detail=f"queued in outbox #{item.id}: {reason}", source=opts.source, type=opts.type,
        )
        self.counters["queued"] += 1
        self.history.record(queued)
        return queued

    def connections(self) -> dict:
        s = self.settings
        inbox = self.inbox.metrics() if self.inbox is not None else {}
        return {
            "mqtt": {"enabled": bool(s.mqtt_broker.strip()),
                     "connected": self.mqtt_status.connected,
                     "broker": f"{s.mqtt_broker}:{s.mqtt_port}" if s.mqtt_broker.strip() else ""},
            "webhook": {"enabled": bool(s.sms_inbox_webhook_url),
                        "forwarded": inbox.get("inbox_forwarded", 0),
                        "failures": inbox.get("inbox_forward_failures", 0)},
            "history_store": "MySQL" if self.history.durable else "memory",
        }

    def metrics(self) -> dict[str, int | float]:
        """Flat numeric snapshot: counters since start, outbox, last known signal."""
        out: dict[str, int | float] = {
            "uptime_seconds": round(time.monotonic() - self._started),
            **{k: self.counters[k] for k in
               ("sent", "failed", "queued", "blocked_rate_limit", "blocked_duplicate")},
            "segments_sent": self.counters["segments"]
            + (self.outbox.counters["segments"] if self.outbox is not None else 0),
            "idempotent_replays": self.counters["replays"],
            "mqtt_connected": int(self.mqtt_status.connected),
        }
        b = self.limits.budget()
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
