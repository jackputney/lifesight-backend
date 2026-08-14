"""GoogleConnectionService — load/store/disconnect per-user Google connections."""

from __future__ import annotations

from typing import Any, Optional

from shared.google import connection_store
from shared.google.capabilities import CAPABILITY_IDS, capabilities_from_scopes
from shared.google.errors import GoogleFailureState, GoogleIntegrationError
from shared.google.token_service import GoogleTokenService


def status_payload(
    *,
    connected: bool,
    email: str | None = None,
    granted_scopes: list[str] | None = None,
) -> dict[str, Any]:
    """Frozen status JSON for iOS (no tokens)."""
    caps = capabilities_from_scopes(granted_scopes if connected else [])
    # Ensure all capability keys present even when disconnected.
    capabilities = {
        cap: bool(caps.get(cap)) if connected else False for cap in CAPABILITY_IDS
    }
    return {
        "connected": connected,
        "email": email if connected else None,
        "capabilities": capabilities,
    }


class GoogleConnectionService:
    @staticmethod
    async def get_active_connection(user_id: str) -> Optional[dict]:
        """Load this user's active connection row. Never accepts another user_id
        from the client — callers must pass authenticated identity only."""
        return await connection_store.get_active_google_connection(user_id)

    @classmethod
    async def get_status(cls, user_id: str) -> dict[str, Any]:
        row = await cls.get_active_connection(user_id)
        if row is None:
            return status_payload(connected=False)
        return status_payload(
            connected=True,
            email=row.get("google_email"),
            granted_scopes=list(row.get("granted_scopes") or []),
        )

    @classmethod
    async def persist_authorized_connection(
        cls,
        *,
        user_id: str,
        google_subject: str,
        google_email: str | None,
        display_name: str | None,
        refresh_token: str,
        granted_scopes: list[str],
    ) -> dict:
        """Encrypt refresh token and upsert active connection for this user only."""
        enc = GoogleTokenService.encrypt_refresh_token(refresh_token)
        return await connection_store.upsert_active_google_connection(
            user_id=user_id,
            google_subject=google_subject,
            google_email=google_email,
            display_name=display_name,
            encrypted_refresh_token=enc,
            granted_scopes=granted_scopes,
        )

    @classmethod
    async def disconnect(cls, user_id: str) -> bool:
        """Best-effort Google revoke, then always revoke locally.

        Provider/network failures must not block removing LifeSight access.
        Never exposes tokens. Leaves other users' connections untouched.
        """
        from shared.google import oauth as google_oauth

        row = await cls.get_active_connection(user_id)
        if row is None:
            return True

        try:
            refresh = GoogleTokenService.decrypt_refresh_token(
                row["encrypted_refresh_token"]
            )
            await google_oauth.revoke_google_token(refresh)
        except Exception:
            # Outage / bad ciphertext / provider reject — continue locally.
            pass

        await connection_store.revoke_active_google_connection(user_id)
        return True

    @classmethod
    async def load_credentials_for_user(cls, user_id: str):
        """Decrypt + refresh access for the authenticated user only.

        Returns (connection_row, google Credentials).
        """
        row = await cls.get_active_connection(user_id)
        if row is None:
            raise GoogleIntegrationError(GoogleFailureState.not_connected)

        scopes = list(row.get("granted_scopes") or [])
        old_refresh = GoogleTokenService.decrypt_refresh_token(
            row["encrypted_refresh_token"]
        )
        try:
            refreshed = await GoogleTokenService.refresh_access(
                encrypted_refresh_token=row["encrypted_refresh_token"],
                scopes=scopes,
            )
        except GoogleIntegrationError as exc:
            if exc.state in (
                GoogleFailureState.authorization_revoked,
                GoogleFailureState.authorization_expired,
            ):
                await connection_store.revoke_active_google_connection(user_id)
            raise

        await connection_store.touch_google_connection_refresh(str(row["id"]), user_id)
        new_refresh = refreshed.get("refresh_token")
        if new_refresh and new_refresh != old_refresh:
            await connection_store.update_google_connection_refresh_token(
                str(row["id"]),
                user_id,
                GoogleTokenService.encrypt_refresh_token(new_refresh),
            )
            row = await cls.get_active_connection(user_id) or row

        return row, refreshed["credentials"]

    @classmethod
    async def mark_revoked(cls, user_id: str) -> None:
        await connection_store.revoke_active_google_connection(user_id)
