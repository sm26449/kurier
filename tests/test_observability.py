"""Metrics, MQTT publishing, inbox dedup, housekeeping, async history writes."""

import threading
from datetime import datetime, timezone

import pytest

from src.models.schemas import SendHistoryEntry
from src.services.housekeeping import Housekeeping
from src.services.mqtt import TOPIC_AVAILABILITY, TOPIC_SENT, MQTTService
from src.services.sms_service import DuplicateMessage, RateLimitExceeded
from src.storage import MemoryOutboxStore, SendHistoryStore
from tests.test_sms import make_service

NUMBER = "+40712345678"


# ── Metrics ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_counters_and_signal():
    svc, client = make_service(rate_minute=2, outbox=True)
    await svc.send(number=NUMBER, message="a")
    with pytest.raises(DuplicateMessage):
        await svc.send(number=NUMBER, message="a")
    await svc.send(number=NUMBER, message="b")
    with pytest.raises(RateLimitExceeded):
        await svc.send(number=NUMBER, message="c")

    m = svc.metrics()
    assert m["sent"] == 2
    assert m["blocked_duplicate"] == 1
    assert m["blocked_rate_limit"] == 1
    assert m["outbox_pending"] == 0
    assert m["rssi"] == -65 and m["rsrp"] == -95
    assert "sinr" not in m  # non-numeric values dropped (Telegraf json parser)
    assert all(isinstance(v, int | float) for v in m.values())


@pytest.mark.asyncio
async def test_metrics_outbox_counters():
    svc, client = make_service(outbox=True)

    async def down(number, message):
        raise ConnectionError("down")

    client.send_sms = down
    await svc.send(number=NUMBER, message="x")
    m = svc.metrics()
    assert m["queued"] == 1 and m["outbox_pending"] == 1

    async def up(number, message):
        return {"success": True}

    client.send_sms = up
    await svc.outbox.process_due(svc.outbox.pending()[0].next_attempt_at)
    m = svc.metrics()
    assert m["outbox_delivered"] == 1 and m["outbox_pending"] == 0


# ── MQTT ──────────────────────────────────────────────────────


def mqtt_service(svc):
    mqtt = MQTTService(svc, svc.settings)
    published = []

    async def capture(topic, payload, *, retain=False):
        published.append((topic, payload, retain))

    mqtt._publish = capture
    return mqtt, published


@pytest.mark.asyncio
async def test_outbox_outcome_published():
    svc, client = make_service(outbox=True)
    mqtt, published = mqtt_service(svc)

    async def down(number, message):
        raise ConnectionError("down")

    client.send_sms = down
    await svc.send(number=NUMBER, message="x")

    async def up(number, message):
        return {}

    client.send_sms = up
    await svc.outbox.process_due(svc.outbox.pending()[0].next_attempt_at)
    [(topic, payload, retain)] = published
    assert topic == TOPIC_SENT
    assert payload["from_outbox"] is True and payload["status"] == "sent" and payload["success"]


def test_last_will_is_retained_offline():
    svc, _ = make_service()
    will = MQTTService(svc, svc.settings)._client_kwargs()["will"]
    assert will.topic == TOPIC_AVAILABILITY
    assert will.payload == "offline" and will.retain is True


# ── Housekeeping ──────────────────────────────────────────────


class FakeHistory:
    def __init__(self):
        self.calls = []

    def prune_older_than_days(self, days):
        self.calls.append(days)
        return 3


@pytest.mark.asyncio
async def test_housekeeping_prunes_both_stores():
    svc, _ = make_service()
    store = MemoryOutboxStore()
    now = datetime.now(timezone.utc)
    from src.storage import OutboxItem
    for status in ("pending", "sent", "expired"):
        store.add(OutboxItem(number=NUMBER, message=status, priority="normal", dedup_key="k",
                             created_at=now, next_attempt_at=now, status=status))
    history = FakeHistory()
    result = await Housekeeping(svc.settings, history, store).run_once()
    assert result == {"sms_log": 3, "sms_outbox": 2}
    assert history.calls == [svc.settings.sms_history_retention_days]
    assert [i.message for i in store.load_pending()] == ["pending"]


@pytest.mark.asyncio
async def test_housekeeping_survives_store_errors():
    svc, _ = make_service()

    class Broken:
        def prune_older_than_days(self, days):
            raise RuntimeError("mysql down")

    assert await Housekeeping(svc.settings, Broken(), None).run_once() == {}


# ── History store writes off the event loop ───────────────────


def test_history_record_is_non_blocking_and_ordered():
    gate = threading.Event()
    written = []

    class SlowDB:
        def run(self, fn):
            class Cur:
                def execute(self, sql, params=None):
                    if sql.startswith("INSERT"):
                        gate.wait(5)  # only writes are slow; the schema probe is not
                        written.append(params[2])  # message column

            return fn(Cur())

    store = SendHistoryStore(SlowDB())
    for m in ("one", "two", "three"):
        store.record(SendHistoryEntry(number=NUMBER, message=m, priority="normal",
                                      success=True, timestamp=datetime.now(timezone.utc)))
    assert written == []  # record() returned while the DB was still blocked
    gate.set()
    store.close()  # flushes
    assert written == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_mqtt_is_off_without_a_broker():
    svc, _ = make_service()
    svc.settings.mqtt_broker = ""
    mqtt = MQTTService(svc, svc.settings)
    await mqtt.start()
    assert mqtt.enabled is False and mqtt._task is None and mqtt.connected is False
    assert svc.connections()["mqtt"] == {"enabled": False, "connected": False, "broker": ""}
    await mqtt.stop()


def test_mqtt_connection_state_reaches_status_and_metrics():
    svc, _ = make_service()
    mqtt = MQTTService(svc, svc.settings)
    assert svc.connections()["mqtt"]["connected"] is False and svc.metrics()["mqtt_connected"] == 0
    mqtt._set_client(object())  # what the connection loop does once connected
    assert mqtt.connected and svc.connections()["mqtt"]["connected"] is True
    assert svc.metrics()["mqtt_connected"] == 1
    mqtt._set_client(None)      # connection lost
    assert svc.connections()["mqtt"]["connected"] is False and svc.metrics()["mqtt_connected"] == 0


@pytest.mark.asyncio
async def test_mqtt_enabled_with_a_broker():
    svc, _ = make_service()
    svc.settings.mqtt_broker = "mosquitto"
    assert MQTTService(svc, svc.settings).enabled is True
    assert svc.connections()["mqtt"]["broker"].startswith("mosquitto:")
