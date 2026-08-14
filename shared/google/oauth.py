"""Google OAuth authorization-code flow with PKCE (per-user connections).

Ownership is always the authenticated LifeSight user_id from the JWT /
start request — never a browser-supplied user_id. OAuth state is a random
server-stored one-time token bound to that user in google_oauth_transactions.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx

from shared import crypto
from shared.google import capabilities as caps
from shared.google.transactions import (
    consume_transaction,
    create_transaction,
    get_transaction,
)

OAUTH_STATE_TTL_SECONDS = 600
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"

APP_RETURN_RESULTS = frozenset({"success", "error", "reauth_required"})

# KEY ROTATION: incomplete — TOKEN_ENCRYPTION_KEY has no version column on
# google_connections / google_oauth_transactions. Rotating without re-encrypt
# makes stored material unrecoverable.
ENCRYPTION_KEY_ROTATION_STATUS = "incomplete"


class OAuthConfigError(RuntimeError):
    """Required OAuth configuration is missing — fail closed."""


class OAuthFlowError(ValueError):
    """OAuth flow failure that may still carry an allowlisted app return URI."""

    def __init__(self, message: str, *, app_return_uri: str | None = None) -> None:
        super().__init__(message)
        self.app_return_uri = app_return_uri


def require_oauth_config() -> dict[str, str]:
    client_id = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    redirect_uri = (os.environ.get("GOOGLE_INTEGRATIONS_REDIRECT_URI") or "").strip()
    env = (os.environ.get("GOOGLE_OAUTH_ENV") or "development").strip().lower()
    if not redirect_uri and env == "development":
        # Local alias only — never use Docs-era GOOGLE_REDIRECT_URI in prod docs.
        redirect_uri = (os.environ.get("GOOGLE_REDIRECT_URI") or "").strip()
    token_key = (os.environ.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    allowlist_raw = (os.environ.get("GOOGLE_APP_RETURN_URI_ALLOWLIST") or "").strip()

    missing = [
        name
        for name, val in (
            ("GOOGLE_CLIENT_ID", client_id),
            ("GOOGLE_CLIENT_SECRET", client_secret),
            ("GOOGLE_INTEGRATIONS_REDIRECT_URI", redirect_uri),
            ("TOKEN_ENCRYPTION_KEY", token_key),
            ("GOOGLE_APP_RETURN_URI_ALLOWLIST", allowlist_raw),
        )
        if not val
    ]
    if missing:
        raise OAuthConfigError(
            "Google integrations OAuth is not configured. Missing: "
            + ", ".join(missing)
        )

    if env not in ("development", "production"):
        raise OAuthConfigError("GOOGLE_OAUTH_ENV must be development or production")

    if not _backend_callback_allowed(redirect_uri, env=env):
        raise OAuthConfigError(
            "GOOGLE_INTEGRATIONS_REDIRECT_URI must be an allowlisted backend "
            "callback (/integrations/google/callback). Localhost http is "
            "development-only; production requires https."
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "env": env,
        "app_return_allowlist": allowlist_raw,
    }


def _backend_callback_allowed(uri: str, *, env: str) -> bool:
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    if parsed.path.rstrip("/") != "/integrations/google/callback":
        return False
    if parsed.query or parsed.fragment:
        return False
    if not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if env == "development":
        if parsed.scheme not in ("http", "https"):
            return False
        if host in ("127.0.0.1", "localhost") and parsed.scheme == "http":
            return True
        return parsed.scheme == "https"
    if parsed.scheme != "https":
        return False
    if host in ("127.0.0.1", "localhost"):
        return False
    return True


def validate_app_return_uri(uri: str, *, allowlist_csv: str | None = None) -> str:
    cfg_allow = allowlist_csv
    if cfg_allow is None:
        cfg_allow = require_oauth_config()["app_return_allowlist"]
    allowed = {u.strip() for u in cfg_allow.split(",") if u.strip()}
    candidate = (uri or "").strip()
    if not candidate:
        raise ValueError("app_return_uri is required")
    if candidate not in allowed:
        raise ValueError("app_return_uri is not allowlisted")
    parsed = urlparse(candidate)
    if parsed.query or parsed.fragment:
        raise ValueError("app_return_uri must not include query or fragment")
    if not parsed.scheme or parsed.scheme in ("javascript", "data", "file"):
        raise ValueError("app_return_uri scheme is not allowed")
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("http app_return_uri only allowed for localhost")
    return candidate


def build_app_redirect(app_return_uri: str, *, result: str) -> str:
    if result not in APP_RETURN_RESULTS:
        raise ValueError(
            "app return result must be one of: " + ", ".join(sorted(APP_RETURN_RESULTS))
        )
    parsed = urlparse(app_return_uri)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            urlencode({"result": result}),
            "",
        )
    )


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def start_authorization(
    user_id: str,
    *,
    app_return_uri: str,
    requested_capabilities: list[str] | None = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Create single-use transaction + PKCE; return Google authorization URL.

    `user_id` MUST come from authenticated Depends(get_current_user_id).
    """
    cfg = require_oauth_config()
    return_uri = validate_app_return_uri(
        app_return_uri, allowlist_csv=cfg["app_return_allowlist"]
    )
    capability_list = caps.normalize_capabilities(requested_capabilities)
    scope_list = caps.scopes_for_capabilities(capability_list)

    issued = now or datetime.now(timezone.utc)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    expires_at = issued + timedelta(seconds=OAUTH_STATE_TTL_SECONDS)

    await create_transaction(
        state=state,
        user_id=user_id,
        code_verifier_enc=crypto.encrypt(verifier),
        app_return_uri=return_uri,
        requested_capabilities=capability_list,
        expires_at=expires_at,
    )

    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": " ".join(scope_list),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {
        "authorization_url": f"{GOOGLE_AUTH_URI}?{urlencode(params)}",
        "expires_in": OAUTH_STATE_TTL_SECONDS,
    }


async def complete_authorization(
    *,
    code: str,
    state: str,
) -> dict[str, Any]:
    """Consume transaction, exchange code with PKCE, fetch identity, return
    material for persistence. Never returns tokens to HTTP clients.
    """
    row = await consume_transaction(state)
    if row is None:
        existing = await get_transaction(state)
        if existing is not None and existing.get("consumed_at") is not None:
            raise OAuthFlowError(
                "OAuth state already used",
                app_return_uri=existing.get("app_return_uri"),
            )
        if existing is not None:
            raise OAuthFlowError(
                "OAuth state expired",
                app_return_uri=existing.get("app_return_uri"),
            )
        raise OAuthFlowError("Unknown OAuth state")

    return_uri = str(row["app_return_uri"])
    bound_user = str(row["user_id"])

    try:
        verifier = crypto.decrypt(row["code_verifier_enc"])
    except ValueError as exc:
        raise OAuthFlowError(
            "Could not decrypt PKCE verifier", app_return_uri=return_uri
        ) from exc

    cfg = require_oauth_config()
    try:
        tokens = await _exchange_code_pkce(
            code=code,
            code_verifier=verifier,
            redirect_uri=cfg["redirect_uri"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
        )
    except Exception as exc:
        raise OAuthFlowError(
            f"Token exchange failed: {type(exc).__name__}",
            app_return_uri=return_uri,
        ) from exc

    refresh = tokens.get("refresh_token")
    if not refresh:
        raise OAuthFlowError(
            "Google did not return a refresh token; reconnect with consent",
            app_return_uri=return_uri,
        )

    try:
        identity = await _fetch_userinfo(tokens["access_token"])
    except Exception as exc:
        raise OAuthFlowError(
            f"Userinfo failed: {type(exc).__name__}",
            app_return_uri=return_uri,
        ) from exc

    subject = (identity.get("sub") or "").strip()
    if not subject:
        raise OAuthFlowError(
            "Google userinfo missing subject",
            app_return_uri=return_uri,
        )

    return {
        "user_id": bound_user,
        "app_return_uri": return_uri,
        "refresh_token": refresh,
        "access_token": tokens["access_token"],
        "scopes": tokens.get("scopes") or [],
        "google_subject": subject,
        "google_email": (identity.get("email") or None),
        "display_name": (identity.get("name") or None),
        "requested_capabilities": list(row.get("requested_capabilities") or []),
    }


async def _exchange_code_pkce(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(GOOGLE_TOKEN_URI, data=data)
        if resp.status_code >= 400:
            raise RuntimeError(f"Token exchange HTTP {resp.status_code}")
        payload = resp.json()
    access = payload.get("access_token")
    if not access:
        raise RuntimeError("Token exchange returned no access_token")
    scope_str = payload.get("scope") or ""
    return {
        "access_token": access,
        "refresh_token": payload.get("refresh_token"),
        "scopes": [s for s in scope_str.split() if s],
        "expires_in": payload.get("expires_in"),
    }


async def _fetch_userinfo(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            GOOGLE_USERINFO_URI,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Userinfo HTTP {resp.status_code}")
        return resp.json()
