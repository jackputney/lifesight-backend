"""HTTP: per-user Google OAuth under /integrations/google.

Frozen contract for iOS:
  GET  /integrations/google/status
  POST /integrations/google/start
  GET  /integrations/google/callback   (Google → backend; no bearer)
  POST /integrations/google/disconnect
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from shared.auth import get_current_user_id
from shared.google import oauth
from shared.google.capabilities import UnknownCapabilityError
from shared.google.connection_service import GoogleConnectionService

router = APIRouter(prefix="/integrations/google", tags=["integrations-google"])


class GoogleStartRequest(BaseModel):
    app_return_uri: str = Field(..., min_length=1)
    # Capability names only — never raw OAuth scope URLs.
    capabilities: Optional[list[str]] = None


class GoogleStartResponse(BaseModel):
    authorization_url: str
    expires_in: int


class GoogleStatusResponse(BaseModel):
    connected: bool
    email: Optional[str] = None
    capabilities: dict[str, bool]


class GoogleDisconnectResponse(BaseModel):
    disconnected: bool


@router.get("/status", response_model=GoogleStatusResponse)
async def google_status(user_id: str = Depends(get_current_user_id)):
    """Connection metadata for the authenticated user only — never tokens."""
    payload = await GoogleConnectionService.get_status(user_id)
    return GoogleStatusResponse(**payload)


@router.post("/start", response_model=GoogleStartResponse)
async def google_start(
    body: GoogleStartRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Begin OAuth for the authenticated LifeSight user.

    Ownership is taken only from the bearer identity — body must not include
    user_id (and is ignored if a client invents one).
    """
    try:
        result = await oauth.start_authorization(
            user_id,
            app_return_uri=body.app_return_uri,
            requested_capabilities=body.capabilities,
        )
    except oauth.OAuthConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except UnknownCapabilityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return GoogleStartResponse(
        authorization_url=result["authorization_url"],
        expires_in=int(result["expires_in"]),
    )


@router.get("/callback")
async def google_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Google redirects here. Exchange code server-side; redirect to app with
    result= only. No tokens in the redirect.
    """
    app_return: str | None = None
    try:
        if error:
            # Best-effort: if state present, recover app_return for redirect.
            if state:
                from shared.google.transactions import get_transaction

                row = await get_transaction(state)
                if row:
                    app_return = row.get("app_return_uri")
            if app_return:
                return RedirectResponse(
                    oauth.build_app_redirect(app_return, result="error"),
                    status_code=302,
                )
            raise HTTPException(status_code=400, detail="OAuth error")

        if not code or not state:
            raise HTTPException(status_code=400, detail="Missing code or state")

        material = await oauth.complete_authorization(code=code, state=state)
        app_return = material["app_return_uri"]
        await GoogleConnectionService.persist_authorized_connection(
            user_id=material["user_id"],
            google_subject=material["google_subject"],
            google_email=material.get("google_email"),
            display_name=material.get("display_name"),
            refresh_token=material["refresh_token"],
            granted_scopes=list(material.get("scopes") or []),
        )
        return RedirectResponse(
            oauth.build_app_redirect(app_return, result="success"),
            status_code=302,
        )
    except oauth.OAuthFlowError as exc:
        if exc.app_return_uri:
            result = (
                "reauth_required"
                if "refresh token" in str(exc).lower()
                else "error"
            )
            return RedirectResponse(
                oauth.build_app_redirect(exc.app_return_uri, result=result),
                status_code=302,
            )
        raise HTTPException(status_code=400, detail="OAuth flow failed") from exc
    except oauth.OAuthConfigError as exc:
        if app_return:
            return RedirectResponse(
                oauth.build_app_redirect(app_return, result="error"),
                status_code=302,
            )
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/disconnect", response_model=GoogleDisconnectResponse)
async def google_disconnect(user_id: str = Depends(get_current_user_id)):
    revoked = await GoogleConnectionService.disconnect(user_id)
    return GoogleDisconnectResponse(disconnected=bool(revoked) or True)
