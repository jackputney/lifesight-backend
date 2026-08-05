"""Google OAuth for Mail & Calendar (read scopes) — hardened.

- Backend callback: localhost only in development; HTTPS in production.
- After token exchange, redirect to an allowlisted app return URI with only
  `result=success|error|reauth_required` (no tokens, codes, or error details).
- HMAC state with user binding + nonce + short TTL; single-use via transactions.
- PKCE S256 with verifier stored encrypted in oauth_transactions (not credentials).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from shared import crypto
from shared.mail_calendar import transactions

PROVIDER_ID = "google_mail_calendar"

READ_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]

OAUTH_STATE_TTL_SECONDS = 600
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# KEY ROTATION: incomplete — TOKEN_ENCRYPTION_KEY has no version column on
# oauth_credentials / oauth_transactions. Rotating the key without a
# re-encrypt migration makes stored material unrecoverable.
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
    redirect_uri = (os.environ.get("GOOGLE_MAIL_CALENDAR_REDIRECT_URI") or "").strip()
    # Optional alias only in development.
    env = (os.environ.get("MAIL_CALENDAR_OAUTH_ENV") or "development").strip().lower()
    if not redirect_uri and env == "development":
        redirect_uri = (os.environ.get("GOOGLE_REDIRECT_URI") or "").strip()
    state_secret = (os.environ.get("OAUTH_STATE_SECRET") or "").strip()
    token_key = (os.environ.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    allowlist_raw = (os.environ.get("MAIL_CALENDAR_APP_RETURN_URI_ALLOWLIST") or "").strip()

    missing = [
        name
        for name, val in (
            ("GOOGLE_CLIENT_ID", client_id),
            ("GOOGLE_CLIENT_SECRET", client_secret),
            ("GOOGLE_MAIL_CALENDAR_REDIRECT_URI", redirect_uri),
            ("OAUTH_STATE_SECRET", state_secret),
            ("TOKEN_ENCRYPTION_KEY", token_key),
            ("MAIL_CALENDAR_APP_RETURN_URI_ALLOWLIST", allowlist_raw),
        )
        if not val
    ]
    if missing:
        raise OAuthConfigError(
            "Mail & Calendar OAuth is not configured. Missing: " + ", ".join(missing)
        )

    if env not in ("development", "production"):
        raise OAuthConfigError("MAIL_CALENDAR_OAUTH_ENV must be development or production")

    if not _backend_callback_allowed(redirect_uri, env=env):
        raise OAuthConfigError(
            "GOOGLE_MAIL_CALENDAR_REDIRECT_URI must be an allowlisted backend "
            "callback (/mail-calendar/oauth/callback). Localhost http is "
            "development-only; production requires https."
        )

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "state_secret": state_secret,
        "env": env,
        "app_return_allowlist": allowlist_raw,
    }


def _backend_callback_allowed(uri: str, *, env: str) -> bool:
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    if parsed.path.rstrip("/") != "/mail-calendar/oauth/callback":
        return False
    if parsed.query or parsed.fragment:
        return False
    if not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if env == "development":
        if parsed.scheme not in ("http", "https"):
            return False
        # Localhost callback is development-only.
        if host in ("127.0.0.1", "localhost") and parsed.scheme == "http":
            return True
        # Allow https even in development for tunneling.
        return parsed.scheme == "https"
    # production: HTTPS only, never localhost as the Google redirect.
    if parsed.scheme != "https":
        return False
    if host in ("127.0.0.1", "localhost"):
        return False
    return True


def validate_app_return_uri(uri: str, *, allowlist_csv: str | None = None) -> str:
    """Validate client app return URI against allowlist (universal link or custom scheme)."""
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


# Public app-return contract (iOS ASWebAuthenticationSession). Backend emits
# only `result`; cancelled is iOS-local and must not be generated here.
APP_RETURN_RESULTS = frozenset({"success", "error", "reauth_required"})


def build_app_redirect(app_return_uri: str, *, result: str) -> str:
    """Append only `result=` — never tokens, codes, user ids, or error details."""
    if result not in APP_RETURN_RESULTS:
        raise ValueError(
            "app return result must be one of: " + ", ".join(sorted(APP_RETURN_RESULTS))
        )
    parsed = urlparse(app_return_uri)
    # Preserve path; replace query entirely with the single public result param.
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


def _sign_state(user_id: str, nonce: str, issued: int, secret: str) -> str:
    body = f"{user_id}:{issued}:{nonce}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}:{sig}"


def verify_oauth_state_signature(state: str, *, now: Optional[float] = None) -> str:
    """Validate HMAC/TTL only (does not consume). Returns bound user_id."""
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
        cfg["state_secret"].encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("OAuth state signature mismatch")
    return user_id


async def start_authorization(
    user_id: str,
    *,
    app_return_uri: str,
    now: Optional[float] = None,
) -> dict[str, str]:
    """Create single-use transaction + PKCE and return Google auth URL + state."""
    cfg = require_oauth_config()
    return_uri = validate_app_return_uri(
        app_return_uri, allowlist_csv=cfg["app_return_allowlist"]
    )
    issued = int(now if now is not None else time.time())
    nonce = secrets.token_hex(8)
    state = _sign_state(user_id, nonce, issued, cfg["state_secret"])
    verifier, challenge = _pkce_pair()
    expires_at = datetime.fromtimestamp(issued + OAUTH_STATE_TTL_SECONDS, tz=timezone.utc)
    await transactions.create(
        state=state,
        user_id=user_id,
        provider=PROVIDER_ID,
        code_verifier_enc=crypto.encrypt(verifier),
        app_return_uri=return_uri,
        expires_at=expires_at,
    )
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": " ".join(READ_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {
        "authorization_url": f"{GOOGLE_AUTH_URI}?{urlencode(params)}",
        "state": state,
        "redirect_uri": cfg["redirect_uri"],
        "env": cfg["env"],
    }


async def complete_authorization(
    *,
    code: str,
    state: str,
    expected_user_id: str | None = None,
    now: Optional[float] = None,
) -> dict:
    """Consume transaction, exchange code with PKCE, return tokens + app_return_uri.

    Never returns tokens to the HTTP client — caller persists then redirects.
    """
    # Signature/TTL first (clear errors), then single-use consume.
    bound_user = verify_oauth_state_signature(state, now=now)
    if expected_user_id is not None and bound_user != expected_user_id:
        raise ValueError("OAuth state user mismatch")

    row = await transactions.consume(state)
    if row is None:
        existing = await transactions.get(state)
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

    # Row stays until purge (consumed_at set) so replay is distinguishable from
    # unknown state. Verifier is only used once; do not return it to clients.
    return_uri = row["app_return_uri"]
    if str(row["user_id"]) != bound_user:
        raise OAuthFlowError("OAuth state user mismatch", app_return_uri=return_uri)

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

    tokens["user_id"] = bound_user
    tokens["app_return_uri"] = return_uri
    return tokens


async def _exchange_code_pkce(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> dict:
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
    expires_in = payload.get("expires_in")
    expiry = None
    if expires_in is not None:
        expiry = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    scope_str = payload.get("scope") or " ".join(READ_SCOPES)
    return {
        "access_token": access,
        "refresh_token": payload.get("refresh_token"),
        "scopes": scope_str.split(),
        "expiry": expiry,
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
        token_uri=GOOGLE_TOKEN_URI,
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
