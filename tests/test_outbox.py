"""Outbox: queue on router failure, retry with backoff, expire after TTL."""

from datetime import timedelta

import pytest

from src.models.schemas import Priority
from src.services.send_limits import DuplicateMessage
from src.storage import MemoryOutboxStore
from tests.test_sms import make_service

NUMBER = "+40712345678"


class RouterDown:
    """Patchable send_sms: fails while ``down`` is True."""

    def __init__(self, client):
        self.client = client
        self.down = True
        self.calls = 0

    async def __call__(self, number, message):
        self.calls += 1
        if self.down:
            raise ConnectionError("All connection attempts failed")
        self.client.sent.append((number, message))
        return {"success": True}


def outbox_service(**kw):
    svc, client = make_service(outbox=True, **kw)
    router = RouterDown(client)
    client.send_sms = router
    return svc, client, router


@pytest.mark.asyncio
async def test_failed_send_is_queued_not_raised():
    svc, client, router = outbox_service()
    results = await svc.send(number=NUMBER, message="INTERNET DOWN")
    assert results[0].queued is True
    assert results[0].success is False
    assert results[0].outbox_id is not None
    assert svc.outbox.pending_count == 1
    assert svc.history[0].queued is True


@pytest.mark.asyncio
async def test_new_messages_queue_directly_while_outbox_busy():
    svc, client, router = outbox_service()
    await svc.send(number=NUMBER, message="first")
    calls = router.calls
    results = await svc.send(number=NUMBER, message="second")
    assert results[0].queued is True
    assert router.calls == calls  # no live attempt: answered immediately
    assert svc.outbox.pending_count == 2


@pytest.mark.asyncio
async def test_delivered_when_router_recovers():
    svc, client, router = outbox_service()
    done = []

    async def listener(item):
        done.append((item.id, item.status))

    svc.outbox.add_listener(listener)
    results = await svc.send(number=NUMBER, message="INTERNET DOWN")
    router.down = False
    await svc.outbox.process_due()
    assert client.sent == []  # live attempt just failed: waits for backoff
    item = svc.outbox.pending()[0]
    await svc.outbox.process_due(item.next_attempt_at)
    assert client.sent == [(NUMBER, "INTERNET DOWN")]
    assert svc.outbox.pending_count == 0
    assert done == [(results[0].outbox_id, "sent")]
    assert svc.history[0].success is True
    assert "delivered from outbox" in svc.history[0].detail


@pytest.mark.asyncio
async def test_retry_backoff_is_exponential_and_capped():
    svc, client, router = outbox_service()
    svc.settings.sms_outbox_max_backoff_seconds = 100
    await svc.send(number=NUMBER, message="x")
    item = svc.outbox.pending()[0]
    assert item.attempts == 1  # the live attempt counts
    delays = [(item.next_attempt_at - item.created_at).total_seconds()]
    now = item.next_attempt_at
    for _ in range(3):
        await svc.outbox.process_due(now)
        item = svc.outbox.pending()[0]
        delays.append((item.next_attempt_at - now).total_seconds())
        now = item.next_attempt_at
    assert delays == [30, 60, 100, 100]
    assert item.attempts == 4


@pytest.mark.asyncio
async def test_not_retried_before_due():
    svc, client, router = outbox_service()
    await svc.send(number=NUMBER, message="x")
    item = svc.outbox.pending()[0]
    calls = router.calls
    await svc.outbox.process_due(item.created_at + timedelta(seconds=10))
    assert router.calls == calls  # first retry only after 30s


@pytest.mark.asyncio
async def test_expires_after_ttl():
    svc, client, router = outbox_service()
    done = []

    async def listener(item):
        done.append(item.status)

    svc.outbox.add_listener(listener)
    await svc.send(number=NUMBER, message="old alert")
    item = svc.outbox.pending()[0]
    router.down = False
    await svc.outbox.process_due(item.created_at + timedelta(hours=6, seconds=1))
    assert client.sent == []  # never delivered late
    assert svc.outbox.pending_count == 0
    assert done == ["expired"]
    assert svc.history[0].success is False and "expired" in svc.history[0].detail


@pytest.mark.asyncio
async def test_duplicate_of_queued_message_rejected():
    svc, client, router = outbox_service(dedup_seconds=0)
    await svc.send(number=NUMBER, message="same")
    with pytest.raises(DuplicateMessage, match="outbox"):
        await svc.send(number=NUMBER, message="same")


@pytest.mark.asyncio
async def test_critical_delivered_first():
    svc, client, router = outbox_service()
    await svc.send(number=NUMBER, message="normal")
    await svc.send(number=NUMBER, message="critical", priority=Priority.critical)
    router.down = False
    await svc.outbox.process_due(svc.outbox.pending()[-1].next_attempt_at)
    assert [m for _, m in client.sent] == ["critical", "normal"]


@pytest.mark.asyncio
async def test_cancel():
    svc, client, router = outbox_service()
    results = await svc.send(number=NUMBER, message="x")
    assert await svc.outbox.cancel(results[0].outbox_id) is True
    assert await svc.outbox.cancel(results[0].outbox_id) is False
    router.down = False
    await svc.outbox.process_due()
    assert client.sent == []


@pytest.mark.asyncio
async def test_pending_survives_restart():
    store = MemoryOutboxStore()
    svc, client, router = outbox_service(outbox_store=store)
    await svc.send(number=NUMBER, message="before restart")

    svc2, client2, router2 = outbox_service(outbox_store=store)
    router2.down = False
    await svc2.outbox.start()
    await svc2.outbox.stop()
    assert svc2.outbox.pending_count == 1
    await svc2.outbox.process_due(svc2.outbox.pending()[0].next_attempt_at)
    assert client2.sent == [(NUMBER, "before restart")]
    assert store.load_pending() == []


@pytest.mark.asyncio
async def test_store_failure_keeps_item_in_memory():
    class BrokenStore(MemoryOutboxStore):
        def add(self, item):
            raise RuntimeError("mysql down")

    svc, client, router = outbox_service(outbox_store=BrokenStore())
    results = await svc.send(number=NUMBER, message="x")
    assert results[0].queued is True and results[0].outbox_id < 0
    router.down = False
    await svc.outbox.process_due(svc.outbox.pending()[0].next_attempt_at)
    assert client.sent == [(NUMBER, "x")]


@pytest.mark.asyncio
async def test_round_stops_at_first_connection_failure():
    svc, client, router = outbox_service()
    for m in ("a", "b", "c"):
        await svc.send(number=NUMBER, message=m)
    later = svc.outbox.pending()[0].next_attempt_at
    calls = router.calls
    await svc.outbox.process_due(later)
    assert router.calls == calls + 1  # router down: no point trying b and c


@pytest.mark.asyncio
async def test_rejection_does_not_stop_round():
    from src.services.teltonika import TeltonikaError

    svc, client, router = outbox_service()
    await svc.send(number=NUMBER, message="bad")
    await svc.send(number=NUMBER, message="good")

    async def reject_bad(number, message):
        if message == "bad":
            raise TeltonikaError("Router rejected SMS: invalid")
        client.sent.append((number, message))

    client.send_sms = reject_bad
    await svc.outbox.process_due(svc.outbox.pending()[0].next_attempt_at)
    assert client.sent == [(NUMBER, "good")]


@pytest.mark.asyncio
async def test_oldest_age_is_zero_when_empty_and_grows():
    svc, client, router = outbox_service()
    assert svc.outbox.oldest_age_seconds() == 0
    await svc.send(number=NUMBER, message="x")
    item = svc.outbox.pending()[0]
    assert svc.outbox.oldest_age_seconds(item.created_at + timedelta(seconds=700)) == 700
