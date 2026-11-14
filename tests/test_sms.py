"""Basic tests for SMS service logic."""

import time

import pytest

from src.config import Settings
from src.models.schemas import Priority
from src.services.sms_service import DuplicateMessage, RateLimitExceeded, SMSService


class FakeTeltonikaClient:
    """Mock Teltonika client for testing."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.api_mode = "rest"
        self.fail_next = False

    async def send_sms(self, number: str, message: str) -> dict:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("Simulated failure")
        self.sent.append((number, message))
        return {"success": True}

    async def get_inbox(self) -> list[dict]:
        return []

    async def read_sms(self, msg_id: str) -> dict | None:
        return None

    async def delete_sms(self, msg_id: str) -> bool:
        return True

    async def get_status(self) -> dict:
        return {"signal_strength": -65, "operator": "Test", "sim_state": "ready"}

    async def is_reachable(self) -> bool:
        return True

    def cached_status(self) -> dict:
        return {"rssi": -65, "rsrp": -95, "sinr": None}


def make_service(rate_hour=10, rate_minute=3, dedup_seconds=300, outbox=False,
                 outbox_store=None) -> tuple[SMSService, FakeTeltonikaClient]:
    client = FakeTeltonikaClient()
    settings = Settings(
        teltonika_host="127.0.0.1",
        teltonika_pass="test",
        sms_rate_limit_hour=rate_hour,
        sms_rate_limit_minute=rate_minute,
        sms_dedup_seconds=dedup_seconds,
        sms_default_recipients="+40700000001,+40700000002",
        sms_outbox_enabled=outbox,
    )
    return SMSService(client, settings, outbox_store=outbox_store), client


@pytest.mark.asyncio
async def test_send_single():
    svc, client = make_service()
    results = await svc.send(number="+40712345678", message="Hello")
    assert len(results) == 1
    assert results[0].success is True
    assert client.sent == [("+40712345678", "Hello")]


@pytest.mark.asyncio
async def test_send_default_recipients():
    svc, client = make_service()
    results = await svc.send(message="Alert!", number=None)
    assert len(results) == 2
    assert client.sent[0][0] == "+40700000001"
    assert client.sent[1][0] == "+40700000002"


@pytest.mark.asyncio
async def test_rate_limit_per_minute():
    svc, client = make_service(rate_minute=2)
    await svc.send(number="+40712345678", message="Msg 1")
    await svc.send(number="+40712345678", message="Msg 2")
    with pytest.raises(RateLimitExceeded):
        await svc.send(number="+40712345678", message="Msg 3")


@pytest.mark.asyncio
async def test_rate_limit_critical_bypasses():
    svc, client = make_service(rate_minute=1)
    await svc.send(number="+40712345678", message="Msg 1")
    # Normal would be blocked
    with pytest.raises(RateLimitExceeded):
        await svc.send(number="+40712345678", message="Msg 2", priority=Priority.normal)
    # Critical bypasses
    results = await svc.send(number="+40712345678", message="Critical!", priority=Priority.critical)
    assert results[0].success is True


@pytest.mark.asyncio
async def test_dedup():
    svc, client = make_service(dedup_seconds=300)
    await svc.send(number="+40712345678", message="Same message")
    with pytest.raises(DuplicateMessage):
        await svc.send(number="+40712345678", message="Same message")


@pytest.mark.asyncio
async def test_dedup_critical_bypasses():
    svc, client = make_service(dedup_seconds=300)
    await svc.send(number="+40712345678", message="Alert")
    # Critical bypasses dedup
    results = await svc.send(number="+40712345678", message="Alert", priority=Priority.critical)
    assert results[0].success is True


@pytest.mark.asyncio
async def test_history_recorded():
    svc, client = make_service()
    await svc.send(number="+40712345678", message="Test")
    assert len(svc.history) == 1
    assert svc.history[0].success is True
    assert svc.history[0].number == "+40712345678"


@pytest.mark.asyncio
async def test_send_failure_recorded():
    svc, client = make_service()
    client.fail_next = True
    with pytest.raises(ConnectionError):
        await svc.send(number="+40712345678", message="Fail")
    assert len(svc.history) == 1
    assert svc.history[0].success is False


@pytest.mark.asyncio
async def test_no_recipients_error():
    client = FakeTeltonikaClient()
    settings = Settings(
        teltonika_host="127.0.0.1",
        teltonika_pass="test",
        sms_default_recipients="",
    )
    svc = SMSService(client, settings)
    with pytest.raises(ValueError, match="No phone number"):
        await svc.send(message="Test", number=None)


# ── Phone normalization / validation ──────────────────────────


@pytest.mark.parametrize("raw", [
    "+40712345678", "0040712345678", "0712345678", "0712 345 678", "+40-712-345-678",
])
def test_normalize_phone_variants(raw):
    from src.phone import normalize_phone
    assert normalize_phone(raw, "+40") == "+40712345678"


@pytest.mark.parametrize("raw", ["abc", "", "12", "+0712345678", "0722"])
def test_normalize_phone_rejects_invalid(raw):
    from src.phone import normalize_phone
    with pytest.raises(ValueError):
        normalize_phone(raw, "+40")


@pytest.mark.asyncio
async def test_number_normalized_before_send():
    svc, client = make_service()
    results = await svc.send(number="0712 345 678", message="Hi")
    assert client.sent == [("+40712345678", "Hi")]
    assert results[0].number == "+40712345678"


@pytest.mark.asyncio
async def test_invalid_number_rejected_without_sending():
    svc, client = make_service()
    with pytest.raises(ValueError):
        await svc.send(number="not-a-number", message="Hi")
    assert client.sent == []


@pytest.mark.asyncio
async def test_dedup_across_number_formats():
    svc, client = make_service()
    await svc.send(number="+40712345678", message="Same")
    with pytest.raises(DuplicateMessage):
        await svc.send(number="0712345678", message="Same")


# ── Concurrency / reservation ─────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_duplicates_send_once():
    import asyncio

    svc, client = make_service()

    async def slow_send(number, message):
        await asyncio.sleep(0.01)
        client.sent.append((number, message))
        return {"success": True}

    client.send_sms = slow_send
    outcomes = await asyncio.gather(
        svc.send(number="+40712345678", message="Burst"),
        svc.send(number="+40712345678", message="Burst"),
        return_exceptions=True,
    )
    assert len(client.sent) == 1
    assert sum(isinstance(o, DuplicateMessage) for o in outcomes) == 1


@pytest.mark.asyncio
async def test_failure_releases_dedup_but_keeps_rate_slot():
    svc, client = make_service(rate_minute=2)
    client.fail_next = True
    with pytest.raises(ConnectionError):
        await svc.send(number="+40712345678", message="Retry me")
    # Same payload may be retried (dedup released) …
    await svc.send(number="+40712345678", message="Retry me")
    # … but the failed attempt still consumed a rate slot.
    with pytest.raises(RateLimitExceeded):
        await svc.send(number="+40712345678", message="Third")


@pytest.mark.asyncio
async def test_rate_limit_retry_after_is_accurate():
    svc, client = make_service(rate_minute=1)
    await svc.send(number="+40712345678", message="One")
    with pytest.raises(RateLimitExceeded) as exc:
        await svc.send(number="+40712345678", message="Two")
    assert 1 <= exc.value.retry_after <= 61


# ── Critical ceiling ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_critical_has_hard_ceiling():
    svc, client = make_service()
    svc.settings.sms_rate_limit_critical_hour = 2
    for i in range(2):
        await svc.send(number="+40712345678", message=f"C{i}", priority=Priority.critical)
    with pytest.raises(RateLimitExceeded, match="critical"):
        await svc.send(number="+40712345678", message="C3", priority=Priority.critical)


@pytest.mark.asyncio
async def test_critical_does_not_consume_normal_slots():
    svc, client = make_service(rate_minute=1)
    await svc.send(number="+40712345678", message="C", priority=Priority.critical)
    results = await svc.send(number="+40712345678", message="N")
    assert results[0].success is True


# ── Multiple recipients ───────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_failure_still_tries_all_recipients():
    svc, client = make_service()
    client.fail_next = True  # first recipient fails
    results = await svc.send(message="Alert", number=None)
    assert [r.success for r in results] == [False, True]
    assert client.sent == [("+40700000002", "Alert")]


@pytest.mark.asyncio
async def test_all_recipients_failing_raises():
    svc, client = make_service()

    async def always_fail(number, message):
        raise ConnectionError("down")

    client.send_sms = always_fail
    with pytest.raises(ConnectionError):
        await svc.send(message="Alert", number=None)
    assert len(svc.history) == 2


# ── Error description ─────────────────────────────────────────


def test_describe_error_falls_back_to_type():
    import httpx

    from src.services.sms_service import describe_error
    assert describe_error(httpx.ReadTimeout("")) == "ReadTimeout"
    assert describe_error(ValueError("boom")) == "boom"
