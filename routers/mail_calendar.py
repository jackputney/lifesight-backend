"""Mail & Calendar HTTP API — Google read-only foundation.

No send/archive/delete, no event mutations, no Confirm Gate / pending_action.
Never returns OAuth tokens or authorization codes to the client.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from shared.auth import get_current_user_id
from shared.mail_calendar import bounds, oauth, service, transactions
from shared.mail_calendar.types import (
    CalendarEvent,
    ConnectIn,
    ConnectOut,
    ConnectionStatus,
    DisconnectOut,
    EventListOut,
    FreeBusyOut,
    MailCalendarStatusOut,
    MailListOut,
    MailMessage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mail-calendar", tags=["mail-calendar"])


def _map_mc_error(exc: service.MailCalendarError) -> HTTPException:
    code = {
        ConnectionStatus.disconnected: 409,
        ConnectionStatus.reauth_required: 401,
        ConnectionStatus.error: 503,
    }.get(exc.status, 400)
    return HTTPException(
        status_code=code,
        detail={"status": exc.status.value, "detail": exc.detail},
    )


@router.get("/status", response_model=MailCalendarStatusOut)
async def mail_calendar_status(user_id: str = Depends(get_current_user_id)):
    return await service.get_status(user_id)


@router.post("/connect", response_model=ConnectOut)
async def mail_calendar_connect(
    body: ConnectIn,
    user_id: str = Depends(get_current_user_id),
):
    try:
        started = await oauth.start_authorization(
            user_id, app_return_uri=body.app_return_uri
        )
    except oauth.OAuthConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ConnectOut(
        authorization_url=started["authorization_url"],
        state=started["state"],
    )


@router.get("/oauth/callback")
async def mail_calendar_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Browser callback — identity from signed state; redirect to app (no tokens)."""
    if error:
        return await _redirect_or_error(state, result="error", detail=error)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    try:
        tokens = await oauth.complete_authorization(code=code, state=state)
    except oauth.OAuthConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except oauth.OAuthFlowError as exc:
        if exc.app_return_uri:
            logger.warning("Mail & Calendar OAuth flow error: %s", exc)
            return RedirectResponse(
                oauth.build_app_redirect(exc.app_return_uri, result="error"),
                status_code=302,
            )
        return await _redirect_or_error(state, result="error", detail=str(exc))
    except ValueError as exc:
        return await _redirect_or_error(state, result="error", detail=str(exc))
    except Exception as exc:
        return await _redirect_or_error(
            state,
            result="error",
            detail=f"Token exchange failed: {type(exc).__name__}: {exc}",
        )

    access = tokens.get("access_token")
    if not access:
        logger.warning("Mail & Calendar OAuth: Google returned no access token")
        return RedirectResponse(
            oauth.build_app_redirect(tokens["app_return_uri"], result="error"),
            status_code=302,
        )

    await service.persist_tokens(
        tokens["user_id"],
        access_token=access,
        refresh_token=tokens.get("refresh_token"),
        scopes=list(tokens.get("scopes") or oauth.READ_SCOPES),
        expiry=tokens.get("expiry"),
    )
    # Redirect must never include access/refresh tokens or the auth code.
    return RedirectResponse(
        oauth.build_app_redirect(tokens["app_return_uri"], result="success"),
        status_code=302,
    )


async def _redirect_or_error(
    state: str | None, *, result: str, detail: str
) -> RedirectResponse:
    """Redirect with public `result` only; keep `detail` in server logs."""
    logger.warning("Mail & Calendar OAuth callback failure: %s", detail)
    return_uri = None
    if state:
        row = await transactions.get(state)
        if row:
            return_uri = row.get("app_return_uri")
            # Best-effort cleanup on failure paths.
            await transactions.delete(state)
    if return_uri:
        return RedirectResponse(
            oauth.build_app_redirect(return_uri, result=result),
            status_code=302,
        )
    raise HTTPException(status_code=400, detail="OAuth callback failed")


@router.post("/disconnect", response_model=DisconnectOut)
async def mail_calendar_disconnect(user_id: str = Depends(get_current_user_id)):
    await service.disconnect(user_id)
    return DisconnectOut()


@router.get("/mail", response_model=MailListOut)
async def mail_list(
    q: str | None = Query(default=None, description="Provider search query"),
    max_results: int = Query(default=bounds.DEFAULT_MAIL_PAGE_SIZE),
    page_token: str | None = Query(default=None),
    user_id: str = Depends(get_current_user_id),
):
    try:
        query = bounds.clamp_search_query(q)
        size = bounds.clamp_page_size(
            max_results,
            default=bounds.DEFAULT_MAIL_PAGE_SIZE,
            maximum=bounds.MAX_MAIL_PAGE_SIZE,
        )
    except bounds.BoundsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        provider = await service.get_mail_provider(user_id)
        return await provider.list_messages(
            query=query, max_results=size, page_token=page_token
        )
    except service.MailCalendarError as exc:
        raise _map_mc_error(exc) from exc


@router.get("/mail/{message_id}", response_model=MailMessage)
async def mail_get(message_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        provider = await service.get_mail_provider(user_id)
        return await provider.get_message(message_id)
    except service.MailCalendarError as exc:
        raise _map_mc_error(exc) from exc


@router.get("/events", response_model=EventListOut)
async def events_list(
    time_min: str = Query(..., description="RFC3339 lower bound"),
    time_max: str = Query(..., description="RFC3339 upper bound"),
    max_results: int = Query(default=bounds.DEFAULT_EVENTS_PAGE_SIZE),
    page_token: str | None = Query(default=None),
    calendar_id: str = Query(default="primary"),
    user_id: str = Depends(get_current_user_id),
):
    try:
        tmin, tmax = bounds.validate_time_range(time_min, time_max)
        size = bounds.clamp_page_size(
            max_results,
            default=bounds.DEFAULT_EVENTS_PAGE_SIZE,
            maximum=bounds.MAX_EVENTS_PAGE_SIZE,
        )
    except bounds.BoundsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        provider = await service.get_calendar_provider(user_id)
        return await provider.list_events(
            time_min=tmin,
            time_max=tmax,
            max_results=size,
            calendar_id=calendar_id,
            page_token=page_token,
        )
    except service.MailCalendarError as exc:
        raise _map_mc_error(exc) from exc


@router.get("/events/{event_id}", response_model=CalendarEvent)
async def events_get(
    event_id: str,
    calendar_id: str = Query(default="primary"),
    user_id: str = Depends(get_current_user_id),
):
    try:
        provider = await service.get_calendar_provider(user_id)
        return await provider.get_event(event_id, calendar_id=calendar_id)
    except service.MailCalendarError as exc:
        raise _map_mc_error(exc) from exc


@router.get("/freebusy", response_model=FreeBusyOut)
async def freebusy(
    time_min: str = Query(...),
    time_max: str = Query(...),
    calendar_id: str = Query(default="primary"),
    user_id: str = Depends(get_current_user_id),
):
    try:
        tmin, tmax = bounds.validate_time_range(time_min, time_max)
    except bounds.BoundsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        provider = await service.get_calendar_provider(user_id)
        return await provider.freebusy(
            time_min=tmin,
            time_max=tmax,
            calendar_id=calendar_id,
        )
    except service.MailCalendarError as exc:
        raise _map_mc_error(exc) from exc
