"""Access JWTs (short-lived) + opaque refresh tokens (hashed at rest)."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

ACCESS_TOKEN_TTL_SECONDS = 15 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
ACCESS_AUD = "lifesight-access"


class TokenConfigError(RuntimeError):
    pass


def _jwt_secret() -> str:
    secret = (os.environ.get("AUTH_JWT_SECRET") or "").strip()
    if not secret:
        raise TokenConfigError("AUTH_JWT_SECRET is not configured")
    return secret


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def mint_access_token(*, user_id: str, session_id: str, now: datetime | None = None) -> tuple[str, int]:
    current = now or datetime.now(timezone.utc)
    expires_in = ACCESS_TOKEN_TTL_SECONDS
    payload = {
        "sub": user_id,
        "sid": session_id,
        "typ": "access",
        "aud": ACCESS_AUD,
        "iat": int(current.timestamp()),
        "exp": int((current + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm="HS256")
    return token, expires_in


def decode_access_token(token: str, *, now: datetime | None = None) -> dict[str, Any]:
    options = {"require": ["sub", "exp", "sid", "typ"]}
    kwargs: dict[str, Any] = {
        "algorithms": ["HS256"],
        "audience": ACCESS_AUD,
        "options": options,
    }
    if now is not None:
        kwargs["leeway"] = 0
        # PyJWT uses time.time(); for tests we patch via options + manual exp check.
    payload = jwt.decode(token, _jwt_secret(), **kwargs)
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    if now is not None:
        exp = int(payload["exp"])
        if int(now.timestamp()) >= exp:
            raise jwt.ExpiredSignatureError("expired")
    return payload


def refresh_expiry(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    return current + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS)
