import logging
import time

from fastapi import APIRouter, Depends
from typing import Annotated

from src.models.schemas import HealthResponse
from src.routers.sms import SMS
from src.services.teltonika import TeltonikaClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

_start_time = time.monotonic()


def get_teltonika_client() -> TeltonikaClient:
    """Dependency — overridden in main.py."""
    raise RuntimeError("TeltonikaClient not initialized")


Teltonika = Annotated[TeltonikaClient, Depends(get_teltonika_client)]


@router.get("/api/health", response_model=HealthResponse)
async def health_check(client: Teltonika, sms: SMS):
    """SMS path health as data: modem reachability, SIM, outbox.

    Contract: always HTTP 200 while the process runs — the state is in
    the body (``status`` = healthy | degraded | unhealthy). Klopot's
    ``http_poll`` drops responses >= 400, so a non-200 here would hide
    exactly the "modem unreachable" value its rules alert on. Container
    liveness is covered by the Docker healthcheck / docker-monitor.
    """
    reachable = await client.is_reachable()

    sim_ok = False
    if reachable:
        try:
            status = await client.get_status()
            sim_state = status.get("sim_state", status.get("sim", ""))
            sim_ok = sim_state.lower() in ("ready", "inserted") if isinstance(sim_state, str) else bool(sim_state)
        except Exception as e:
            logger.warning("Health check status query failed: %s", e)

    overall = "healthy" if reachable and sim_ok else "degraded" if reachable else "unhealthy"

    return HealthResponse(
        status=overall,
        modem_reachable=reachable,
        sim_ok=sim_ok,
        api_mode=client.api_mode,
        uptime_seconds=round(time.monotonic() - _start_time, 1),
        outbox_pending=sms.outbox.pending_count if sms.outbox else 0,
        outbox_oldest_age_seconds=sms.outbox.oldest_age_seconds() if sms.outbox else 0,
    )
