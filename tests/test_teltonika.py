"""TeltonikaClient tests against a fake router (httpx.MockTransport)."""

import json

import httpx
import pytest

from src.config import Settings
from src.services.teltonika import TeltonikaClient, TeltonikaError

SESSION = "a" * 32


class FakeRouter:
    def __init__(self):
        self.calls: list[str] = []
        self.send_response: dict = {"success": True, "data": {"sms_used": 1}}
        self.ubus_exec_codes: list[int] = []  # result codes for successive file.exec calls
        self.gsmctl_stdout = ""
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("All connection attempts failed")
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/api/login":
            self.calls.append("rest_login")
            return httpx.Response(200, json={"data": {"token": "tok", "expires": 299}})
        if path == "/api/messages/actions/send":
            self.calls.append("send")
            return httpx.Response(200, json=self.send_response)
        if path == "/ubus":
            obj = body["params"][1]
            if obj == "session":
                self.calls.append("ubus_login")
                return httpx.Response(200, json={"result": [0, {"ubus_rpc_session": SESSION}]})
            self.calls.append("gsmctl:" + " ".join(body["params"][3]["params"]))
            code = self.ubus_exec_codes.pop(0) if self.ubus_exec_codes else 0
            if code:
                return httpx.Response(200, json={"result": [code]})
            return httpx.Response(200, json={"result": [0, {"stdout": self.gsmctl_stdout}]})
        return httpx.Response(200, text="ok")


@pytest.fixture
def router_client():
    router = FakeRouter()
    client = TeltonikaClient(Settings(teltonika_host="router", teltonika_pass="x"))
    client._client = httpx.AsyncClient(
        base_url="http://router", transport=httpx.MockTransport(router.handler)
    )
    return router, client


@pytest.mark.asyncio
async def test_detects_hybrid(router_client):
    router, client = router_client
    assert await client.detect_api() == "hybrid"


@pytest.mark.asyncio
async def test_send_redetects_when_down_at_startup(router_client):
    router, client = router_client
    router.down = True
    with pytest.raises(ConnectionError):
        await client.detect_api()
    assert client.api_mode is None

    router.down = False
    await client.send_sms("+40712345678", "hi")
    assert client.api_mode == "hybrid"
    assert "send" in router.calls  # REST path, not the gsmctl fallback


@pytest.mark.asyncio
async def test_send_rejected_by_router_raises(router_client):
    router, client = router_client
    router.send_response = {"success": False, "errors": [{"code": 100, "error": "modem busy"}]}
    await client.detect_api()
    with pytest.raises(TeltonikaError, match="modem busy"):
        await client.send_sms("+40712345678", "hi")


@pytest.mark.asyncio
async def test_gsmctl_session_retry_is_bounded(router_client):
    router, client = router_client
    await client.detect_api()
    router.ubus_exec_codes = [6, 6, 6, 6]
    with pytest.raises(TeltonikaError):
        await client.get_inbox()
    assert sum(c.startswith("gsmctl") for c in router.calls) == 2


@pytest.mark.asyncio
async def test_gsmctl_session_retry_recovers(router_client):
    router, client = router_client
    await client.detect_api()
    router.ubus_exec_codes = [6]
    router.gsmctl_stdout = "Index: 3\nDate: 2026-11-14\nSender: +40712345678\nText: hello\nStatus: read\n"
    msgs = await client.get_inbox()
    assert msgs == [{"id": "3", "date": "2026-11-14", "sender": "+40712345678",
                     "message": "hello", "read": True}]


@pytest.mark.asyncio
async def test_status_is_cached(router_client):
    router, client = router_client
    await client.detect_api()
    await client.get_status()
    n = len(router.calls)
    await client.get_status()
    assert len(router.calls) == n


def test_parse_signal():
    parsed = TeltonikaClient._parse_signal("RSSI: -65\nRSRP: -95\nSINR: 12\nRSRQ: -10\n")
    assert parsed == {"rssi": -65, "rsrp": -95, "sinr": 12, "rsrq": -10}


class FlakyModem(FakeRouter):
    """Answers selected gsmctl commands from a script; the rest normally."""

    def __init__(self, script):
        super().__init__()
        self.script = script  # {"-q": [outputs...]}
        self.gsmctl_stdout = "value"

    def handler(self, request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/ubus" and body["params"][1] == "file":
            args = body["params"][3]["params"]
            queue = self.script.get(args[0])
            if queue:
                self.calls.append("gsmctl:" + " ".join(args))
                return httpx.Response(200, json={"result": [0, {"stdout": queue.pop(0)}]})
        return super().handler(request)


def flaky_client(script):
    router = FlakyModem(script)
    client = TeltonikaClient(Settings(teltonika_host="router", teltonika_pass="x"))
    client._client = httpx.AsyncClient(base_url="http://router",
                                       transport=httpx.MockTransport(router.handler))
    return router, client


SIGNAL = "RSSI: -66\nRSRP: -93\nSINR: 13\nRSRQ: -12\n"
ERR = "ERROR: Couldn't retrieve data\n"


@pytest.mark.asyncio
async def test_status_retries_a_modem_error_once():
    router, client = flaky_client({"-q": [ERR, SIGNAL]})
    await client.detect_api()
    st = await client._query_status()
    assert st["signal_strength"] == -66 and st["rsrp"] == -93


@pytest.mark.asyncio
async def test_status_keeps_last_good_signal_and_never_shows_error_text():
    router, client = flaky_client({"-q": [SIGNAL, ERR, ERR], "-o": ["orange", ERR, ERR]})
    await client.detect_api()
    first = await client._query_status()
    second = await client._query_status()
    assert first["signal_strength"] == second["signal_strength"] == -66
    assert second["operator"] == "orange"   # last good, not "ERROR: …"


@pytest.mark.asyncio
async def test_last_good_expires():
    router, client = flaky_client({"-q": [SIGNAL, ERR, ERR]})
    await client.detect_api()
    await client._query_status()
    ts, v = client._last_good["signal_strength"]
    client._last_good["signal_strength"] = (ts - 601, v)
    assert (await client._query_status())["signal_strength"] is None


MODEM_INFO = json.dumps({
    "model": "SLM750-VE", "state": "Idle",
    "cache": {"rssi_value": -66, "rsrp_value": -93, "rsrq_value": -7, "sinr_value": 12,
              "operator": "orange", "net_mode_str": "LTE", "band_str": "LTE B3",
              "temperature": 410, "pin_state_str": "OK"},
})


@pytest.mark.asyncio
async def test_status_from_modem_cache_json():
    router, client = flaky_client({"-E": [MODEM_INFO], "-z": ["Inserted"], "-i": ["864086000000000"]})
    await client.detect_api()
    st = await client._query_status()
    assert st == {"signal_strength": -66, "rsrp": -93, "sinr": 12, "rsrq": -7,
                  "operator": "orange", "connection_type": "LTE", "band": "LTE B3",
                  "modem_temperature": 41.0, "modem_model": "SLM750-VE",
                  "sim_state": "Inserted", "imei": "864086000000000"}
    assert not any(c in router.calls for c in ("gsmctl:-q", "gsmctl:-o", "gsmctl:-t"))


@pytest.mark.asyncio
async def test_status_falls_back_when_modem_cache_unusable():
    router, client = flaky_client({"-E": [ERR, "not json"], "-q": [SIGNAL], "-o": ["orange"]})
    await client.detect_api()
    st = await client._query_status()
    assert st["signal_strength"] == -66 and st["operator"] == "orange"


@pytest.mark.asyncio
async def test_imei_is_read_once():
    router, client = flaky_client({"-E": [MODEM_INFO, MODEM_INFO], "-i": ["864086000000000"]})
    await client.detect_api()
    await client._query_status()
    await client._query_status()
    assert router.calls.count("gsmctl:-i") == 1
