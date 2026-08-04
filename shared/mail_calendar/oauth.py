"""Google OAuth for Mail & Calendar (read scopes). Isolated from Jarvis/Docs."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

# Distinct from Docs-era provider="google" rows.
PROVIDER_ID = "google_mail_calendar"

READ_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

OAUTH_STATE_TTL_SECONDS = 600
_pending_oauth_flows: dict[str, Flow] = {}


class OAuthConfigError(RuntimeError):
    """Required OAuth configuration is missing — fail closed."""


def require_oauth_config() -> dict[str, str]:
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    redirect_uri = (
        os.environ.get("GOOGLE_MAIL_CALENDAR_REDIRECT_URI")
        or os.environ.get("GOOGLE_REDIRECT_URI")
        or ""
    ).strip()
    state_secret = (os.environ.get("OAUTH_STATE_SECRET") or "").strip()
    token_key = (os.environ.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    missing = [
        name
        for name, val in (
            ("GOOGLE_CLIENT_ID", client_id),
            ("GOOGLE_CLIENT_SECRET", client_secret),
            ("GOOGLE_MAIL_CALENDAR_REDIRECT_URI (or GOOGLE_REDIRECT_URI)", redirect_uri),
            ("OAUTH_STATE_SECRET", state_secret),
            ("TOKEN_ENCRYPTION_KEY", token_key),
        )
        if not val
    ]
    if missing:
        raise OAuthConfigError(
            "Mail & Calendar OAuth is not configured. Missing: " + ", ".join(missing)
        )
    if not _redirect_uri_is_allowlisted(redirect_uri):
        raise OAuthConfigError(
            "GOOGLE_MAIL_CALENDAR_REDIRECT_URI is not an allowlisted http(s) URI "
            "path under /mail-calendar/oauth/callback."
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "state_secret": state_secret,
    }


def _redirect_uri_is_allowlisted(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    # Exact path match — refuse open redirects / query injection.
    if parsed.path.rstrip("/") != "/mail-calendar/oauth/callback":
        return False
    if parsed.query or parsed.fragment:
        return False
    return True


def _client_config(cfg: dict[str, str]) -> dict:
    return {
        "web": {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uris": [cfg["redirect_uri"]],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def sign_oauth_state(user_id: str, *, now: Optional[float] = None) -> str:
    """HMAC state binding the authenticated user. Format: user_id:ts:nonce:sig."""
    cfg = require_oauth_config()
    issued = int(now if now is not None else time.time())
    nonce = secrets.token_hex(8)
    body = f"{user_id}:{issued}:{nonce}"
    sig = hmac.new(
        cfg["state_secret"].encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{body}:{sig}"


def verify_oauth_state(state: str, *, now: Optional[float] = None) -> str:
    """Validate state and return the bound user_id. Raises ValueError on failure."""
    cfg = require_oauth_config()
    parts = (state or "").split(":")
    if len(parts) != 4:
        raise ValueError("Invalid OAuth state")
    user_id, ts_s, nonce, sig = parts
    if not user_id or not nonce or not sig:
        raise ValueError("Invalid OAuth state")
    try:
        issued = int(ts_s)
    except ValueError as exc:
        raise ValueError("Invalid OAuth state timestamp") from exc
    current = int(now if now is not None else time.time())
    if current - issued > OAUTH_STATE_TTL_SECONDS or issued > current + 30:
        raise ValueError("OAuth state expired")
    body = f"{user_id}:{ts_s}:{nonce}"
    expected = hmac.new(
        cfg["state_secret"].encode(),
        body.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("OAuth state signature mismatch")
    return user_id


def build_authorization_url(state: str) -> str:
    cfg = require_oauth_config()
    flow = Flow.from_client_config(_client_config(cfg), scopes=READ_SCOPES)
    flow.redirect_uri = cfg["redirect_uri"]
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="false",
        state=state,
    )
    _pending_oauth_flows[state] = flow
    return auth_url


def exchange_code(code: str, state: str) -> dict:
    flow = _pending_oauth_flows.pop(state, None)
    if flow is None:
        # Reconstruct flow for environments where process restarted mid-OAuth
        # (no PKCE verifier) — fail closed rather than half-succeed.
        raise ValueError(
            "No pending OAuth flow for this state — start again from connect."
        )
    flow.fetch_token(code=code)
    creds = flow.credentials
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "scopes": list(creds.scopes or READ_SCOPES),
        "expiry": creds.expiry,
    }


def credentials_from_tokens(
    access_token: str,
    refresh_token: Optional[str],
    scopes: list[str],
) -> Credentials:
    cfg = require_oauth_config()
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        scopes=scopes or READ_SCOPES,
    )


def refresh_access_token(refresh_token: str, scopes: list[str]) -> dict:
    creds = credentials_from_tokens("", refresh_token, scopes)
    creds.refresh(Request())
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token or refresh_token,
        "scopes": list(creds.scopes or scopes),
        "expiry": creds.expiry,
    }


def clear_pending_flow(state: str) -> None:
    _pending_oauth_flows.pop(state, None)
