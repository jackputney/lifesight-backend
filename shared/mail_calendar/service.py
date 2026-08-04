"""Connection lifecycle + credential loading for Mail & Calendar.

Status mapping (connection check / refresh):
- missing credentials → disconnected
- valid or successfully refreshed access token → connected_read
- revoked / rejected refresh token → reauth_required
- temporary provider/network failure during refresh → error

Encryption: tokens are Fernet-encrypted and keyed by (user_id, provider).
Key rotation is incomplete — see oauth.ENCRYPTION_KEY_ROTATION_STATUS
(no key-version column on oauth_credentials yet).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from google.auth.exceptions import RefreshError

from shared import crypto, db
from shared.mail_calendar import oauth
from shared.mail_calendar.google_calendar import GoogleCalendarProvider
from shared.mail_calendar.google_mail import GoogleMailProvider
from shared.mail_calendar.types import (
    ConnectionStatus,
    MailCalendarStatusOut,
)


class MailCalendarError(Exception):
    def __init__(self, status: ConnectionStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def get_status(user_id: str) -> MailCalendarStatusOut:
    try:
        oauth.require_oauth_config()
    except oauth.OAuthConfigError as exc:
        return MailCalendarStatusOut(
            status=ConnectionStatus.error,
            detail=str(exc),
        )

    row = await db.get_oauth_credentials(user_id, oauth.PROVIDER_ID)
    if row is None:
        return MailCalendarStatusOut(status=ConnectionStatus.disconnected)

    scopes = list(row.get("scopes") or [])
    try:
        await _load_valid_credentials(user_id, row)
    except MailCalendarError as exc:
        return MailCalendarStatusOut(
            status=exc.status,
            scopes=scopes,
            detail=exc.detail,
        )
    except Exception as exc:
        return MailCalendarStatusOut(
            status=ConnectionStatus.error,
            scopes=scopes,
            detail=f"Credential check failed: {type(exc).__name__}",
        )

    return MailCalendarStatusOut(
        status=ConnectionStatus.connected_read,
        scopes=scopes,
    )


async def persist_tokens(
    user_id: str,
    *,
    access_token: str,
    refresh_token: Optional[str],
    scopes: list[str],
    expiry: Optional[datetime],
) -> None:
    access_enc = crypto.encrypt(access_token)
    refresh_enc = crypto.encrypt(refresh_token) if refresh_token else None
    expires_at = expiry
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    await db.save_oauth_credentials(
        user_id,
        oauth.PROVIDER_ID,
        access_enc,
        refresh_enc,
        scopes,
        expires_at,
    )


async def disconnect(user_id: str) -> bool:
    return await db.delete_oauth_credentials(user_id, oauth.PROVIDER_ID)


async def get_mail_provider(user_id: str) -> GoogleMailProvider:
    creds = await _require_connected_credentials(user_id)
    return GoogleMailProvider(creds)


async def get_calendar_provider(user_id: str) -> GoogleCalendarProvider:
    creds = await _require_connected_credentials(user_id)
    return GoogleCalendarProvider(creds)


async def _require_connected_credentials(user_id: str):
    row = await db.get_oauth_credentials(user_id, oauth.PROVIDER_ID)
    if row is None:
        raise MailCalendarError(
            ConnectionStatus.disconnected,
            "Mail & Calendar is not connected.",
        )
    return await _load_valid_credentials(user_id, row)


async def _load_valid_credentials(user_id: str, row: dict):
    try:
        access = crypto.decrypt(row["access_token_enc"]) if row.get("access_token_enc") else ""
        refresh = (
            crypto.decrypt(row["refresh_token_enc"])
            if row.get("refresh_token_enc")
            else None
        )
    except ValueError as exc:
        raise MailCalendarError(
            ConnectionStatus.reauth_required,
            "Stored credentials could not be decrypted — reconnect Mail & Calendar.",
        ) from exc

    scopes = list(row.get("scopes") or oauth.READ_SCOPES)
    expires_at = row.get("expires_at")
    needs_refresh = False
    if not access:
        needs_refresh = True
    elif expires_at is not None:
        exp = expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= datetime.now(timezone.utc):
            needs_refresh = True

    if needs_refresh:
        if not refresh:
            raise MailCalendarError(
                ConnectionStatus.reauth_required,
                "Access expired and no refresh token is stored — reconnect.",
            )
        try:
            refreshed = oauth.refresh_access_token(refresh, scopes)
        except RefreshError as exc:
            raise MailCalendarError(
                ConnectionStatus.reauth_required,
                "Google rejected the refresh token — reconnect Mail & Calendar.",
            ) from exc
        except oauth.OAuthConfigError:
            raise
        except Exception as exc:
            raise MailCalendarError(
                ConnectionStatus.error,
                f"Token refresh failed: {type(exc).__name__}",
            ) from exc
        await persist_tokens(
            user_id,
            access_token=refreshed["access_token"],
            refresh_token=refreshed.get("refresh_token") or refresh,
            scopes=list(refreshed.get("scopes") or scopes),
            expiry=refreshed.get("expiry"),
        )
        access = refreshed["access_token"]
        refresh = refreshed.get("refresh_token") or refresh
        scopes = list(refreshed.get("scopes") or scopes)

    return oauth.credentials_from_tokens(access, refresh, scopes)
