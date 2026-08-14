"""GoogleTokenService — decrypt refresh credential and obtain access tokens.

Claude / HTTP responses never see tokens. Callers use Credentials objects
in-process only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from shared import crypto
from shared.google.errors import GoogleFailureState, GoogleIntegrationError
from shared.google.oauth import GOOGLE_TOKEN_URI, require_oauth_config


class GoogleTokenService:
    @staticmethod
    def decrypt_refresh_token(encrypted_refresh_token: str) -> str:
        try:
            return crypto.decrypt(encrypted_refresh_token)
        except ValueError as exc:
            raise GoogleIntegrationError(
                GoogleFailureState.authorization_expired,
                "Stored Google credential could not be decrypted",
            ) from exc

    @staticmethod
    def encrypt_refresh_token(refresh_token: str) -> str:
        return crypto.encrypt(refresh_token)

    @classmethod
    def credentials_from_refresh(
        cls,
        *,
        refresh_token: str,
        scopes: list[str],
        access_token: str | None = None,
    ) -> Credentials:
        cfg = require_oauth_config()
        return Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            scopes=scopes,
        )

    @classmethod
    async def refresh_access(
        cls,
        *,
        encrypted_refresh_token: str,
        scopes: list[str],
    ) -> dict[str, Any]:
        """Return access token material. Raises GoogleIntegrationError on revoke."""
        refresh = cls.decrypt_refresh_token(encrypted_refresh_token)
        creds = cls.credentials_from_refresh(refresh_token=refresh, scopes=scopes)

        def _refresh() -> Credentials:
            creds.refresh(Request())
            return creds

        try:
            refreshed = await asyncio.to_thread(_refresh)
        except RefreshError as exc:
            msg = str(exc).lower()
            if "revoked" in msg or "invalid_grant" in msg:
                raise GoogleIntegrationError(
                    GoogleFailureState.authorization_revoked,
                    "Google refresh token rejected",
                ) from exc
            raise GoogleIntegrationError(
                GoogleFailureState.authorization_expired,
                "Google refresh failed",
            ) from exc
        except Exception as exc:
            raise GoogleIntegrationError(
                GoogleFailureState.provider_unavailable,
                f"Google token refresh unavailable: {type(exc).__name__}",
            ) from exc

        if not refreshed.token:
            raise GoogleIntegrationError(
                GoogleFailureState.authorization_expired,
                "Google refresh returned no access token",
            )

        expiry: Optional[datetime] = refreshed.expiry
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        return {
            "access_token": refreshed.token,
            "refresh_token": refreshed.refresh_token or refresh,
            "scopes": list(refreshed.scopes or scopes),
            "expiry": expiry,
            "credentials": refreshed,
        }
