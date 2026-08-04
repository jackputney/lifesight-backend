"""GoogleMailProvider — Gmail readonly. No send/archive/delete."""

from __future__ import annotations

import base64
from typing import Any, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from shared.mail_calendar.sanitize import plain_text
from shared.mail_calendar.types import MailListOut, MailMessage, MailMessageSummary


class GoogleMailProvider:
    def __init__(self, credentials: Credentials) -> None:
        self._creds = credentials

    def _service(self):
        return build("gmail", "v1", credentials=self._creds, cache_discovery=False)

    async def list_messages(
        self,
        *,
        query: str | None = None,
        max_results: int = 20,
        page_token: str | None = None,
    ) -> MailListOut:
        import asyncio

        return await asyncio.to_thread(
            self._list_messages_sync, query, max_results, page_token
        )

    def _list_messages_sync(
        self,
        query: str | None,
        max_results: int,
        page_token: str | None,
    ) -> MailListOut:
        svc = self._service()
        kwargs: dict[str, Any] = {
            "userId": "me",
            "maxResults": max(1, min(int(max_results), 50)),
        }
        if query:
            kwargs["q"] = query
        if page_token:
            kwargs["pageToken"] = page_token
        listing = svc.users().messages().list(**kwargs).execute()
        out: list[MailMessageSummary] = []
        for item in listing.get("messages") or []:
            mid = item.get("id")
            if not mid:
                continue
            meta = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"].lower(): h["value"]
                for h in (meta.get("payload") or {}).get("headers") or []
                if "name" in h and "value" in h
            }
            internal = meta.get("internalDate")
            out.append(
                MailMessageSummary(
                    id=mid,
                    thread_id=meta.get("threadId") or item.get("threadId"),
                    subject=plain_text(headers.get("subject"), max_len=500),
                    from_address=plain_text(headers.get("from"), max_len=500),
                    snippet=plain_text(meta.get("snippet"), max_len=500),
                    internal_date=str(internal) if internal is not None else None,
                )
            )
        return MailListOut(
            items=out,
            next_page_token=listing.get("nextPageToken"),
        )

    async def get_message(self, message_id: str) -> MailMessage:
        import asyncio

        return await asyncio.to_thread(self._get_message_sync, message_id)

    def _get_message_sync(self, message_id: str) -> MailMessage:
        svc = self._service()
        raw = (
            svc.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        headers = {
            h["name"].lower(): h["value"]
            for h in (raw.get("payload") or {}).get("headers") or []
            if "name" in h and "value" in h
        }
        to_raw = headers.get("to") or ""
        to_addresses = [p.strip() for p in to_raw.split(",") if p.strip()]
        body = _extract_body_text(raw.get("payload") or {})
        return MailMessage(
            id=raw["id"],
            thread_id=raw.get("threadId"),
            subject=plain_text(headers.get("subject"), max_len=500),
            from_address=plain_text(headers.get("from"), max_len=500),
            to_addresses=to_addresses,
            date=headers.get("date"),
            body_text=plain_text(body),
            snippet=plain_text(raw.get("snippet"), max_len=500),
        )


def _extract_body_text(payload: dict[str, Any]) -> Optional[str]:
    """Prefer text/plain; fall back to HTML only as source for sanitization."""
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    data = body.get("data")
    if data and mime.startswith("text/plain"):
        return _b64url_decode(data)
    for part in payload.get("parts") or []:
        text = _extract_body_text(part)
        if text:
            return text
    if data and mime.startswith("text/"):
        return _b64url_decode(data)
    return None


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(padded.encode())
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")
