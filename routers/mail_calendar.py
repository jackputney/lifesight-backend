"""Mail & Calendar HTTP API — Google read-only foundation.

No send/archive/delete, no event mutations, no Confirm Gate / pending_action.
Never returns OAuth tokens to the client.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from shared.auth import get_current_user_id
from shared.mail_calendar import oauth, service
from shared.mail_calendar.types import (
    CalendarEvent,
    CalendarEventSummary,
    ConnectOut,
    ConnectionStatus,
    DisconnectOut,
    FreeBusyOut,
    MailCalendarStatusOut,
    MailMessage,
    MailMessageSummary,
)

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
async def mail_calendar_connect(user_id: str = Depends(get_current_user_id)):
    try:
        state = oauth.sign_oauth_state(user_id)
        url = oauth.build_authorization_url(state)
    except oauth.OAuthConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ConnectOut(authorization_url=url, state=state)


@router.get("/oauth/callback")
async def mail_calendar_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Browser callback — identity from signed state only (no Bearer header)."""
    if error:
        return HTMLResponse(
            content=_html_page("Mail & Calendar connection failed", error),
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    try:
        user_id = oauth.verify_oauth_state(state)
    except oauth.OAuthConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        tokens = oauth.exchange_code(code, state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Token exchange failed: {type(exc).__name__}",
        ) from exc

    access = tokens.get("access_token")
    if not access:
        raise HTTPException(status_code=502, detail="Google returned no access token")

    await service.persist_tokens(
        user_id,
        access_token=access,
        refresh_token=tokens.get("refresh_token"),
        scopes=list(tokens.get("scopes") or oauth.READ_SCOPES),
        expiry=tokens.get("expiry"),
    )
    return HTMLResponse(
        content=_html_page(
            "Mail & Calendar connected",
            "You can close this window and return to LifeSight.",
        )
    )


@router.post("/disconnect", response_model=DisconnectOut)
async def mail_calendar_disconnect(user_id: str = Depends(get_current_user_id)):
    await service.disconnect(user_id)
    return DisconnectOut()


@router.get("/mail", response_model=list[MailMessageSummary])
async def mail_list(
    q: str | None = Query(default=None, description="Gmail search query"),
    max_results: int = Query(default=20, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
):
    try:
        provider = await service.get_mail_provider(user_id)
        return await provider.list_messages(query=q, max_results=max_results)
    except service.MailCalendarError as exc:
        raise _map_mc_error(exc) from exc


@router.get("/mail/{message_id}", response_model=MailMessage)
async def mail_get(message_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        provider = await service.get_mail_provider(user_id)
        return await provider.get_message(message_id)
    except service.MailCalendarError as exc:
        raise _map_mc_error(exc) from exc


@router.get("/events", response_model=list[CalendarEventSummary])
async def events_list(
    time_min: str = Query(..., description="RFC3339 lower bound"),
    time_max: str = Query(..., description="RFC3339 upper bound"),
    max_results: int = Query(default=50, ge=1, le=100),
    calendar_id: str = Query(default="primary"),
    user_id: str = Depends(get_current_user_id),
):
    try:
        provider = await service.get_calendar_provider(user_id)
        return await provider.list_events(
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
            calendar_id=calendar_id,
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
        provider = await service.get_calendar_provider(user_id)
        return await provider.freebusy(
            time_min=time_min,
            time_max=time_max,
            calendar_id=calendar_id,
        )
    except service.MailCalendarError as exc:
        raise _map_mc_error(exc) from exc


def _html_page(title: str, body: str) -> str:
    # Minimal HTML for the browser OAuth return — not an iOS surface.
    safe_title = title.replace("<", "&lt;")
    safe_body = body.replace("<", "&lt;")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{safe_title}</title></head><body>"
        f"<h1>{safe_title}</h1><p>{safe_body}</p></body></html>"
    )
