"""How the process is put together: which store backs each service, and
how the services reach each other.

``build_services()`` is the one place that decides this. Each store is
MySQL when ``MYSQL_PASSWORD`` is set (and history is enabled) and its
table exists; otherwise it falls back to memory with a warning naming
the migration to apply. ``main.lifespan`` only calls it, starts the
background tasks and stops them on shutdown.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import Settings
from src.services.housekeeping import Housekeeping
from src.services.idempotency import IdempotencyCache
from src.services.inbox import InboxService
from src.services.mqtt import MQTTService
from src.services.mqtt_status import MQTTStatus
from src.services.sms_service import SMSService
from src.services.teltonika import TeltonikaClient
from src.services.watchdogs import WatchdogService
from src.storage import (
    MemoryIdempotencyStore,
    MemoryInboxStore,
    MemoryOutboxStore,
    MemoryWatchdogStore,
    MySQLConnection,
    MySQLIdempotencyStore,
    MySQLInboxStore,
    MySQLOutboxStore,
    MySQLWatchdogStore,
    SendHistoryStore,
)

logger = logging.getLogger(__name__)


@dataclass
class Stores:
    """The persistence chosen for this run. ``db`` and ``history`` are
    None without MySQL; the others are then in-memory stores."""

    db: MySQLConnection | None
    history: SendHistoryStore | None
    outbox: MySQLOutboxStore | MemoryOutboxStore
    idempotency: MySQLIdempotencyStore | MemoryIdempotencyStore
    inbox: MySQLInboxStore | MemoryInboxStore
    watchdogs: MySQLWatchdogStore | MemoryWatchdogStore


@dataclass
class Services:
    """Every long-lived object of the process, wired together."""

    teltonika: TeltonikaClient
    stores: Stores
    sms: SMSService
    mqtt: MQTTService
    inbox: InboxService
    housekeeping: Housekeeping
    watchdogs: WatchdogService

    async def start(self) -> None:
        """Start the background tasks (outbox worker, MQTT, inbox poll, pruning, watchdog checks)."""
        if self.sms.outbox is not None:
            await self.sms.outbox.start()
        await self.mqtt.start()
        await self.inbox.start()
        await self.housekeeping.start()
        await self.watchdogs.start()

    async def stop(self) -> None:
        """Stop in reverse order, then close the router client and MySQL."""
        await self.watchdogs.stop()
        await self.housekeeping.stop()
        await self.inbox.stop()
        await self.mqtt.stop()
        if self.sms.outbox is not None:
            await self.sms.outbox.stop()
        await self.teltonika.close()
        if self.stores.history is not None:
            self.stores.history.close()
        if self.stores.db is not None:
            self.stores.db.close()


def _mysql_or_memory(mysql_store, memory_store, warning: str):
    """``mysql_store`` if its table answers, else ``memory_store`` (logged)."""
    try:
        mysql_store.check_table()
        return mysql_store
    except Exception as e:
        logger.warning(warning, e)
        return memory_store


def open_stores(settings: Settings) -> Stores:
    """Pick MySQL or memory for each store. Never raises for a missing table."""
    if not (settings.sms_history_enabled and settings.mysql_password):
        logger.info("MySQL persistence disabled (no MYSQL_PASSWORD set)")
        return Stores(db=None, history=None, outbox=MemoryOutboxStore(),
                      idempotency=MemoryIdempotencyStore(), inbox=MemoryInboxStore(),
                      watchdogs=MemoryWatchdogStore())
    db = MySQLConnection(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
    )
    return Stores(
        db=db,
        history=SendHistoryStore(db),
        outbox=_mysql_or_memory(
            MySQLOutboxStore(db), MemoryOutboxStore(),
            "outbox: MySQL table sms_outbox unavailable (%s) — using in-memory "
            "outbox; apply migrations/002_sms_outbox.sql and restart"),
        idempotency=_mysql_or_memory(
            MySQLIdempotencyStore(db), MemoryIdempotencyStore(),
            "idempotency: MySQL table sms_idempotency unavailable (%s) — keys "
            "kept in memory; apply migrations/003_extended_contract.sql"),
        inbox=_mysql_or_memory(
            MySQLInboxStore(db), MemoryInboxStore(),
            "inbox: MySQL table sms_inbox unavailable (%s) — in-memory archive, "
            "modem messages deleted only once delivered; apply "
            "migrations/004_sms_inbox.sql"),
        watchdogs=_mysql_or_memory(
            MySQLWatchdogStore(db), MemoryWatchdogStore(),
            "watchdogs: MySQL table sms_watchdogs unavailable (%s) — kept in memory, "
            "re-registered by their next ping; apply migrations/005_sms_watchdogs.sql"),
    )


async def build_services(settings: Settings, stores: Stores | None = None) -> Services:
    """Create and wire every service. Nothing is started yet (see ``Services.start``).

    ``stores`` overrides the persistence choice (tests pass in-memory stores).
    """
    teltonika = TeltonikaClient(settings)
    try:
        await teltonika.detect_api()
    except ConnectionError as e:
        logger.warning("Teltonika not reachable at startup: %s — will retry on requests", e)

    if stores is None:
        stores = open_stores(settings)
    idempotency = IdempotencyCache(stores.idempotency, settings.sms_idempotency_ttl_seconds)
    await idempotency.load()
    inbox = InboxService(teltonika, stores.inbox, settings)
    # MQTT needs the send service; the send service only reads MQTT's
    # connection state, so they share an MQTTStatus instead of a cycle.
    sms_service = SMSService(teltonika, settings, history_store=stores.history,
                             outbox_store=stores.outbox, idempotency=idempotency,
                             inbox=inbox, mqtt_status=MQTTStatus())
    mqtt_service = MQTTService(sms_service, settings)
    inbox.add_publisher(mqtt_service.publish_inbox)  # inbound SMS go out over MQTT
    housekeeping = Housekeeping(settings, stores.history,
                                stores.outbox if sms_service.outbox is not None else None,
                                idempotency, stores.inbox)
    watchdogs = WatchdogService(sms_service, teltonika, stores.watchdogs, settings)
    await watchdogs.load()
    return Services(teltonika=teltonika, stores=stores, sms=sms_service, mqtt=mqtt_service,
                    inbox=inbox, housekeeping=housekeeping, watchdogs=watchdogs)
