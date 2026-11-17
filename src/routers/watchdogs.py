"""Watchdogs API (docs/adr/0001-watchdogs.md, docs/API.md#watchdogs)."""
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response

from src.config import settings
from src.models.schemas import WATCHDOG_NAME, WatchdogInfo, WatchdogPing, WatchdogPingResponse
from src.phone import normalize_phone
from src.routers.sms import require_api_key
from src.services.watchdogs import TooManyWatchdogs, WatchdogService

router = APIRouter(prefix="/api/watchdogs", tags=["Watchdogs"],
                   dependencies=[Depends(require_api_key)])


def get_watchdog_service() -> WatchdogService:
    """Dependency — overridden in main.py."""
    raise RuntimeError("WatchdogService not initialized")


Watchdogs = Annotated[WatchdogService, Depends(get_watchdog_service)]
Name = Annotated[str, Path(pattern=WATCHDOG_NAME, description="[a-z0-9][a-z0-9._-]{0,63}")]


@router.post("/{name}/ping", response_model=WatchdogPingResponse)
async def ping(name: Name, body: WatchdogPing, dogs: Watchdogs):
    """Check in (and register). Answers with Kurier's own view of the modem,
    so the caller watches Kurier with the same request."""
    try:
        to = [normalize_phone(n, settings.sms_default_country_code) for n in body.to or []]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    try:
        rec = await dogs.ping(name, body.timeout_sec, body.status, body.detail, to)
    except TooManyWatchdogs as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"modem": dogs.modem(),
            "watchdog": {"state": rec.state, "last_ping": rec.last_ping}}


@router.delete("/{name}", status_code=204, responses={404: {"description": "Unknown watchdog"}})
async def delete(name: Name, dogs: Watchdogs):
    """Forget a watchdog (planned stop, maintenance): no alert."""
    if not await dogs.delete(name):
        raise HTTPException(status_code=404, detail=f"no watchdog named {name!r}")
    return Response(status_code=204)


@router.get("", response_model=list[WatchdogInfo])
async def list_watchdogs(dogs: Watchdogs):
    """Every watchdog and its state (status page)."""
    return [asdict(r) for r in dogs.list()]
