"""HTTP layer tests (auth, alias, status codes) with a fake service."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import settings
from src.routers import health, sms
from src.services.sms_service import SMSService
from tests.test_sms import FakeTeltonikaClient, make_service


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "sms_api_key", "")
    svc, fake = make_service()
    app = FastAPI()
    app.include_router(sms.router)
    app.include_router(health.router)
    app.dependency_overrides[sms.get_sms_service] = lambda: svc
    app.dependency_overrides[health.get_teltonika_client] = lambda: fake
    return TestClient(app), fake


def test_phone_alias_accepted(api):
    client, fake = api
    r = client.post("/api/sms/send", json={"phone": "0712345678", "message": "Hi"})
    assert r.status_code == 200
    assert fake.sent == [("+40712345678", "Hi")]


def test_invalid_number_is_400(api):
    client, fake = api
    r = client.post("/api/sms/send", json={"number": "xyz", "message": "Hi"})
    assert r.status_code == 400


def test_api_key_enforced(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(settings, "sms_api_key", "secret")
    assert client.post("/api/sms/send", json={"message": "x"}).status_code == 401
    assert client.post("/api/sms/send", json={"message": "x"},
                       headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/api/sms/send", json={"message": "x"},
                       headers={"X-API-Key": "secret"}).status_code == 200


def test_history_limit_bounded(api):
    client, _ = api
    assert client.get("/api/sms/history?limit=1000").status_code == 422


def test_health_reports_unhealthy_with_200(api):
    # Contract: state in the body, always 200 — Klopot's http_poll drops
    # responses >= 400, which would hide modem_reachable=false from rules.
    client, fake = api

    async def unreachable():
        return False

    fake.is_reachable = unreachable
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["modem_reachable"] is False and body["sim_ok"] is False
    assert body["outbox_pending"] == 0 and body["outbox_oldest_age_seconds"] == 0


def test_health_ok(api):
    client, _ = api
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_queued_send_returns_202_and_outbox_listing(monkeypatch):
    monkeypatch.setattr(settings, "sms_api_key", "")
    svc, fake = make_service(outbox=True)

    async def down(number, message):
        raise ConnectionError("router down")

    fake.send_sms = down
    app = FastAPI()
    app.include_router(sms.router)
    app.include_router(health.router)
    app.dependency_overrides[sms.get_sms_service] = lambda: svc
    app.dependency_overrides[health.get_teltonika_client] = lambda: fake
    client = TestClient(app)

    r = client.post("/api/sms/send", json={"phone": "+40712345678", "message": "Hi"})
    assert r.status_code == 202
    body = r.json()[0]
    assert body["queued"] is True and body["outbox_id"] is not None

    listing = client.get("/api/sms/outbox").json()
    assert [i["id"] for i in listing] == [body["outbox_id"]]
    hc = client.get("/api/health").json()
    assert hc["outbox_pending"] == 1 and isinstance(hc["outbox_oldest_age_seconds"], int)

    assert client.delete(f"/api/sms/outbox/{body['outbox_id']}").status_code == 200
    assert client.get("/api/sms/outbox").json() == []
