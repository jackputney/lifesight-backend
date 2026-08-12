# shared/auth.py
# Auth injection point. Endpoints depend on get_current_user_id and never
# decode tokens themselves — so mode swaps touch ONLY this file.
#
#   AUTH_MODE=dev   (default for local) -> fixed DEV_FAKE_USER_ID (bypass only).
#   AUTH_MODE=self  -> verify short-lived self-hosted access JWTs.
#
# Staging/production (APP_ENV/ENVIRONMENT in staging|stage|production|prod)
# MUST use AUTH_MODE=self and a non-empty AUTH_JWT_SECRET. Identity always
# comes from authentication — never from a request-body user_id.

from __future__ import annotations

import os

from fastapi import Header, HTTPException

DEV_FAKE_USER_ID = "00000000-0000-4000-8000-000000000001"

# Environments that must never use the local-dev auth bypass.
DEPLOY_ENVIRONMENTS = frozenset({"production", "prod", "staging", "stage"})


def auth_mode() -> str:
    return (os.getenv("AUTH_MODE") or "dev").strip().lower()


def app_environment() -> str:
    return (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()


def is_deploy_environment() -> bool:
    return app_environment() in DEPLOY_ENVIRONMENTS


def _jwt_secret_configured() -> bool:
    return bool((os.environ.get("AUTH_JWT_SECRET") or "").strip())


def cors_allow_origins() -> list[str]:
    """Origins for CORSMiddleware.

    Local/dev: CORS_ALLOW_ORIGINS unset → ["*"].
    Staging/production: must be an explicit comma-separated allowlist (not *).
    """
    raw = (os.environ.get("CORS_ALLOW_ORIGINS") or "").strip()
    if not raw:
        if is_deploy_environment():
            raise RuntimeError(
                "CORS_ALLOW_ORIGINS must be set to an explicit allowlist "
                "when APP_ENV/ENVIRONMENT is staging/production"
            )
        return ["*"]
    origins = [part.strip() for part in raw.split(",") if part.strip()]
    if not origins:
        raise RuntimeError("CORS_ALLOW_ORIGINS is empty after parsing")
    if is_deploy_environment() and any(origin == "*" for origin in origins):
        raise RuntimeError(
            "CORS_ALLOW_ORIGINS must not include '*' in staging/production"
        )
    return origins


def assert_auth_mode_allowed() -> None:
    """Fail closed on auth / CORS misconfiguration before serving traffic."""
    mode = auth_mode()

    if mode not in ("dev", "self"):
        raise RuntimeError("AUTH_MODE must be 'dev' or 'self'")

    if is_deploy_environment():
        if mode != "self":
            raise RuntimeError(
                "AUTH_MODE must be 'self' when APP_ENV/ENVIRONMENT is "
                f"staging/production (got {mode!r})"
            )
        if not _jwt_secret_configured():
            raise RuntimeError(
                "AUTH_JWT_SECRET is required when AUTH_MODE=self "
                "in staging/production"
            )
        # Validate CORS config at startup (raises if missing / wildcard).
        cors_allow_origins()
        return

    if mode == "self" and not _jwt_secret_configured():
        raise RuntimeError("AUTH_JWT_SECRET is required when AUTH_MODE=self")


def _require_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


async def get_current_user_id(authorization: str = Header(None)) -> str:
    assert_auth_mode_allowed()
    mode = auth_mode()

    if mode == "dev":
        # Explicit local-development bypass only.
        return DEV_FAKE_USER_ID

    if mode != "self":
        raise HTTPException(
            status_code=500,
            detail="AUTH_MODE must be 'dev' or 'self'",
        )

    token = _require_bearer(authorization)
    import jwt  # PyJWT — lazy so import cost stays out of unrelated paths

    from shared.local_auth.service import AuthService
    from shared.local_auth.tokens import TokenConfigError, decode_access_token

    try:
        payload = decode_access_token(token)
    except TokenConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    user_id = str(payload["sub"])
    session_id = str(payload["sid"])
    try:
        await AuthService().assert_session_active(session_id, user_id)
    except Exception as exc:
        from shared.local_auth.service import AuthError

        if isinstance(exc, AuthError):
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        raise
    return user_id


async def get_current_session_id(authorization: str = Header(None)) -> str:
    """Session id from a verified access token (AUTH_MODE=self)."""
    assert_auth_mode_allowed()
    if auth_mode() == "dev":
        return "00000000-0000-4000-8000-0000000000de"
    token = _require_bearer(authorization)
    import jwt

    from shared.local_auth.tokens import TokenConfigError, decode_access_token

    try:
        payload = decode_access_token(token)
    except TokenConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None
    return str(payload["sid"])
