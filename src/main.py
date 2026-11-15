import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src import __version__
from src.config import settings
from src.routers import health, sms
from src.services.housekeeping import Housekeeping
from src.services.idempotency import IdempotencyCache
from src.services.inbox import InboxService
from src.services.mqtt import MQTTService
from src.services.sms_service import SMSService
from src.services.teltonika import TeltonikaClient
from src.storage import (
    MemoryIdempotencyStore,
    MemoryInboxStore,
    MemoryOutboxStore,
    MySQLConnection,
    MySQLIdempotencyStore,
    MySQLInboxStore,
    MySQLOutboxStore,
    SendHistoryStore,
)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# httpx logs every request at INFO — ~10k lines/day of router polling.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Kurier starting…")
    if not settings.sms_api_key:
        if settings.sms_allow_no_auth:
            logger.warning("SMS_ALLOW_NO_AUTH=true: /api/sms/* is open to anyone who can "
                           "reach this port — they can send SMS on your SIM")
        else:
            logger.error("SMS_API_KEY is not set: /api/sms/* answers 503 until you set it "
                         "(or SMS_ALLOW_NO_AUTH=true) and restart")

    teltonika = TeltonikaClient(settings)
    try:
        await teltonika.detect_api()
    except ConnectionError as e:
        logger.warning("Teltonika not reachable at startup: %s — will retry on requests", e)

    db: MySQLConnection | None = None
    history_store: SendHistoryStore | None = None
    outbox_store: MySQLOutboxStore | MemoryOutboxStore = MemoryOutboxStore()
    idem_store: MySQLIdempotencyStore | MemoryIdempotencyStore = MemoryIdempotencyStore()
    inbox_store: MySQLInboxStore | MemoryInboxStore = MemoryInboxStore()
    if settings.sms_history_enabled and settings.mysql_password:
        db = MySQLConnection(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
        )
        history_store = SendHistoryStore(db)
        try:
            mysql_outbox = MySQLOutboxStore(db)
            mysql_outbox.check_table()
            outbox_store = mysql_outbox
        except Exception as e:
            logger.warning("outbox: MySQL table sms_outbox unavailable (%s) — using in-memory "
                           "outbox; apply migrations/002_sms_outbox.sql and restart", e)
        try:
            mysql_idem = MySQLIdempotencyStore(db)
            mysql_idem.check_table()
            idem_store = mysql_idem
        except Exception as e:
            logger.warning("idempotency: MySQL table sms_idempotency unavailable (%s) — keys "
                           "kept in memory; apply migrations/003_extended_contract.sql", e)
        try:
            mysql_inbox = MySQLInboxStore(db)
            mysql_inbox.check_table()
            inbox_store = mysql_inbox
        except Exception as e:
            logger.warning("inbox: MySQL table sms_inbox unavailable (%s) — in-memory archive, "
                           "modem messages deleted only once delivered; apply "
                           "migrations/004_sms_inbox.sql", e)
    else:
        logger.info("MySQL persistence disabled (no MYSQL_PASSWORD set)")
    idempotency = IdempotencyCache(idem_store, settings.sms_idempotency_ttl_seconds)
    await idempotency.load()
    sms_service = SMSService(teltonika, settings, history_store=history_store,
                             outbox_store=outbox_store, idempotency=idempotency)
    mqtt_service = MQTTService(sms_service, settings)
    inbox = InboxService(teltonika, inbox_store, settings)
    inbox.add_publisher(mqtt_service.publish_inbox)
    sms_service.inbox = inbox
    sms_service.mqtt = mqtt_service
    housekeeping = Housekeeping(settings, history_store,
                                outbox_store if sms_service.outbox is not None else None,
                                idempotency, inbox_store)

    # Wire up dependency overrides
    app.dependency_overrides[sms.get_sms_service] = lambda: sms_service
    app.dependency_overrides[health.get_teltonika_client] = lambda: teltonika

    # Store on app state for access if needed
    app.state.teltonika = teltonika
    app.state.sms_service = sms_service
    app.state.mqtt_service = mqtt_service
    app.state.history_store = history_store

    if sms_service.outbox is not None:
        await sms_service.outbox.start()
    await mqtt_service.start()
    await inbox.start()
    await housekeeping.start()
    logger.info("Kurier ready — API mode: %s", teltonika.api_mode or "pending")

    yield

    # Shutdown
    logger.info("Kurier shutting down…")
    await housekeeping.stop()
    await inbox.stop()
    await mqtt_service.stop()
    if sms_service.outbox is not None:
        await sms_service.outbox.stop()
    await teltonika.close()
    if history_store is not None:
        history_store.close()
    if db is not None:
        db.close()


app = FastAPI(
    title="Kurier",
    description="SMS gateway for Teltonika RutOS routers: REST + MQTT, durable outbox, inbound transport.",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(sms.router)
app.include_router(health.router)


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "kurier", "version": __version__}


# ── Status page (read-only). Static files only; the data comes from the
#    API with the operator's key. Strict CSP: no inline script/style, no
#    third-party origin — it must work when the internet is down.
_UI_DIR = Path(__file__).parent / "ui"
_UI_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


@app.get("/ui", include_in_schema=False)
async def status_page():
    return FileResponse(_UI_DIR / "index.html", headers=_UI_HEADERS)


app.mount("/ui/assets", StaticFiles(directory=_UI_DIR), name="ui-assets")
