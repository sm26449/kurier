"""Persistent stores — one module per table family, each with a MySQL
store and an in-memory store of the same interface:

- ``db`` — ``MySQLConnection``, UTC helpers, column probes
- ``history`` — ``SendHistoryStore`` (``sms_log``)
- ``outbox`` — ``OutboxItem``, ``MySQLOutboxStore`` / ``MemoryOutboxStore`` (``sms_outbox``)
- ``idempotency`` — ``MySQLIdempotencyStore`` / ``MemoryIdempotencyStore`` (``sms_idempotency``)
- ``inbox`` — ``InboxRecord``, ``MySQLInboxStore`` / ``MemoryInboxStore`` (``sms_inbox``)

Columns added by later migrations are probed at startup, so the code runs
before and after a migration is applied. Everything is re-exported here:
``from src.storage import X`` keeps working.
"""
from src.storage.db import MySQLConnection
from src.storage.history import SendHistoryStore
from src.storage.idempotency import MemoryIdempotencyStore, MySQLIdempotencyStore
from src.storage.inbox import InboxRecord, MemoryInboxStore, MySQLInboxStore
from src.storage.outbox import MemoryOutboxStore, MySQLOutboxStore, OutboxItem

__all__ = [
    "InboxRecord",
    "MemoryIdempotencyStore",
    "MemoryInboxStore",
    "MemoryOutboxStore",
    "MySQLConnection",
    "MySQLIdempotencyStore",
    "MySQLInboxStore",
    "MySQLOutboxStore",
    "OutboxItem",
    "SendHistoryStore",
]
