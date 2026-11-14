import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class Priority(str, Enum):
    normal = "normal"
    critical = "critical"


_LABEL_BAD = re.compile(r"[^A-Za-z0-9_.:-]")
_MAX_MESSAGE = 1600


def _clean_label(value, max_len: int) -> str | None:
    """Labels are metadata: sanitize, never reject (an alert must not be lost)."""
    if value is None:
        return None
    text = _LABEL_BAD.sub("_", str(value).strip())[:max_len]
    return text or None


def _clean_key(value, max_len: int = 128) -> str | None:
    if value is None:
        return None
    text = str(value).strip()[:max_len]
    return text or None


def _clamp_int(value, lo: int, hi: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        return min(max(int(float(value)), lo), hi)
    except (TypeError, ValueError):
        return None


class SendSMSRequest(BaseModel):
    """Send contract (docs/API.md). Only ``message`` is required.

    Robustness rule: optional metadata never causes a rejection — unknown
    fields are ignored, bad labels are sanitized, out-of-range numbers are
    clamped, an unparsable number is dropped, an over-long text is cut.
    A producer filling these from templates can't lose an alert to a 422.
    """

    number: str | None = Field(
        None, description="Phone number (alias: `phone`). If omitted, uses SMS_DEFAULT_RECIPIENTS"
    )
    message: str = Field(..., min_length=1, max_length=_MAX_MESSAGE)
    priority: Priority = Field(
        Priority.normal,
        description="`critical` uses the critical budget and skips dedup; anything else is normal. "
                    "`severity` (info|warning|critical) is accepted instead.",
    )
    dedup_key: str | None = Field(
        None, description="Identifies the alert (e.g. rule): at most one SMS per number+key per "
                          "SMS_DEDUP_SECONDS, even if the text changes. Default: the text. ≤128 chars.",
    )
    idempotency_key: str | None = Field(
        None, description="Retries with the same key get the first result (sent/queued) "
                          "without sending again. Kept 24h. ≤128 chars.",
    )
    ttl_seconds: int | None = Field(
        None, description="If queued in the outbox, drop it after this long (clamped 60–86400). "
                          "Default SMS_OUTBOX_TTL_SECONDS.",
    )
    max_segments: int | None = Field(
        None, description="Truncate the text to fit this many SMS parts (clamped 1–10).",
    )
    source: str | None = Field(None, description="Producer label, e.g. `klopot` (≤32, [A-Za-z0-9_.:-]).")
    type: str | None = Field(None, description="Alert type label, e.g. the template type (≤64).")

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        # Klopot's SMS channel posts {"phone": ..., "message": ...}.
        if not data.get("number") and data.get("phone"):
            data["number"] = data["phone"]
        # Lenient priority: severity words map to the two budgets; an
        # unknown value is "normal", never a 422 that drops an alert.
        raw = data.get("priority")
        if raw in (None, "") and data.get("severity") is not None:
            raw = data["severity"]
        data["priority"] = (Priority.critical if str(raw or "").strip().lower() == "critical"
                            else Priority.normal)
        if isinstance(data.get("message"), str):
            data["message"] = data["message"][:_MAX_MESSAGE]
        data["dedup_key"] = _clean_key(data.get("dedup_key"))
        data["idempotency_key"] = _clean_key(data.get("idempotency_key"))
        data["source"] = _clean_label(data.get("source"), 32)
        data["type"] = _clean_label(data.get("type"), 64)
        data["ttl_seconds"] = _clamp_int(data.get("ttl_seconds"), 60, 86400)
        data["max_segments"] = _clamp_int(data.get("max_segments"), 1, 10)
        return data


class SendSMSResponse(BaseModel):
    success: bool
    message_id: str | None = None
    detail: str | None = None
    number: str | None = None
    timestamp: datetime | None = None
    queued: bool = False
    outbox_id: int | None = None
    segments: int | None = None
    replayed: bool = False


class InboxMessage(BaseModel):
    id: str
    sender: str
    message: str
    date: str                      # when sent (modem clock, ISO) — falls back to archive time
    read: bool = False
    received_at: datetime | None = None  # when the gateway archived it
    authorized: bool = False       # sender in SMS_DEFAULT_RECIPIENTS
    stale: bool = False            # too old on arrival → archived, not delivered
    published: bool = False        # MQTT sms-gateway/inbox
    forwarded: bool = False        # webhook
    forward_error: str | None = None


class InboxListResponse(BaseModel):
    messages: list[InboxMessage]
    count: int


class ModemStatus(BaseModel):
    signal_strength: int | None = None   # RSSI, dBm
    rsrp: int | None = None
    rsrq: int | None = None
    sinr: int | None = None
    operator: str | None = None
    sim_state: str | None = None
    connection_type: str | None = None
    band: str | None = None              # e.g. "LTE B3"
    modem_temperature: float | None = None  # °C
    modem_model: str | None = None
    imei: str | None = None


class HealthResponse(BaseModel):
    status: str
    modem_reachable: bool
    sim_ok: bool
    api_mode: str | None = None
    uptime_seconds: float | None = None
    # Always numeric (0 when empty / disabled): consumers compare them,
    # and a null in a Klopot comparison is an eval error.
    outbox_pending: int = 0
    outbox_oldest_age_seconds: int = 0
    detail: str | None = None


class SendHistoryEntry(BaseModel):
    number: str
    message: str
    priority: str
    success: bool
    timestamp: datetime
    detail: str | None = None
    queued: bool = False
    outbox_id: int | None = None
    source: str | None = None
    type: str | None = None
    segments: int | None = None
    # Response-only: true when served from the idempotency cache.
    replayed: bool = False


class OutboxEntry(BaseModel):
    id: int
    number: str
    message: str
    priority: str
    created_at: datetime
    attempts: int
    next_attempt_at: datetime
    last_error: str | None = None


class RateLimitError(BaseModel):
    detail: str
    retry_after_seconds: int | None = None
