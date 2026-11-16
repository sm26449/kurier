import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone

import aiomqtt

from src.config import Settings
from pydantic import ValidationError

from src.models.schemas import SendSMSRequest
from src.services.sms_service import (
    DuplicateMessage,
    RateLimitExceeded,
    SMSService,
    describe_error,
    options_from_request,
)
from src.services.inbox import inbox_payload
from src.services.mqtt_status import MQTTStatus
from src.storage import InboxRecord, OutboxItem

logger = logging.getLogger(__name__)

TOPIC_SEND = "sms-gateway/send"
TOPIC_SENT = "sms-gateway/sent"
TOPIC_INBOX = "sms-gateway/inbox"
TOPIC_STATUS = "sms-gateway/status"            # retained
TOPIC_METRICS = "sms-gateway/metrics"          # retained, flat numeric JSON (Telegraf-friendly)
TOPIC_AVAILABILITY = "sms-gateway/availability"  # retained "online" / "offline" (LWT)

STATUS_POLL_SECONDS = 300
METRICS_PUBLISH_SECONDS = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MQTTService:
    def __init__(self, sms_service: SMSService, settings: Settings):
        self.sms = sms_service
        self.settings = settings
        self.status: MQTTStatus = sms_service.mqtt_status
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task | None = None
        if sms_service.outbox is not None:
            sms_service.outbox.add_listener(self._on_outbox_done)

    @property
    def enabled(self) -> bool:
        """MQTT is optional: an empty MQTT_BROKER turns it off."""
        return bool(self.settings.mqtt_broker.strip())

    async def start(self):
        """Start MQTT client with reconnect loop (no-op when not configured)."""
        if not self.enabled:
            logger.info("MQTT off (MQTT_BROKER is empty)")
            return
        self._task = asyncio.create_task(self._connection_loop())

    @property
    def connected(self) -> bool:
        return self._client is not None

    def _set_client(self, client: aiomqtt.Client | None) -> None:
        self._client = client
        self.status.connected = client is not None

    async def stop(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _publish(self, topic: str, payload: dict | str, *, retain: bool = False) -> None:
        if self._client is None:
            return
        data = payload if isinstance(payload, str) else json.dumps(payload)
        await self._client.publish(topic, data, qos=1, retain=retain)

    def _client_kwargs(self) -> dict:
        kwargs: dict = {
            "hostname": self.settings.mqtt_broker,
            "port": self.settings.mqtt_port,
            "will": aiomqtt.Will(TOPIC_AVAILABILITY, "offline", qos=1, retain=True),
        }
        if self.settings.mqtt_user:
            kwargs["username"] = self.settings.mqtt_user
        if self.settings.mqtt_pass:
            kwargs["password"] = self.settings.mqtt_pass
        return kwargs

    async def _connection_loop(self):
        """Maintain MQTT connection with automatic reconnect."""
        failures = 0  # consecutive; only the first is logged above DEBUG
        while True:
            try:
                async with aiomqtt.Client(**self._client_kwargs()) as client:
                    self._set_client(client)
                    failures = 0
                    logger.info("MQTT connected to %s:%d", self.settings.mqtt_broker, self.settings.mqtt_port)
                    await self._publish(TOPIC_AVAILABILITY, "online", retain=True)
                    await client.subscribe(TOPIC_SEND, qos=1)
                    logger.info("MQTT subscribed to %s", TOPIC_SEND)

                    background = [
                        asyncio.create_task(self._status_poll_loop()),
                        asyncio.create_task(self._metrics_loop()),
                    ]
                    try:
                        async for message in client.messages:
                            await self._handle_message(message)
                    finally:
                        for task in background:
                            task.cancel()
                        # Graceful shutdown: say goodbye (the LWT covers crashes).
                        with contextlib.suppress(Exception):
                            await self._publish(TOPIC_AVAILABILITY, "offline", retain=True)
                        self._set_client(None)

            except aiomqtt.MqttError as e:
                # A broker that stays down is retried every 5 s; say so once,
                # not 17 000 times a day.
                failures += 1
                logger.log(logging.WARNING if failures == 1 else logging.DEBUG,
                           "MQTT connection to %s:%d lost: %s — retrying every 5s until it is back",
                           self.settings.mqtt_broker, self.settings.mqtt_port, describe_error(e))
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                logger.info("MQTT service stopped")
                raise
            except Exception as e:
                logger.error("MQTT unexpected error: %s — reconnecting in 10s", describe_error(e))
                await asyncio.sleep(10)
            finally:
                self._set_client(None)

    # ── sms-gateway/send ──────────────────────────────────────

    async def _handle_message(self, message: aiomqtt.Message):
        """Handle incoming MQTT message on sms-gateway/send."""
        try:
            payload = json.loads(message.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("MQTT bad payload: %s", e)
            return

        # Same contract as POST /api/sms/send (docs/API.md).
        try:
            req = SendSMSRequest.model_validate(payload)
        except ValidationError as e:
            logger.warning("MQTT send request rejected: %s", e.errors(include_url=False)[:3])
            return
        msg_text, number = req.message, req.number

        try:
            results = await self.sms.send(message=msg_text, number=number, priority=req.priority,
                                          options=options_from_request(req))
            for result in results:
                await self._publish(TOPIC_SENT, {
                    "number": result.number,
                    "message": result.message,
                    "timestamp": _now(),
                    "success": result.success,
                    "queued": result.queued,
                    "segments": result.segments,
                    "replayed": result.replayed,
                })
        except (RateLimitExceeded, DuplicateMessage) as e:
            await self._publish(TOPIC_SENT, {
                "number": number, "message": msg_text, "timestamp": _now(),
                "success": False, "detail": str(e),
            })
        except Exception as e:
            logger.error("MQTT send failed: %s", describe_error(e))
            await self._publish(TOPIC_SENT, {
                "number": number, "message": msg_text, "timestamp": _now(),
                "success": False, "detail": describe_error(e),
            })

    async def _on_outbox_done(self, item: OutboxItem):
        """Publish the final outcome of a queued SMS (delivered or expired)."""
        await self._publish(TOPIC_SENT, {
            "number": item.number,
            "message": item.message,
            "timestamp": _now(),
            "success": item.status == "sent",
            "from_outbox": True,
            "outbox_id": item.id,
            "status": item.status,
            "detail": item.last_error if item.status != "sent" else None,
        })

    # ── Periodic publishers ───────────────────────────────────

    async def publish_inbox(self, rec: InboxRecord) -> bool:
        """Inbox publisher for ``InboxService``: True once the broker has it."""
        if self._client is None:
            return False
        try:
            await self._publish(TOPIC_INBOX, inbox_payload(rec, self.settings.teltonika_modem))
            return True
        except aiomqtt.MqttError as e:
            logger.warning("MQTT inbox publish failed: %s", describe_error(e))
            return False

    async def _status_poll_loop(self):
        """Publish modem status (retained) on connect and periodically."""
        while True:
            try:
                status = await self.sms.get_status()
                await self._publish(TOPIC_STATUS, {
                    "signal": status.get("signal_strength"),
                    "rsrp": status.get("rsrp"),
                    "sinr": status.get("sinr"),
                    "rsrq": status.get("rsrq"),
                    "operator": status.get("operator"),
                    "sim_state": status.get("sim_state"),
                    "connection_type": status.get("connection_type"),
                    "band": status.get("band"),
                    "modem_temperature": status.get("modem_temperature"),
                    "timestamp": _now(),
                }, retain=True)
            except Exception as e:
                logger.warning("Status poll failed: %s", describe_error(e))
            await asyncio.sleep(STATUS_POLL_SECONDS)

    async def _metrics_loop(self):
        """Publish counters (retained, flat numeric JSON) for Telegraf/Grafana."""
        while True:
            try:
                await self._publish(TOPIC_METRICS, self.sms.metrics(), retain=True)
            except Exception as e:
                logger.warning("Metrics publish failed: %s", describe_error(e))
            await asyncio.sleep(METRICS_PUBLISH_SECONDS)
