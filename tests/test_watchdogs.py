"""Watchdogs (docs/adr/0001-watchdogs.md): transitions, flapping limit,
restart grace, recipients, validation and auth."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import Settings, settings
from src.routers import watchdogs as watchdogs_router
from src.services.sms_service import SMSService
from src.services.watchdogs import RENOTIFY, WatchdogService
from src.storage import MemoryWatchdogStore, WatchdogRecord
from tests.test_sms import FakeTeltonikaClient

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now += timedelta(**kw)
        return self.now


class Modem(FakeTeltonikaClient):
    def __init__(self) -> None:
        super().__init__()
        self.age: float | None = 10.0
        self.status = {"signal_strength": -71, "operator": "Orange RO", "sim_state": "inserted"}
        self.status_queries = 0

    def status_age_seconds(self):
        return self.age

    def cached_status(self) -> dict:
        return dict(self.status)

    async def get_status(self) -> dict:
        self.status_queries += 1
        self.age = 0.0
        return dict(self.status)


def make(store=None, clock=None, **kw):
    clock = clock or Clock()
    kw = {"sms_default_recipients": "+40700000001", **kw}
    s = Settings(teltonika_host="127.0.0.1", teltonika_pass="x", sms_outbox_enabled=False,
                 tz="Europe/Bucharest", sms_rate_limit_critical_hour=1000, **kw)
    modem = Modem()
    sms = SMSService(modem, s)
    dogs = WatchdogService(sms, modem, store or MemoryWatchdogStore(), s, clock=clock)
    return dogs, modem, clock


async def settle(dogs: WatchdogService) -> None:
    """Wait for the SMS the service sends in the background."""
    while dogs._sending:
        await asyncio.gather(*list(dogs._sending))


def texts(modem) -> list[str]:
    return [m for _, m in modem.sent]


# ── transitions ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_ping_registers_up_and_sends_nothing():
    dogs, modem, _ = make()
    await dogs.load()
    rec = await dogs.ping("klopot-sms_main", 180)
    await settle(dogs)
    assert rec.state == "up" and rec.timeout_sec == 180
    assert [r.name for r in dogs.list()] == ["klopot-sms_main"]
    assert modem.sent == []


@pytest.mark.asyncio
async def test_silence_past_the_timeout_goes_down_with_one_sms():
    dogs, modem, clock = make()
    await dogs.load()
    await dogs.ping("backup", 60)
    clock.advance(seconds=59)
    await dogs.check_once()
    assert dogs.get("backup").state == "up"
    clock.advance(seconds=2)
    await dogs.check_once()
    await settle(dogs)
    assert dogs.get("backup").state == "down" and dogs.get("backup").down_reason == "timeout"
    # last ping 12:00 UTC = 14:00 in Bucharest (TZ)
    assert texts(modem) == ["⚠ backup: no check-in since 2026-11-17 14:00"]
    for _ in range(5):
        clock.advance(minutes=10)
        await dogs.check_once()
    await settle(dogs)
    assert len(modem.sent) == 1  # one SMS per outage, however long it lasts


@pytest.mark.asyncio
async def test_fail_ping_goes_down_at_once_with_the_detail():
    dogs, modem, _ = make()
    await dogs.load()
    await dogs.ping("klopot-sms_main", 180)
    rec = await dogs.ping("klopot-sms_main", 180, "fail", "rule engine stuck")
    await settle(dogs)
    assert rec.state == "down" and rec.down_reason == "fail"
    assert texts(modem) == ["⚠ klopot-sms_main: rule engine stuck"]
    await dogs.ping("klopot-sms_main", 180, "fail", "still stuck")
    await settle(dogs)
    assert len(modem.sent) == 1


@pytest.mark.asyncio
async def test_ok_ping_while_down_comes_back_with_an_sms():
    dogs, modem, clock = make()
    await dogs.load()
    await dogs.ping("nas", 60)
    clock.advance(seconds=61)
    await dogs.check_once()
    await settle(dogs)
    rec = await dogs.ping("nas", 60)
    await settle(dogs)
    assert rec.state == "up" and rec.down_since is None
    assert texts(modem)[-1] == "✅ nas is back"
    assert len(modem.sent) == 2


@pytest.mark.asyncio
async def test_watchdog_sms_use_the_normal_send_path_as_critical():
    dogs, _, _ = make()
    await dogs.load()
    await dogs.ping("nas", 60, "fail", "disk full")
    await settle(dogs)
    entry = dogs.sms.history[0]
    assert entry.success and entry.priority == "critical"
    assert entry.source == "kurier" and entry.type == "watchdog_down"


# ── flapping ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flapping_sends_at_most_one_down_sms_per_30_minutes():
    dogs, modem, clock = make()
    await dogs.load()
    await dogs.ping("flappy", 60, "fail", "x")         # down, SMS 1
    clock.advance(minutes=2)
    await dogs.ping("flappy", 60)                       # up, "back"
    clock.advance(minutes=2)
    await dogs.ping("flappy", 60, "fail", "x")         # down again: inside 30 min, silent
    clock.advance(minutes=2)
    await dogs.ping("flappy", 60)                       # up: its down SMS never went out → silent
    await settle(dogs)
    assert texts(modem) == ["⚠ flappy: x", "✅ flappy is back"]


@pytest.mark.asyncio
async def test_an_outage_silenced_by_the_flapping_limit_is_alerted_when_it_ends():
    dogs, modem, clock = make()
    await dogs.load()
    await dogs.ping("flappy", 60, "fail", "x")
    clock.advance(minutes=1)
    await dogs.ping("flappy", 60)
    clock.advance(minutes=1)
    await dogs.ping("flappy", 60, "fail", "broken again")  # silent for now
    await settle(dogs)
    assert len(modem.sent) == 2
    clock.now = T0 + RENOTIFY - timedelta(seconds=1)
    await dogs.ping("flappy", 60, "fail", "broken again")
    await dogs.check_once()
    await settle(dogs)
    assert len(modem.sent) == 2
    clock.now = T0 + RENOTIFY
    await dogs.check_once()
    await settle(dogs)
    assert texts(modem)[-1] == "⚠ flappy: broken again"


# ── restart ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_after_a_restart_timers_start_from_the_restart():
    store = MemoryWatchdogStore()
    store.save(WatchdogRecord(name="old", timeout_sec=120, last_ping=T0 - timedelta(days=1),
                              created_at=T0 - timedelta(days=2)))
    clock = Clock()
    dogs, modem, _ = make(store=store, clock=clock)
    await dogs.load()
    clock.advance(seconds=119)
    await dogs.check_once()
    assert dogs.get("old").state == "up" and modem.sent == []
    clock.advance(seconds=2)
    await dogs.check_once()
    await settle(dogs)
    assert dogs.get("old").state == "down" and len(modem.sent) == 1


@pytest.mark.asyncio
async def test_a_restart_keeps_an_alerted_outage_without_alerting_again():
    store = MemoryWatchdogStore()
    dogs, modem, clock = make(store=store)
    await dogs.load()
    await dogs.ping("nas", 60, "fail", "x")
    await settle(dogs)
    assert len(modem.sent) == 1

    again, modem2, _ = make(store=store, clock=Clock(clock.advance(hours=2)))
    await again.load()
    await again.check_once()
    await settle(again)
    assert again.get("nas").state == "down" and modem2.sent == []
    await again.ping("nas", 60)
    await settle(again)
    assert texts(modem2) == ["✅ nas is back"]


# ── recipients, delete, limits ───────────────────────────────

@pytest.mark.asyncio
async def test_recipients_ping_then_setting_then_default_recipients():
    dogs, modem, _ = make(sms_watchdog_to="+40700000009")
    await dogs.load()
    await dogs.ping("a", 60, "fail", "x", to=["+40711111111", "+40722222222"])
    await dogs.ping("b", 60, "fail", "x")
    await settle(dogs)
    assert sorted(n for n, _ in modem.sent) == ["+40700000009", "+40711111111", "+40722222222"]

    plain, modem2, _ = make()
    await plain.load()
    await plain.ping("c", 60, "fail", "x")
    await settle(plain)
    assert [n for n, _ in modem2.sent] == ["+40700000001"]  # SMS_DEFAULT_RECIPIENTS


@pytest.mark.asyncio
async def test_a_down_sms_that_could_not_go_out_is_retried_after_the_window():
    dogs, modem, clock = make(sms_default_recipients="")
    await dogs.load()
    await dogs.ping("nas", 60, "fail", "x")  # no recipient anywhere → not sent
    await settle(dogs)
    assert modem.sent == [] and dogs.get("nas").down_sms_sent is False
    clock.advance(minutes=29)
    await dogs.ping("nas", 60, "fail", "x", to=["+40711111111"])
    await settle(dogs)
    assert modem.sent == []  # still inside the window of the failed attempt
    clock.advance(minutes=1)
    await dogs.check_once()
    await settle(dogs)
    assert modem.sent == [("+40711111111", "⚠ nas: x")]


@pytest.mark.asyncio
async def test_no_back_sms_when_nobody_heard_of_the_outage():
    dogs, modem, _ = make(sms_default_recipients="")
    await dogs.load()
    await dogs.ping("nas", 60, "fail", "x")
    await settle(dogs)
    rec = await dogs.ping("nas", 60)
    await settle(dogs)
    assert rec.state == "up" and modem.sent == []


@pytest.mark.asyncio
async def test_delete_forgets_without_alerting():
    store = MemoryWatchdogStore()
    dogs, modem, clock = make(store=store)
    await dogs.load()
    await dogs.ping("job", 60)
    assert await dogs.delete("job") is True
    clock.advance(hours=1)
    await dogs.check_once()
    await settle(dogs)
    assert dogs.list() == [] and store.load_all() == [] and modem.sent == []
    assert await dogs.delete("job") is False


@pytest.mark.asyncio
async def test_ping_answers_with_the_cached_modem_state():
    dogs, modem, _ = make()
    await dogs.load()
    assert dogs.modem() == {"registered": True, "signal_strength": -71,
                            "sim_state": "inserted", "operator": "Orange RO"}
    modem.age = 600  # nobody could refresh it: the router is gone
    assert dogs.modem()["registered"] is False and dogs.modem()["operator"] is None
    assert modem.status_queries == 0  # reading it never queries the router


@pytest.mark.asyncio
async def test_the_check_loop_keeps_the_modem_state_fresh_only_while_needed():
    dogs, modem, _ = make()
    await dogs.load()
    modem.age = 300
    await dogs.check_once()
    assert modem.status_queries == 0  # no watchdogs: nothing to answer
    await dogs.ping("a", 60)
    await dogs.check_once()
    assert modem.status_queries == 1
    await dogs.check_once()
    assert modem.status_queries == 1  # fresh now


# ── HTTP ─────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "sms_api_key", "secret")
    monkeypatch.setattr(settings, "sms_allow_no_auth", False)
    dogs, modem, clock = make(sms_watchdog_max=2)
    app = FastAPI()
    app.include_router(watchdogs_router.router)
    app.dependency_overrides[watchdogs_router.get_watchdog_service] = lambda: dogs
    return TestClient(app, headers={"X-API-Key": "secret"}), dogs, modem


def test_ping_contract(api):
    client, _, _ = api
    r = client.post("/api/watchdogs/klopot-sms_main/ping", json={"timeout_sec": 180})
    assert r.status_code == 200
    body = r.json()
    assert body["kurier"] == "ok"
    assert body["modem"] == {"registered": True, "signal_strength": -71,
                             "sim_state": "inserted", "operator": "Orange RO"}
    assert body["watchdog"]["state"] == "up"
    assert datetime.fromisoformat(body["watchdog"]["last_ping"].replace("Z", "+00:00")) == T0


def test_fail_ping_answers_down(api):
    client, _, _ = api
    r = client.post("/api/watchdogs/nas/ping", json={"timeout_sec": 60, "status": "FAIL",
                                                     "detail": "disk  full\n" + "x" * 300})
    assert r.status_code == 200 and r.json()["watchdog"]["state"] == "down"
    listed = client.get("/api/watchdogs").json()
    assert listed[0]["name"] == "nas" and listed[0]["down_reason"] == "fail"
    assert listed[0]["detail"].startswith("disk full x") and len(listed[0]["detail"]) == 160


@pytest.mark.parametrize("name", ["Klopot", "-x", "a b", "x" * 65, ".x"])
def test_bad_names_are_refused(api, name):
    client, _, _ = api
    assert client.post(f"/api/watchdogs/{name}/ping", json={"timeout_sec": 60}).status_code in (404, 422)


def test_name_limits_are_accepted(api):
    client, _, _ = api
    for name in ("a", "0" + "x" * 63, "klopot-sms_main.v2"):
        assert client.post(f"/api/watchdogs/{name}/ping", json={"timeout_sec": 60}).status_code == 200
        client.delete(f"/api/watchdogs/{name}")


@pytest.mark.parametrize("body", [
    {}, {"timeout_sec": 59}, {"timeout_sec": 604801}, {"timeout_sec": 60, "status": "warn"},
    {"timeout_sec": 60, "to": ["not a number"]}, {"timeout_sec": 60, "to": ["+40711111111"] * 11},
])
def test_invalid_pings_are_422(api, body):
    client, dogs, _ = api
    assert client.post("/api/watchdogs/x/ping", json=body).status_code == 422
    assert dogs.list() == []


def test_to_is_normalized_and_stored(api):
    client, dogs, _ = api
    r = client.post("/api/watchdogs/x/ping", json={"timeout_sec": 604800, "to": ["0711 111 111"]})
    assert r.status_code == 200 and dogs.get("x").recipients == ["+40711111111"]


def test_watchdog_limit_is_409(api):
    client, _, _ = api
    for n in ("a", "b"):
        assert client.post(f"/api/watchdogs/{n}/ping", json={"timeout_sec": 60}).status_code == 200
    assert client.post("/api/watchdogs/c/ping", json={"timeout_sec": 60}).status_code == 409
    assert client.post("/api/watchdogs/a/ping", json={"timeout_sec": 60}).status_code == 200


def test_delete(api):
    client, _, _ = api
    client.post("/api/watchdogs/a/ping", json={"timeout_sec": 60})
    assert client.delete("/api/watchdogs/a").status_code == 204
    assert client.delete("/api/watchdogs/a").status_code == 404
    assert client.get("/api/watchdogs").json() == []


def test_auth(api, monkeypatch):
    client, _, _ = api
    assert client.post("/api/watchdogs/a/ping", json={"timeout_sec": 60},
                       headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/watchdogs", headers={"X-API-Key": ""}).status_code == 401
    assert client.delete("/api/watchdogs/a", headers={"X-API-Key": "nope"}).status_code == 401
    monkeypatch.setattr(settings, "sms_api_key", "")
    assert client.post("/api/watchdogs/a/ping", json={"timeout_sec": 60}).status_code == 503
