import logging
import secrets
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from src.config import settings
from src.models.schemas import (
    InboxListResponse,
    InboxMessage,
    ModemStatus,
    OutboxEntry,
    SendHistoryEntry,
    SendSMSRequest,
    SendSMSResponse,
)
from src.errors import describe_error
from src.services.idempotency import IdempotencyConflict
from src.services.send_limits import DuplicateMessage, RateLimitExceeded
from src.services.sms_service import SMSService, options_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sms", tags=["SMS"], dependencies=[])


NO_KEY_DETAIL = ("SMS_API_KEY is not set: the API is closed. Set SMS_API_KEY "
                 "(or SMS_ALLOW_NO_AUTH=true to run without authentication) and restart.")


def get_sms_service() -> SMSService:
    """Dependency — overridden in main.py via app.dependency_overrides."""
    raise RuntimeError("SMSService not initialized")


def require_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Every ``/api/sms/*`` request must carry ``X-API-Key`` = ``SMS_API_KEY``.

    Fail closed: with no key configured the API is refused (503) rather
    than left open — it can spend SIM credit. ``SMS_ALLOW_NO_AUTH=true``
    is the explicit opt-out (closed networks only).
    """
    expected = settings.sms_api_key
    if not expected:
        if settings.sms_allow_no_auth:
            return
        raise HTTPException(status_code=503, detail=NO_KEY_DETAIL)
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


SMS = Annotated[SMSService, Depends(get_sms_service)]
Auth = Depends(require_api_key)


@router.post(
    "/send",
    response_model=list[SendSMSResponse],
    responses={202: {"description": "Accepted into the outbox; delivered when the router is back"}},
)
async def send_sms(req: SendSMSRequest, sms: SMS, response: Response, _: None = Auth):
    """Send an SMS. If no number given, uses SMS_DEFAULT_RECIPIENTS.

    200 — sent. 202 — at least one recipient was queued in the outbox
    (router unreachable) and will be retried until delivered or expired.
    A retry carrying the same ``idempotency_key`` gets the first result
    back (same status, ``replayed: true``, header ``Idempotent-Replayed``).
    Full contract: docs/API.md.
    """
    try:
        results = await sms.send(
            message=req.message, number=req.number, priority=req.priority,
            options=options_from_request(req),
        )
        if any(r.queued for r in results):
            response.status_code = 202
        if results and results[0].replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return [
            SendSMSResponse(
                success=r.success,
                number=r.number,
                detail=r.detail,
                timestamp=r.timestamp,
                queued=r.queued,
                outbox_id=r.outbox_id,
                segments=r.segments,
                replayed=r.replayed,
            )
            for r in results
        ]
    except IdempotencyConflict as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=429,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after or 60)},
        )
    except DuplicateMessage as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (ConnectionError, httpx.HTTPError) as e:
        raise HTTPException(status_code=502, detail=f"Teltonika error: {describe_error(e)}")
    except Exception as e:
        logger.exception("Send SMS error: %s", describe_error(e))
        raise HTTPException(status_code=500, detail=describe_error(e))


def _inbox(sms: SMSService):
    if sms.inbox is None:
        raise HTTPException(status_code=503, detail="Inbox service not running")
    return sms.inbox


def _inbox_message(r) -> InboxMessage:
    return InboxMessage(
        id=str(r.id), sender=r.sender, message=r.message,
        date=(r.modem_date or r.received_at).isoformat(), read=True,
        received_at=r.received_at, authorized=r.authorized, stale=r.stale,
        published=r.published_at is not None, forwarded=r.forwarded_at is not None,
        forward_error=r.forward_error,
    )


@router.get("/inbox", response_model=InboxListResponse)
async def list_inbox(sms: SMS, limit: Annotated[int, Query(ge=1, le=200)] = 50, _: None = Auth):
    """Received SMS from the archive (newest first). The modem itself is
    emptied after archiving — this is the record of what arrived."""
    items = [_inbox_message(r) for r in await _inbox(sms).recent(limit)]
    return InboxListResponse(messages=items, count=len(items))


@router.get("/inbox/{msg_id}", response_model=InboxMessage)
async def read_inbox_message(msg_id: int, sms: SMS, _: None = Auth):
    """One archived SMS."""
    rec = await _inbox(sms).get(msg_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return _inbox_message(rec)


@router.delete("/inbox/{msg_id}")
async def delete_inbox_message(msg_id: int, sms: SMS, _: None = Auth):
    """Delete one archived SMS."""
    if not await _inbox(sms).delete(msg_id):
        raise HTTPException(status_code=404, detail="Message not found")
    return {"success": True}


@router.get("/status", response_model=ModemStatus)
async def modem_status(sms: SMS, _: None = Auth):
    """Get modem/SIM/signal status."""
    try:
        raw = await sms.get_status()
        return ModemStatus(**{k: raw.get(k) for k in ModemStatus.model_fields})
    except (ConnectionError, httpx.HTTPError) as e:
        raise HTTPException(status_code=502, detail=describe_error(e))


@router.get("/metrics")
async def metrics(sms: SMS, _: None = Auth) -> dict[str, int | float]:
    """Counters since start, outbox state and last known signal (flat, numeric)."""
    return sms.metrics()


@router.get("/budget")
async def budget(sms: SMS, _: None = Auth) -> dict:
    """Live rate-limit usage per window, dedup window, and connection state."""
    return {**sms.limits.budget(), "connections": sms.connections()}


@router.get("/stats")
async def stats(sms: SMS, days: Annotated[int, Query(ge=1, le=90)] = 30, _: None = Auth) -> dict:
    """Per-day outcomes (sent / blocked / duplicate / failed / queued) and billed
    segments, plus per-type totals, over the last ``days`` local days."""
    return await sms.history.stats(days)


@router.get("/history/search", response_model=list[SendHistoryEntry])
async def search_history(
    sms: SMS,
    q: Annotated[str | None, Query(max_length=100)] = None,
    type: Annotated[str | None, Query(max_length=64)] = None,
    source: Annotated[str | None, Query(max_length=32)] = None,
    status: Annotated[str | None, Query(pattern="^(sent|blocked|duplicate|failed|queued)$")] = None,
    priority: Annotated[str | None, Query(pattern="^(normal|critical)$")] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: None = Auth,
):
    """Search the full send history (MySQL, ``SMS_HISTORY_RETENTION_DAYS``)."""
    return await sms.history.search(q=q or None, type=type or None, source=source or None,
                                    status=status, priority=priority, since=since,
                                    until=until, limit=limit, offset=offset)


@router.get("/history", response_model=list[SendHistoryEntry])
async def send_history(sms: SMS, limit: Annotated[int, Query(ge=1, le=100)] = 20, _: None = Auth):
    """Get recent send history."""
    return list(sms.history)[:limit]


def _outbox(sms: SMSService):
    if sms.outbox is None:
        raise HTTPException(status_code=404, detail="Outbox disabled (SMS_OUTBOX_ENABLED=false)")
    return sms.outbox


@router.get("/outbox", response_model=list[OutboxEntry])
async def list_outbox(sms: SMS, _: None = Auth):
    """Pending (undelivered) messages, in delivery order."""
    return [
        OutboxEntry(
            id=i.id, number=i.number, message=i.message, priority=i.priority,
            created_at=i.created_at, attempts=i.attempts,
            next_attempt_at=i.next_attempt_at, last_error=i.last_error,
        )
        for i in _outbox(sms).pending()
    ]


@router.delete("/outbox/{item_id}")
async def cancel_outbox_item(item_id: int, sms: SMS, _: None = Auth):
    """Cancel a pending message so it is never delivered."""
    if not await _outbox(sms).cancel(item_id):
        raise HTTPException(status_code=404, detail="No pending outbox item with that id")
    return {"success": True}
