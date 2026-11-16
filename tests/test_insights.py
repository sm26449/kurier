"""Budget, connections, daily statistics and history search."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import settings
from src.models.schemas import Priority
from src.routers import sms
from src.services.sms_service import SendOptions
from src.services.stats import classify, daily_stats
from tests.test_sms import make_service

NUMBER = "+40712345678"


# ── Budget / connections ──────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_counts_windows_separately():
    svc, _ = make_service(rate_minute=2)
    await svc.send(number=NUMBER, message="a")
    b = svc.limits.budget()
    assert b["normal_minute"] == {"used": 1, "limit": 2, "frees_in_seconds": 0,
                                  "oldest_leaves_in_seconds": b["normal_minute"]["oldest_leaves_in_seconds"]}
    await svc.send(number=NUMBER, message="b")
    full = svc.limits.budget()["normal_minute"]
    assert full["used"] == 2 and 1 <= full["frees_in_seconds"] <= 61
    await svc.send(number=NUMBER, message="c", priority=Priority.critical)
    b = svc.limits.budget()
    assert b["critical_hour"]["used"] == 1 and b["normal_hour"]["used"] == 2
    assert b["dedup_active"] == 2


def test_connections_without_mqtt():
    svc, _ = make_service()
    svc.settings.sms_inbox_webhook_url = ""  # independent of the host's .env
    c = svc.connections()
    assert c["mqtt"]["connected"] is False and c["webhook"]["enabled"] is False
    assert c["history_store"] == "memory"


@pytest.mark.asyncio
async def test_metrics_expose_rate_usage():
    svc, _ = make_service()
    await svc.send(number=NUMBER, message="a")
    m = svc.metrics()
    assert m["rate_normal_minute_used"] == 1 and m["mqtt_connected"] == 0


# ── Daily statistics ──────────────────────────────────────────


@pytest.mark.parametrize("success,detail,expected", [
    (True, None, "sent"),
    (True, "delivered from outbox after 2 attempt(s)", "sent"),
    (False, "Rate limit: 20/hour exceeded", "blocked"),
    (False, "Duplicate SMS to +40… within 300s — skipped", "duplicate"),
    (False, "queued in outbox #3: router down", "queued"),
    (False, "expired in outbox after 4 attempt(s)", "failed"),
    (False, None, "failed"),
])
def test_classify(success, detail, expected):
    assert classify(success, detail) == expected


def row(ts, success=True, detail=None, segments=1, type="t1"):
    return {"ts": ts, "success": success, "detail": detail, "segments": segments,
            "type": type, "source": "klopot", "priority": "normal"}


def test_daily_stats_local_days_types_and_unknown_segments():
    today = date(2026, 11, 14)
    rows = [
        row(datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc), segments=3),
        # 22:30 UTC on the 13th is 00:30 on the 14th in Bucharest (UTC+2)
        row(datetime(2026, 11, 13, 22, 30, tzinfo=timezone.utc), segments=None),
        row(datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc), success=False,
            detail="Rate limit: 20/hour exceeded", type="t2"),
        row(datetime(2026, 11, 13, 10, 5, tzinfo=timezone.utc), success=False,
            detail="Rate limit: 20/hour exceeded", type="t2"),
        row(datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)),  # outside the window
    ]
    s = daily_stats(rows, 3, "Europe/Bucharest", today=today)
    assert [d["date"] for d in s["per_day"]] == ["2026-11-12", "2026-11-13", "2026-11-14"]
    by_day = {d["date"]: d for d in s["per_day"]}
    assert by_day["2026-11-14"]["sent"] == 2 and by_day["2026-11-14"]["segments"] == 3
    assert by_day["2026-11-13"]["blocked"] == 2 and by_day["2026-11-12"]["sent"] == 0
    assert s["unknown_segments"] == 1
    assert s["types"][0]["type"] == "t1" and s["types"][1] == {
        "type": "t2", "sent": 0, "blocked": 2, "duplicate": 0, "failed": 0, "queued": 0, "segments": 0}
    assert s["totals"]["sent"] == 2 and s["totals"]["blocked"] == 2


@pytest.mark.asyncio
async def test_service_stats_from_memory_is_partial():
    svc, _ = make_service()
    await svc.send(number=NUMBER, message="a", options=SendOptions(type="x"))
    s = await svc.history.stats(7)
    assert s["partial"] is True and s["totals"]["sent"] == 1 and len(s["per_day"]) == 7


# ── Search ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_in_memory_filters():
    svc, _ = make_service(rate_minute=2)
    await svc.send(number=NUMBER, message="Container down", options=SendOptions(type="cd"))
    await svc.send(number=NUMBER, message="SOC low", options=SendOptions(type="soc"))
    with pytest.raises(Exception):
        await svc.send(number=NUMBER, message="Container again", options=SendOptions(type="cd"))
    assert [e.message for e in await svc.history.search(q="container")] == ["Container again",
                                                                    "Container down"]
    assert [e.message for e in await svc.history.search(status="blocked")] == ["Container again"]
    assert [e.message for e in await svc.history.search(type="soc")] == ["SOC low"]
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert await svc.history.search(since=future) == []


# ── HTTP ──────────────────────────────────────────────────────


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "sms_api_key", "")
    monkeypatch.setattr(settings, "sms_allow_no_auth", True)
    svc, _ = make_service()
    app = FastAPI()
    app.include_router(sms.router)
    app.dependency_overrides[sms.get_sms_service] = lambda: svc
    return TestClient(app), svc


def test_endpoints(api):
    client, svc = api
    client.post("/api/sms/send", json={"message": "hello", "phone": NUMBER, "type": "t"})
    b = client.get("/api/sms/budget").json()
    assert b["normal_hour"]["used"] == 1 and "connections" in b
    s = client.get("/api/sms/stats?days=7").json()
    assert s["totals"]["sent"] == 1 and len(s["per_day"]) == 7
    hits = client.get("/api/sms/history/search", params={"q": "hel", "status": "sent"}).json()
    assert [h["message"] for h in hits] == ["hello"]
    assert client.get("/api/sms/history/search?status=nope").status_code == 422
    assert client.get("/api/sms/stats?days=0").status_code == 422
