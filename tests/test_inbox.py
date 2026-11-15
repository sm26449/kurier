"""Inbound transport: modem → archive → MQTT / webhook, exactly once."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.services.inbox import InboxService, inbox_payload, parse_modem_date
from src.storage import MemoryInboxStore
from tests.test_sms import make_service

ME = "+40700000001"  # in SMS_DEFAULT_RECIPIENTS of make_service()
TZ = "UTC"  # what the RUT241 prints


def modem_date(dt_utc: datetime) -> str:
    """Format a UTC instant the way gsmctl prints modem local time."""
    from zoneinfo import ZoneInfo
    return dt_utc.astimezone(ZoneInfo(TZ)).strftime("%a %b %e %H:%M:%S %Y")


class FakeModem:
    def __init__(self):
        self.messages: list[dict] = []
        self.deleted: list[str] = []
        self.fail_delete = False

    def receive(self, index, sender, text, when):
        self.messages.append({"id": str(index), "sender": sender, "message": text,
                              "date": modem_date(when), "read": False})

    async def get_inbox(self):
        return [dict(m) for m in self.messages]

    async def delete_sms(self, index):
        if self.fail_delete:
            raise ConnectionError("router down")
        self.deleted.append(index)
        self.messages = [m for m in self.messages if m["id"] != index]
        return True


def make_inbox(store=None, webhook=None, handler=None):
    svc, _ = make_service()
    s = svc.settings
    s.sms_inbox_webhook_url = webhook or ""
    s.sms_inbox_webhook_token = "tok" if webhook else ""
    modem = FakeModem()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    inbox = InboxService(modem, store or MemoryInboxStore(), s, http=http)
    published = []

    async def publisher(rec):
        published.append(rec)
        return True

    inbox.add_publisher(publisher)
    return inbox, modem, published


def test_parse_modem_date_handles_double_space_and_tz():
    assert parse_modem_date("Tue Mar  3 20:13:00 2026", "UTC") == datetime(2026, 3, 3, 20, 13,
                                                                            tzinfo=timezone.utc)
    assert parse_modem_date("Tue Mar  3 20:13:00 2026", "Europe/Bucharest") == datetime(
        2026, 3, 3, 18, 13, tzinfo=timezone.utc)
    assert parse_modem_date("garbage", TZ) is None and parse_modem_date(None, TZ) is None


@pytest.mark.asyncio
async def test_real_rut241_timestamp_is_fresh():
    # Production capture 2026-11-14: SMS sent 22:01 EET, archived 20:01:27 UTC.
    inbox, modem, published = make_inbox()
    modem.messages.append({"id": "0", "sender": ME, "message": "Test inbox",
                           "date": "Sat Nov 14 20:01:19 2026"})
    await inbox.poll_once(datetime(2026, 11, 14, 20, 1, 27, tzinfo=timezone.utc))
    assert [(r.message, r.stale) for r in published] == [("Test inbox", False)]


@pytest.mark.asyncio
async def test_future_modem_date_is_not_trusted():
    inbox, modem, published = make_inbox()
    inbox.settings.sms_modem_timezone = "Pacific/Kiritimati"  # wrong setting: UTC+14
    now = datetime.now(timezone.utc)
    await inbox.poll_once(now)  # first poll done (nothing on the modem)
    modem.receive(0, ME, "x", now)  # stamped in UTC, read as UTC+14 → looks 14h old
    inbox.settings.sms_modem_timezone = "Etc/GMT+12"  # read as UTC-12 → 12h in the future
    await inbox.poll_once(now)
    assert [(r.message, r.stale) for r in published] == [("x", False)]


@pytest.mark.asyncio
async def test_new_message_archived_published_once_and_deleted_from_modem():
    store = MemoryInboxStore()
    inbox, modem, published = make_inbox(store)
    now = datetime.now(timezone.utc)
    modem.receive(0, ME, "STATUS", now - timedelta(seconds=30))
    await inbox.poll_once(now)
    await inbox.poll_once(now + timedelta(seconds=20))

    assert [r.message for r in published] == ["STATUS"]
    rec = published[0]
    assert rec.authorized is True and rec.stale is False
    assert modem.deleted == ["0"] and modem.messages == []
    assert store.recent(10)[0].published_at is not None
    assert inbox.metrics()["inbox_received"] == 1


@pytest.mark.asyncio
async def test_old_message_is_stale_archived_not_delivered():
    inbox, modem, published = make_inbox()
    now = datetime.now(timezone.utc)
    modem.receive(1, ME, "STOP", now - timedelta(hours=3))  # sent during an outage
    await inbox.poll_once(now)
    assert published == []
    assert inbox.store.recent(1)[0].stale is True


@pytest.mark.asyncio
async def test_unknown_sender_flagged_not_authorized():
    inbox, modem, published = make_inbox()
    now = datetime.now(timezone.utc)
    modem.receive(2, "Orange", "Promo", now)
    modem.receive(3, "+40799999999", "STATUS", now)
    await inbox.poll_once(now)
    assert [(r.sender, r.authorized) for r in published] == [("Orange", False),
                                                             ("+40799999999", False)]


@pytest.mark.asyncio
async def test_reused_modem_index_is_a_new_message():
    inbox, modem, published = make_inbox()
    now = datetime.now(timezone.utc)
    modem.receive(0, ME, "first", now)
    await inbox.poll_once(now)
    modem.receive(0, ME, "second", now + timedelta(seconds=5))  # modem reuses index 0
    await inbox.poll_once(now + timedelta(seconds=20))
    assert [r.message for r in published] == ["first", "second"]


@pytest.mark.asyncio
async def test_broker_down_retries_in_order():
    inbox, modem, published = make_inbox()
    up = {"ok": False}

    async def flaky(rec):
        if up["ok"]:
            published.append(rec)
        return up["ok"]

    inbox._publishers = [flaky]
    now = datetime.now(timezone.utc)
    modem.receive(0, ME, "a", now)
    modem.receive(1, ME, "b", now)
    await inbox.poll_once(now)
    assert published == []
    up["ok"] = True
    await inbox.poll_once(now + timedelta(seconds=20))
    assert [r.message for r in published] == ["a", "b"]


@pytest.mark.asyncio
async def test_memory_store_keeps_undelivered_message_on_modem():
    inbox, modem, published = make_inbox()

    async def down(rec):
        return False

    inbox._publishers = [down]
    now = datetime.now(timezone.utc)
    modem.receive(0, ME, "a", now)
    await inbox.poll_once(now)
    assert modem.deleted == []  # not durable + not delivered → stays on the SIM


@pytest.mark.asyncio
async def test_webhook_forward_with_token_and_klopot_shape():
    seen = []

    def handler(request):
        seen.append((request.headers.get("X-Input-Token"), json.loads(request.content)))
        return httpx.Response(202)

    inbox, modem, published = make_inbox(webhook="http://klopot/v1/inputs/9/webhook",
                                         handler=handler)
    now = datetime.now(timezone.utc)
    modem.receive(0, ME, "STATUS", now)
    await inbox.poll_once(now)
    await inbox.poll_once(now + timedelta(seconds=20))
    assert len(seen) == 1
    token, body = seen[0]
    assert token == "tok"
    assert body["from"] == ME and body["body"] == "STATUS" and body["authorized"] is True
    assert {"received_at", "modem", "id", "stale"} <= body.keys()
    assert inbox.store.recent(1)[0].forwarded_at is not None


@pytest.mark.asyncio
async def test_webhook_failure_retried_until_ttl():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] < 3 else 200)

    inbox, modem, published = make_inbox(webhook="http://hook", handler=handler)
    now = datetime.now(timezone.utc)
    modem.receive(0, ME, "x", now)
    for i in range(4):
        await inbox.poll_once(now + timedelta(seconds=20 * i))
    rec = inbox.store.recent(1)[0]
    assert calls["n"] == 3 and rec.forwarded_at is not None and rec.forward_attempts == 3

    # A message that leaves the delivery window undelivered is given up.
    modem.receive(1, ME, "late", now)
    calls["n"] = -100  # keep failing
    await inbox.poll_once(now)
    await inbox.deliver_pending(now + timedelta(seconds=601))
    late = inbox.store.recent(1)[0]
    assert late.forwarded_at is None and late.stale is True
    assert inbox.metrics()["inbox_expired"] == 1


@pytest.mark.asyncio
async def test_archive_failure_keeps_message_on_modem():
    class Broken(MemoryInboxStore):
        def add(self, rec):
            raise RuntimeError("mysql down")

    inbox, modem, published = make_inbox(Broken())
    modem.receive(0, ME, "x", datetime.now(timezone.utc))
    await inbox.poll_once()
    assert modem.deleted == [] and published == []


def test_payload_keeps_legacy_mqtt_fields():
    from src.storage import InboxRecord
    now = datetime.now(timezone.utc)
    rec = InboxRecord(fingerprint="f", received_at=now, sender=ME, message="hi",
                      authorized=True, stale=False, id=7)
    p = inbox_payload(rec, "1-1")
    assert p["message"] == p["body"] == "hi" and p["timestamp"] == p["received_at"]


def test_inbox_api_serves_the_archive(monkeypatch):
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.config import settings
    from src.routers import sms

    monkeypatch.setattr(settings, "sms_api_key", "")

    monkeypatch.setattr(settings, "sms_allow_no_auth", True)
    svc, _ = make_service()
    inbox, modem, published = make_inbox()
    svc.inbox = inbox
    modem.receive(0, ME, "STATUS", datetime.now(timezone.utc))
    asyncio.run(inbox.poll_once())
    app = FastAPI()
    app.include_router(sms.router)
    app.dependency_overrides[sms.get_sms_service] = lambda: svc
    client = TestClient(app)

    listing = client.get("/api/sms/inbox").json()
    assert listing["count"] == 1
    item = listing["messages"][0]
    assert item["message"] == "STATUS" and item["authorized"] and item["published"]
    assert client.get(f"/api/sms/inbox/{item['id']}").json()["sender"] == ME
    assert client.delete(f"/api/sms/inbox/{item['id']}").status_code == 200
    assert client.get(f"/api/sms/inbox/{item['id']}").status_code == 404



@pytest.mark.asyncio
async def test_broker_outage_longer_than_window_drops_delivery():
    inbox, modem, published = make_inbox()
    up = {"ok": False}

    async def flaky(rec):
        if up["ok"]:
            published.append(rec)
        return up["ok"]

    inbox._publishers = [flaky]
    now = datetime.now(timezone.utc)
    modem.receive(0, ME, "STOP", now)
    await inbox.poll_once(now)            # broker down
    up["ok"] = True
    await inbox.poll_once(now + timedelta(minutes=15))  # back, but the command is 15 min old
    assert published == []
    assert inbox.store.recent(1)[0].stale is True


@pytest.mark.asyncio
async def test_webhook_enabled_later_does_not_backfill_old_messages():
    # Production 2026-11-14: TEST 2 (sent 20:02:49) reached Klopot at 20:11:46
    # when the webhook was switched on. Now only the window is delivered.
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["body"])
        return httpx.Response(202)

    inbox, modem, published = make_inbox()
    now = datetime.now(timezone.utc)
    modem.receive(0, ME, "old", now)
    await inbox.poll_once(now)                       # MQTT only
    inbox.settings.sms_inbox_webhook_url = "http://hook"
    inbox._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    modem.receive(1, ME, "new", now + timedelta(minutes=12))
    await inbox.poll_once(now + timedelta(minutes=12))
    assert seen == ["new"]


def test_payload_age_seconds():
    from src.storage import InboxRecord
    now = datetime.now(timezone.utc)
    rec = InboxRecord(fingerprint="f", received_at=now, sender=ME, message="hi", authorized=True,
                      stale=False, modem_date=now - timedelta(seconds=42), id=1)
    assert inbox_payload(rec, "1-1", now)["age_seconds"] == 42
