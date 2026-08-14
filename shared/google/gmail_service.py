"""GmailService — only operations matching granted capabilities.

V1 TestFlight does not request gmail_read/gmail_send by default. Send is
implemented behind gmail_send; read is not faked when scope is absent.
"""

from __future__ import annotations

import asyncio
import base64
from email.mime.text import MIMEText
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from shared.google.capabilities import require_capability
from shared.google.connection_service import GoogleConnectionService
from shared.google.errors import GoogleFailureState, GoogleIntegrationError


class GmailService:
    def __init__(self, credentials: Credentials, *, granted_scopes: list[str]) -> None:
        self._creds = credentials
        self._scopes = granted_scopes

    @classmethod
    async def for_user(cls, user_id: str) -> "GmailService":
        row, creds = await GoogleConnectionService.load_credentials_for_user(user_id)
        return cls(creds, granted_scopes=list(row.get("granted_scopes") or []))

    def _service(self):
        return build("gmail", "v1", credentials=self._creds, cache_discovery=False)

    async def send_email(
        self,
        *,
        to: list[str],
        subject: str,
        body_text: str,
    ) -> dict[str, Any]:
        require_capability(self._scopes, "gmail_send")
        if not to:
            raise ValueError("to is required")
        try:
            return await asyncio.to_thread(
                self._send_sync, to, subject, body_text
            )
        except GoogleIntegrationError:
            raise
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in (401, 403):
                raise GoogleIntegrationError(
                    GoogleFailureState.authorization_revoked,
                    f"Gmail HTTP {status}",
                ) from exc
            raise GoogleIntegrationError(
                GoogleFailureState.provider_unavailable,
                f"Gmail HTTP {status}",
            ) from exc
        except Exception as exc:
            raise GoogleIntegrationError(
                GoogleFailureState.provider_unavailable,
                f"Gmail send failed: {type(exc).__name__}",
            ) from exc

    def _send_sync(self, to: list[str], subject: str, body_text: str) -> dict[str, Any]:
        msg = MIMEText(body_text or "", "plain", "utf-8")
        msg["to"] = ", ".join(to)
        msg["subject"] = subject or ""
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        svc = self._service()
        result = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"id": result.get("id"), "thread_id": result.get("threadId")}

    async def list_messages(self, **_: Any) -> list[dict[str, Any]]:
        """Not available unless gmail_read was granted — never invent inbox data."""
        require_capability(self._scopes, "gmail_read")
        raise GoogleIntegrationError(
            GoogleFailureState.insufficient_scope,
            "Gmail read is prepared in architecture but not enabled for this connection",
        )
