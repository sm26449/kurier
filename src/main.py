import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src import __version__
from src.config import settings
from src.routers import health, sms
from src.wiring import build_services

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
    logger.info("Kurier starting…")
    if not settings.sms_api_key:
        if settings.sms_allow_no_auth:
            logger.warning("SMS_ALLOW_NO_AUTH=true: /api/sms/* is open to anyone who can "
                           "reach this port — they can send SMS on your SIM")
        else:
            logger.error("SMS_API_KEY is not set: /api/sms/* answers 503 until you set it "
                         "(or SMS_ALLOW_NO_AUTH=true) and restart")

    services = await build_services(settings)

    app.dependency_overrides[sms.get_sms_service] = lambda: services.sms
    app.dependency_overrides[health.get_teltonika_client] = lambda: services.teltonika
    app.state.services = services

    await services.start()
    logger.info("Kurier ready — API mode: %s", services.teltonika.api_mode or "pending")

    yield

    logger.info("Kurier shutting down…")
    await services.stop()


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
