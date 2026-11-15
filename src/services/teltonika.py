import asyncio
import json
import logging
import time

import httpx

from src.config import Settings

logger = logging.getLogger(__name__)

STATUS_CACHE_SECONDS = 30


class TeltonikaError(ConnectionError):
    """The router answered, but refused or failed the operation."""


def router_base_url(host: str, scheme: str = "http") -> str:
    """``TELTONIKA_HOST`` (+ ``TELTONIKA_SCHEME``) → base URL.

    The host may carry a port (``192.168.1.1:8443``) or be a full URL
    (``https://router.lan``), which then wins over the scheme setting.
    """
    host = host.strip().rstrip("/")
    if "://" in host:
        return host
    return f"{'https' if scheme.strip().lower() == 'https' else 'http'}://{host}"


class TeltonikaClient:
    """Client for Teltonika RUT241 SMS API.

    Uses a hybrid approach discovered through testing:
    - REST API for login + send SMS (POST /api/messages/actions/send)
    - ubus file exec + gsmctl for inbox, delete, status (REST returns 403 on reads)
    """

    def __init__(self, settings: Settings):
        self.host = settings.teltonika_host
        self.user = settings.teltonika_user
        self.password = settings.teltonika_pass
        self.modem = settings.teltonika_modem
        self.base_url = router_base_url(settings.teltonika_host, settings.teltonika_scheme)

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            # Read timeout stays below Klopot's 15s request timeout so a hung
            # router yields a queued 202 instead of a client-side timeout.
            timeout=httpx.Timeout(10.0, connect=5.0),
            verify=settings.teltonika_verify_tls,
        )

        # Auth state
        self._token: str | None = None
        self._token_expires: float = 0
        self._ubus_session: str | None = None
        self._ubus_session_expires: float = 0

        self.api_mode: str | None = None

        # get_status() costs 6 gsmctl round-trips; health checks and the
        # MQTT status loop share a short-lived cached copy.
        self._status_cache: tuple[float, dict] | None = None

    async def close(self):
        await self._client.aclose()

    # ── API Detection ──────────────────────────────────────────

    async def detect_api(self) -> str:
        """Login via REST and ubus. Both are needed (REST for send, ubus for read)."""
        # Both logins in parallel: with the router down this costs one
        # connect timeout (5s) instead of two.
        token, session = await asyncio.gather(
            self._rest_login(), self._ubus_login(), return_exceptions=True
        )
        for name, res in (("REST", token), ("ubus", session)):
            if isinstance(res, Exception):
                logger.debug("%s login failed: %s", name, res)
        rest_ok = bool(token) and not isinstance(token, Exception)
        ubus_ok = bool(session) and not isinstance(session, Exception)

        if rest_ok and ubus_ok:
            self.api_mode = "hybrid"
            logger.info("Teltonika API mode: hybrid (REST send + ubus gsmctl)")
        elif rest_ok:
            self.api_mode = "rest"
            logger.info("Teltonika API mode: REST only (inbox/status may be limited)")
        elif ubus_ok:
            self.api_mode = "ubus"
            logger.info("Teltonika API mode: ubus only")
        else:
            raise ConnectionError(
                f"Cannot connect to Teltonika at {self.host} — neither REST nor ubus responded"
            )
        return self.api_mode

    async def _ensure_api(self) -> None:
        """Detect the API mode lazily if the router was down at startup."""
        if self.api_mode is None:
            await self.detect_api()

    # ── REST API (login + send) ────────────────────────────────

    async def _rest_login(self) -> str | None:
        resp = await self._client.post(
            "/api/login",
            json={"username": self.user, "password": self.password},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("data", {}).get("token") or data.get("token") or data.get("access_token")
        if token:
            self._token = token
            expires = data.get("data", {}).get("expires", 299)
            self._token_expires = time.monotonic() + expires - 30
        return token

    async def _rest_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._token or time.monotonic() > self._token_expires:
            await self._rest_login()

        headers = {"Authorization": f"Bearer {self._token}"}
        resp = await self._client.request(method, path, headers=headers, **kwargs)

        if resp.status_code in (401, 403):
            await self._rest_login()
            headers = {"Authorization": f"Bearer {self._token}"}
            resp = await self._client.request(method, path, headers=headers, **kwargs)

        resp.raise_for_status()
        return resp

    async def _rest_send_sms(self, number: str, message: str) -> dict:
        resp = await self._rest_request(
            "POST",
            "/api/messages/actions/send",
            json={"data": {"number": number, "message": message, "modem": self.modem}},
        )
        data = resp.json()
        # RutOS answers HTTP 200 with {"success": false, "errors": [...]}
        # when it refuses the send — that must not count as delivered.
        if isinstance(data, dict) and data.get("success") is False:
            raise TeltonikaError(f"Router rejected SMS: {data.get('errors') or data}")
        return data

    # ── ubus API (login + gsmctl exec) ─────────────────────────

    async def _ubus_login(self) -> str | None:
        resp = await self._client.post(
            "/ubus",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "call",
                "params": [
                    "00000000000000000000000000000000",
                    "session",
                    "login",
                    {"username": self.user, "password": self.password},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", [])
        if len(result) >= 2 and isinstance(result[1], dict):
            session = result[1].get("ubus_rpc_session")
            if session and session != "00000000000000000000000000000000":
                self._ubus_session = session
                self._ubus_session_expires = time.monotonic() + 240
                return session
        return None

    async def _gsmctl(self, args: list[str], _retry: bool = True) -> str:
        """Run gsmctl command via ubus file exec. Returns stdout."""
        if not self._ubus_session or time.monotonic() > self._ubus_session_expires:
            if not await self._ubus_login():
                raise TeltonikaError("ubus login failed")

        resp = await self._client.post(
            "/ubus",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "call",
                "params": [
                    self._ubus_session,
                    "file",
                    "exec",
                    {"command": "/usr/sbin/gsmctl", "params": args},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", [])

        # ubus errors come back as a single-element list, e.g. [6].
        if result and result[0] == 6:  # Session expired / access denied
            if not _retry:
                raise TeltonikaError("ubus session rejected after re-login")
            self._ubus_session = None
            return await self._gsmctl(args, _retry=False)
        if len(result) >= 2 and isinstance(result[1], dict):
            return result[1].get("stdout", "")
        return ""

    # ── gsmctl Parsers ─────────────────────────────────────────

    @staticmethod
    def _parse_sms_list(raw: str) -> list[dict]:
        """Parse gsmctl -S -l all output into list of dicts."""
        messages = []
        current: dict = {}

        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("Index:"):
                if current:
                    messages.append(current)
                current = {"id": line.split(":", 1)[1].strip()}
            elif ":" in line:
                key, _, val = line.partition(":")
                key = key.strip().lower().replace(" ", "_")
                val = val.strip()
                if key == "date":
                    current["date"] = val
                elif key == "sender":
                    current["sender"] = val
                elif key == "status":
                    current["read"] = val == "read"
                elif key == "text":
                    current["message"] = val

        if current:
            messages.append(current)
        return messages

    @staticmethod
    def _parse_signal(raw: str) -> dict:
        """Parse gsmctl -q output (RSSI, RSRP, SINR, RSRQ)."""
        result = {}
        for line in raw.strip().split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip().upper()
                val = val.strip()
                try:
                    result[key.lower()] = int(val)
                except ValueError:
                    result[key.lower()] = val
        return result

    @staticmethod
    def _parse_modem_info(raw: str) -> dict:
        """Parse gsmctl -E output (JSON; the modem daemon's cache)."""
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # ── Public Interface ───────────────────────────────────────

    async def send_sms(self, number: str, message: str) -> dict:
        """Send SMS via REST API."""
        await self._ensure_api()
        if self.api_mode in ("rest", "hybrid"):
            return await self._rest_send_sms(number, message)
        # Fallback: gsmctl send
        stdout = await self._gsmctl(["-S", "-s", f"{number} {message}"])
        return {"success": "OK" in stdout or not stdout, "raw": stdout}

    async def get_inbox(self) -> list[dict]:
        """List all SMS via gsmctl."""
        await self._ensure_api()
        raw = await self._gsmctl(["-S", "-l", "all"])
        return self._parse_sms_list(raw)

    async def read_sms(self, msg_id: str) -> dict | None:
        """Read specific SMS by index via gsmctl."""
        await self._ensure_api()
        raw = await self._gsmctl(["-S", "-r", msg_id])
        if not raw.strip() or "error" in raw.lower():
            return None
        messages = self._parse_sms_list(raw)
        return messages[0] if messages else {"id": msg_id, "message": raw.strip()}

    async def delete_sms(self, msg_id: str) -> bool:
        """Delete SMS by index via gsmctl."""
        await self._ensure_api()
        raw = await self._gsmctl(["-S", "-d", msg_id])
        return "error" not in raw.lower()

    async def get_status(self) -> dict:
        """Get modem status via gsmctl (cached for STATUS_CACHE_SECONDS)."""
        if self._status_cache and time.monotonic() - self._status_cache[0] < STATUS_CACHE_SECONDS:
            return dict(self._status_cache[1])
        await self._ensure_api()
        status = await self._query_status()
        self._status_cache = (time.monotonic(), status)
        return dict(status)

    def cached_status(self) -> dict:
        """Last status snapshot (any age) without querying the router."""
        if not self._status_cache:
            return {}
        status = dict(self._status_cache[1])
        status["rssi"] = status.get("signal_strength")
        return status

    # Status comes from `gsmctl -E`: the modem daemon's JSON cache. It answers
    # reliably (8/8 in tests) and carries signal, operator, network, band,
    # temperature and model in ONE call. The per-value commands (-q, -o, -t)
    # intermittently answer "ERROR: Couldn't retrieve data" (-q failed 25–65 %
    # depending on spacing) and remain only as a fallback. No answer is ever
    # stored as a value: one retry, then the last good value while recent.
    # SIM state keeps its own command (-z) — it feeds /api/health and
    # Klopot's sms_modem_down rule — and a short fallback.
    _LAST_GOOD_SECONDS = {"sim_state": 60, "imei": 7 * 86400, "modem_model": 7 * 86400}
    _LAST_GOOD_DEFAULT = 600
    _RETRY_DELAY = 1.0  # seconds between the two tries (tests set 0)
    _STATUS_KEYS = ("signal_strength", "rsrp", "sinr", "rsrq", "operator", "connection_type",
                    "band", "modem_temperature", "modem_model", "sim_state", "imei")

    async def _read(self, args: list[str]) -> str | None:
        for attempt in range(2):
            out = (await self._gsmctl(args)).strip()
            if out and not out.upper().startswith("ERROR"):
                return out
            if attempt == 0:
                await asyncio.sleep(self._RETRY_DELAY)
        logger.debug("gsmctl %s: no usable answer", " ".join(args))
        return None

    @staticmethod
    def _from_modem_info(info: dict) -> dict:
        """Map the `gsmctl -E` JSON (cache.*) onto status keys."""
        cache = info.get("cache") if isinstance(info.get("cache"), dict) else {}
        out: dict = {}
        for key, src in (("signal_strength", "rssi_value"), ("rsrp", "rsrp_value"),
                         ("sinr", "sinr_value"), ("rsrq", "rsrq_value")):
            if isinstance(cache.get(src), int | float):
                out[key] = cache[src]
        for key, src in (("operator", "operator"), ("connection_type", "net_mode_str"),
                         ("band", "band_str")):
            if isinstance(cache.get(src), str) and cache[src]:
                out[key] = cache[src]
        if isinstance(cache.get("temperature"), int | float):
            out["modem_temperature"] = cache["temperature"] / 10  # tenths of °C
        if isinstance(info.get("model"), str) and info["model"]:
            out["modem_model"] = info["model"]
        return out

    async def _query_status(self) -> dict:
        now = time.monotonic()
        last_good = getattr(self, "_last_good", {})
        fresh: dict = {}

        raw_info = await self._read(["-E"])
        if raw_info:
            info = self._parse_modem_info(raw_info)
            if isinstance(info, dict):
                fresh.update(self._from_modem_info(info))
        if "signal_strength" not in fresh:  # -E unusable → per-value commands
            raw_signal = await self._read(["-q"])
            signal = self._parse_signal(raw_signal) if raw_signal else {}
            for key, src in (("signal_strength", "rssi"), ("rsrp", "rsrp"),
                             ("sinr", "sinr"), ("rsrq", "rsrq")):
                if isinstance(signal.get(src), int):
                    fresh[key] = signal[src]
            for key, args in (("operator", ["-o"]), ("connection_type", ["-t"]),
                              ("modem_model", ["-m"])):
                if key not in fresh and (value := await self._read(args)) is not None:
                    fresh[key] = value

        if (sim := await self._read(["-z"])) is not None:
            fresh["sim_state"] = sim
        if "imei" not in last_good and (imei := await self._read(["-i"])) is not None:
            fresh["imei"] = imei  # static: read once

        for key, value in fresh.items():
            last_good[key] = (now, value)
        self._last_good = last_good

        result = {}
        for key in self._STATUS_KEYS:
            hit = last_good.get(key)
            max_age = self._LAST_GOOD_SECONDS.get(key, self._LAST_GOOD_DEFAULT)
            result[key] = hit[1] if hit and now - hit[0] <= max_age else None
        return result

    async def is_reachable(self) -> bool:
        try:
            resp = await self._client.get("/", timeout=5.0)
            return resp.status_code < 500
        except Exception:
            return False
