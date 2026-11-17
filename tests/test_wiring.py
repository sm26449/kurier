"""build_services(): which store backs each service, and how services reach each other."""
import logging

import pymysql
import pytest

from src.config import Settings
from src.services.teltonika import TeltonikaClient
from src.storage import (
    MemoryIdempotencyStore,
    MemoryInboxStore,
    MemoryOutboxStore,
    MemoryWatchdogStore,
)
from src.wiring import _mysql_or_memory, build_services, open_stores


def make_settings(**kw) -> Settings:
    return Settings(teltonika_host="192.0.2.1", teltonika_pass="test", mqtt_broker="", **kw)


@pytest.fixture
def router_down(monkeypatch):
    async def down(self):
        raise ConnectionError("unreachable")
    monkeypatch.setattr(TeltonikaClient, "detect_api", down)


def test_without_mysql_every_store_is_in_memory():
    stores = open_stores(make_settings(mysql_password=""))
    assert stores.db is None and stores.history is None
    assert isinstance(stores.outbox, MemoryOutboxStore)
    assert isinstance(stores.idempotency, MemoryIdempotencyStore)
    assert isinstance(stores.inbox, MemoryInboxStore)
    assert isinstance(stores.watchdogs, MemoryWatchdogStore)


def test_history_disabled_skips_mysql_even_with_a_password():
    stores = open_stores(make_settings(mysql_password="x", sms_history_enabled=False))
    assert stores.db is None


def test_missing_table_falls_back_to_memory_and_names_the_migration(caplog):
    class NoTable:
        def check_table(self):
            raise pymysql.err.ProgrammingError(1146, "Table 'sms_outbox' doesn't exist")

    memory = MemoryOutboxStore()
    with caplog.at_level(logging.WARNING):
        chosen = _mysql_or_memory(NoTable(), memory, "outbox unavailable (%s) — apply 002")
    assert chosen is memory
    assert "apply 002" in caplog.text and "doesn't exist" in caplog.text


def test_present_table_keeps_the_mysql_store():
    class Ok:
        def check_table(self):
            pass

    mysql = Ok()
    assert _mysql_or_memory(mysql, MemoryOutboxStore(), "%s") is mysql


@pytest.mark.asyncio
async def test_services_are_cross_linked(router_down):
    settings = make_settings(mysql_password="", sms_outbox_enabled=True)
    services = await build_services(settings)
    try:
        assert services.sms.inbox is services.inbox
        assert services.mqtt.status is services.sms.mqtt_status
        assert services.mqtt.publish_inbox in services.inbox._publishers
        assert services.sms.outbox is not None
        assert services.housekeeping is not None
        assert services.watchdogs.sms is services.sms
        assert services.watchdogs.client is services.teltonika
    finally:
        await services.teltonika.close()


@pytest.mark.asyncio
async def test_router_down_at_startup_is_not_fatal(router_down, caplog):
    with caplog.at_level(logging.WARNING):
        services = await build_services(make_settings(mysql_password=""))
    try:
        assert "Teltonika not reachable at startup" in caplog.text
    finally:
        await services.teltonika.close()
