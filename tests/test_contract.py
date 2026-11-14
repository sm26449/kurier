"""Extended send contract: GSM-7, segments, severity, dedup_key, idempotency, TTL."""

import asyncio
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.config import settings
from src.gsm7 import segment_count, segments_used, transliterate, truncate_to_segments
from src.models.schemas import Priority, SendSMSRequest
from src.routers import health, sms
from src.services.housekeeping import Housekeeping
from src.services.idempotency import IdempotencyCache
from src.services.sms_service import DuplicateMessage, SendOptions
from src.storage import MemoryIdempotencyStore
from tests.test_sms import make_service

NUMBER = "+40712345678"
RO = "Baterie descărcată sub 15 % — verificați invertorul și siguranța… „urgent”"


# ── GSM-7 ─────────────────────────────────────────────────────


def test_transliterate_romanian_and_typography():
    out = transliterate(RO)
    assert out == 'Baterie descarcata sub 15 % - verificati invertorul si siguranta... "urgent"'
    assert segment_count(out) == 1


def test_alert_of_320_chars_drops_from_5_to_3_segments():
    text = ("Container CRITICAL down — un container Tier-1 este oprit; verificați ș ț ă. " * 5)[:320]
    assert segment_count(text) == 5          # UCS-2: 67 chars per part
    assert segment_count(transliterate(text)) == 3  # GSM-7: 153 chars per part


def test_extension_chars_count_double_and_emoji_stays_ucs2():
    assert segment_count("[" * 80) == 1  # 160 septets: exactly one part
    assert segment_count("{" * 81) == 2
    assert segment_count("ok 🔋") == 1
    assert transliterate("🔋") == "🔋"


def test_truncate_to_segments_fits_and_marks():
    long = "x" * 1000
    cut = truncate_to_segments(long, 2)
    assert segment_count(cut) == 2 and cut.endswith("...")
    assert truncate_to_segments("short", 1) == "short"


def test_segments_used_prefers_router_report():
    assert segments_used({"success": True, "data": {"sms_used": 4}}, "x") == 4
    assert segments_used({"success": True}, "x" * 200) == 2


# ── Request schema ────────────────────────────────────────────


@pytest.mark.parametrize("payload,expected", [
    ({"severity": "critical"}, Priority.critical),
    ({"severity": "CRITICAL"}, Priority.critical),
    ({"severity": "warning"}, Priority.normal),
    ({"severity": "info"}, Priority.normal),
    ({"priority": "urgent"}, Priority.normal),        # unknown → normal, never 422
    ({"priority": "normal", "severity": "critical"}, Priority.normal),  # explicit priority wins
    ({}, Priority.normal),
])
def test_priority_mapping(payload, expected):
    assert SendSMSRequest(message="m", **payload).priority == expected


def test_empty_optional_strings_become_none_and_unknown_fields_ignored():
    req = SendSMSRequest(message="m", dedup_key="", source="", future_field=1)
    assert req.dedup_key is None and req.source is None


@pytest.mark.parametrize("given,field,expected", [
    ({"source": "has space/é"}, "source", "has_space__"),
    ({"type": "x" * 80}, "type", "x" * 64),
    ({"ttl_seconds": 10}, "ttl_seconds", 60),
    ({"ttl_seconds": "21600"}, "ttl_seconds", 21600),      # rendered as a string by a template
    ({"ttl_seconds": "soon"}, "ttl_seconds", None),
    ({"max_segments": 0}, "max_segments", 1),
    ({"max_segments": 99}, "max_segments", 10),
    ({"dedup_key": "k" * 200}, "dedup_key", "k" * 128),
    ({"dedup_key": "soc_critical:None"}, "dedup_key", "soc_critical:None"),
])
def test_optional_metadata_is_sanitized_never_rejected(given, field, expected):
    assert getattr(SendSMSRequest(message="m", **given), field) == expected


def test_only_missing_message_is_rejected_and_long_text_is_cut():
    with pytest.raises(ValidationError):
        SendSMSRequest(message="")
    assert len(SendSMSRequest(message="x" * 5000).message) == 1600


# ── Service behaviour ─────────────────────────────────────────


def recording_service(**kw):
    svc, client = make_service(**kw)
    svc.idempotency = IdempotencyCache(MemoryIdempotencyStore(), 3600)
    return svc, client


@pytest.mark.asyncio
async def test_text_is_transliterated_before_sending():
    svc, client = recording_service()
    await svc.send(number=NUMBER, message="Siguranța a sărit — verificați")
    assert client.sent == [(NUMBER, "Siguranta a sarit - verificati")]


@pytest.mark.asyncio
async def test_transliteration_can_be_disabled():
    svc, client = recording_service()
    svc.settings.sms_gsm7_transliterate = False
    await svc.send(number=NUMBER, message="ăș")
    assert client.sent == [(NUMBER, "ăș")]


@pytest.mark.asyncio
async def test_max_segments_truncates():
    svc, client = recording_service()
    await svc.send(number=NUMBER, message="y" * 900, options=SendOptions(max_segments=2))
    sent = client.sent[0][1]
    assert segment_count(sent) == 2 and sent.endswith("...")


@pytest.mark.asyncio
async def test_dedup_key_groups_changing_text():
    svc, client = recording_service()
    await svc.send(number=NUMBER, message="SOC 14.1 %", options=SendOptions(dedup_key="soc_low"))
    with pytest.raises(DuplicateMessage):
        await svc.send(number=NUMBER, message="SOC 13.9 %", options=SendOptions(dedup_key="soc_low"))
    # Another alert with the same text but a different key is not a duplicate.
    await svc.send(number=NUMBER, message="SOC 14.1 %", options=SendOptions(dedup_key="other"))
    assert len(client.sent) == 2


@pytest.mark.asyncio
async def test_idempotent_retry_does_not_send_or_consume_rate():
    svc, client = recording_service(rate_minute=1)
    first = await svc.send(number=NUMBER, message="a", options=SendOptions(idempotency_key="d-1"))
    again = await svc.send(number=NUMBER, message="a", options=SendOptions(idempotency_key="d-1"))
    assert len(client.sent) == 1
    assert first[0].replayed is False and again[0].replayed is True
    assert again[0].success is True
    assert svc.metrics()["idempotent_replays"] == 1


@pytest.mark.asyncio
async def test_concurrent_requests_with_same_key_send_once():
    svc, client = recording_service()

    async def slow(number, message):
        await asyncio.sleep(0.02)
        client.sent.append((number, message))
        return {"success": True}

    client.send_sms = slow
    a, b = await asyncio.gather(
        svc.send(number=NUMBER, message="x", options=SendOptions(idempotency_key="k")),
        svc.send(number=NUMBER, message="x", options=SendOptions(idempotency_key="k")),
    )
    assert len(client.sent) == 1
    assert sorted([a[0].replayed, b[0].replayed]) == [False, True]


@pytest.mark.asyncio
async def test_blocked_request_is_not_remembered():
    svc, client = recording_service(rate_minute=1)
    await svc.send(number=NUMBER, message="first")
    for _ in range(2):  # retry is re-evaluated, still blocked, nothing stored
        with pytest.raises(Exception):
            await svc.send(number=NUMBER, message="second", options=SendOptions(idempotency_key="z"))
    assert "z" not in svc.idempotency._done


@pytest.mark.asyncio
async def test_queued_result_is_replayed_as_queued():
    svc, client = recording_service(outbox=True)
    svc.idempotency = IdempotencyCache(MemoryIdempotencyStore(), 3600)

    async def down(number, message):
        raise ConnectionError("down")

    client.send_sms = down
    first = await svc.send(number=NUMBER, message="x", options=SendOptions(idempotency_key="q"))
    again = await svc.send(number=NUMBER, message="x", options=SendOptions(idempotency_key="q"))
    assert first[0].queued and again[0].queued and again[0].replayed
    assert svc.outbox.pending_count == 1


@pytest.mark.asyncio
async def test_idempotency_survives_restart():
    store = MemoryIdempotencyStore()
    svc, client = make_service()
    svc.idempotency = IdempotencyCache(store, 3600)
    await svc.send(number=NUMBER, message="x", options=SendOptions(idempotency_key="r"))

    svc2, client2 = make_service()
    svc2.idempotency = IdempotencyCache(store, 3600)
    await svc2.idempotency.load()
    again = await svc2.send(number=NUMBER, message="x", options=SendOptions(idempotency_key="r"))
    assert client2.sent == [] and again[0].replayed


@pytest.mark.asyncio
async def test_segments_counted_from_router_report():
    svc, client = recording_service()

    async def billed(number, message):
        return {"success": True, "data": {"sms_used": 4}}

    client.send_sms = billed
    res = await svc.send(number=NUMBER, message="x", options=SendOptions(source="klopot", type="t1"))
    assert res[0].segments == 4 and res[0].source == "klopot" and res[0].type == "t1"
    assert svc.metrics()["segments_sent"] == 4


@pytest.mark.asyncio
async def test_per_message_ttl_in_outbox():
    svc, client = make_service(outbox=True)

    async def down(number, message):
        raise ConnectionError("down")

    client.send_sms = down
    await svc.send(number=NUMBER, message="short-lived", options=SendOptions(ttl_seconds=300))
    item = svc.outbox.pending()[0]
    assert (item.expires_at - item.created_at) == timedelta(seconds=300)
    await svc.outbox.process_due(item.created_at + timedelta(seconds=301))
    assert svc.outbox.pending_count == 0 and svc.history[0].detail.startswith("expired")


@pytest.mark.asyncio
async def test_outbox_delivery_keeps_labels_and_segments():
    svc, client = make_service(outbox=True)
    calls = {"n": 0}

    async def flaky(number, message):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("down")
        return {"success": True, "data": {"sms_used": 2}}

    client.send_sms = flaky
    await svc.send(number=NUMBER, message="x", options=SendOptions(source="klopot", type="t"))
    await svc.outbox.process_due(svc.outbox.pending()[0].next_attempt_at)
    delivered = svc.history[0]
    assert delivered.success and delivered.source == "klopot" and delivered.segments == 2
    assert svc.metrics()["segments_sent"] == 2


@pytest.mark.asyncio
async def test_housekeeping_prunes_idempotency():
    svc, _ = make_service()
    cache = IdempotencyCache(MemoryIdempotencyStore(), 0)
    svc.idempotency = cache
    await svc.send(number=NUMBER, message="x", options=SendOptions(idempotency_key="old"))
    result = await Housekeeping(svc.settings, None, None, cache).run_once()
    assert result == {"sms_idempotency": 1}


# ── HTTP + MQTT ───────────────────────────────────────────────


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "sms_api_key", "")
    svc, fake = recording_service()
    app = FastAPI()
    app.include_router(sms.router)
    app.include_router(health.router)
    app.dependency_overrides[sms.get_sms_service] = lambda: svc
    app.dependency_overrides[health.get_teltonika_client] = lambda: fake
    return TestClient(app), svc, fake


def test_full_klopot_style_body(api):
    client, svc, fake = api
    body = {"phone": "0712345678", "message": "Container CRITICAL down — mosquitto",
            "severity": "critical", "dedup_key": "container_down_mosquitto_v1",
            "idempotency_key": "dlv-661", "ttl_seconds": 3600, "max_segments": 3,
            "source": "klopot", "type": "container_down_critical", "future": "ignored"}
    r = client.post("/api/sms/send", json=body)
    assert r.status_code == 200
    assert fake.sent == [(NUMBER, "Container CRITICAL down - mosquitto")]
    assert r.json()[0]["segments"] == 1 and r.json()[0]["replayed"] is False
    assert svc.history[0].priority == "critical" and svc.history[0].type == "container_down_critical"

    again = client.post("/api/sms/send", json=body)
    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()[0]["replayed"] is True and len(fake.sent) == 1


@pytest.mark.asyncio
async def test_mqtt_uses_the_same_contract():
    from src.services.mqtt import MQTTService

    svc, client = recording_service(rate_minute=1)
    mqtt = MQTTService(svc, svc.settings)
    published = []

    async def capture(topic, payload, *, retain=False):
        published.append(payload)

    mqtt._publish = capture

    class Msg:
        def __init__(self, data):
            self.payload = __import__("json").dumps(data).encode()

    await mqtt._handle_message(Msg({"message": "a", "number": NUMBER}))
    # Rate-limited for normal, but severity=critical uses the critical budget.
    await mqtt._handle_message(Msg({"message": "b", "number": NUMBER, "severity": "critical"}))
    await mqtt._handle_message(Msg({"message": "", "number": NUMBER}))  # invalid → ignored
    assert [m for _, m in client.sent] == ["a", "b"]
    assert all(p["success"] for p in published) and len(published) == 2


@pytest.mark.asyncio
async def test_same_key_different_request_is_a_conflict():
    from src.services.idempotency import IdempotencyConflict

    svc, client = recording_service()
    await svc.send(number=NUMBER, message="alert", options=SendOptions(idempotency_key="n-1"))
    with pytest.raises(IdempotencyConflict):  # e.g. escalation reusing the alert's key
        await svc.send(number=NUMBER, message="ESCALATION: alert",
                       options=SendOptions(idempotency_key="n-1"))
    assert len(client.sent) == 1


def test_conflict_maps_to_422(api):
    client, svc, fake = api
    client.post("/api/sms/send", json={"message": "a", "phone": NUMBER, "idempotency_key": "k1"})
    r = client.post("/api/sms/send", json={"message": "b", "phone": NUMBER, "idempotency_key": "k1"})
    assert r.status_code == 422 and "different request" in r.json()["detail"]
