"""GoogleCalendarService — list/create/update/delete events for one user."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from shared.google.capabilities import require_capability
from shared.google.connection_service import GoogleConnectionService
from shared.google.errors import GoogleFailureState, GoogleIntegrationError


def _plain(text: Any, *, max_len: int = 2000) -> str | None:
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    return s[:max_len]


def _event_time(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    return node.get("dateTime") or node.get("date")


def _normalize_event(item: dict, *, calendar_id: str) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "calendar_id": calendar_id,
        "summary": _plain(item.get("summary"), max_len=500),
        "description": _plain(item.get("description")),
        "location": _plain(item.get("location"), max_len=500),
        "start": _event_time(item.get("start")),
        "end": _event_time(item.get("end")),
        "status": item.get("status"),
        "html_link": item.get("htmlLink"),
    }


class GoogleCalendarService:
    def __init__(self, credentials: Credentials, *, granted_scopes: list[str]) -> None:
        require_capability(granted_scopes, "calendar")
        self._creds = credentials
        self._scopes = granted_scopes

    @classmethod
    async def for_user(cls, user_id: str) -> "GoogleCalendarService":
        row, creds = await GoogleConnectionService.load_credentials_for_user(user_id)
        return cls(creds, granted_scopes=list(row.get("granted_scopes") or []))

    def _service(self):
        return build("calendar", "v3", credentials=self._creds, cache_discovery=False)

    async def list_events(
        self,
        *,
        time_min: str,
        time_max: str,
        max_results: int = 20,
        calendar_id: str = "primary",
    ) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(
                self._list_events_sync,
                time_min,
                time_max,
                max_results,
                calendar_id,
            )
        except GoogleIntegrationError:
            raise
        except HttpError as exc:
            raise _map_http_error(exc) from exc
        except Exception as exc:
            raise GoogleIntegrationError(
                GoogleFailureState.provider_unavailable,
                f"Calendar list failed: {type(exc).__name__}",
            ) from exc

    def _list_events_sync(
        self,
        time_min: str,
        time_max: str,
        max_results: int,
        calendar_id: str,
    ) -> list[dict[str, Any]]:
        svc = self._service()
        result = (
            svc.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max(1, min(int(max_results), 50)),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return [
            _normalize_event(item, calendar_id=calendar_id)
            for item in (result.get("items") or [])
        ]

    async def create_event(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str = "primary",
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": summary,
            "start": _time_body(start),
            "end": _time_body(end),
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees if e]

        try:
            return await asyncio.to_thread(self._insert_sync, calendar_id, body)
        except GoogleIntegrationError:
            raise
        except HttpError as exc:
            raise _map_http_error(exc) from exc
        except Exception as exc:
            raise GoogleIntegrationError(
                GoogleFailureState.provider_unavailable,
                f"Calendar create failed: {type(exc).__name__}",
            ) from exc

    def _insert_sync(self, calendar_id: str, body: dict) -> dict[str, Any]:
        svc = self._service()
        item = svc.events().insert(calendarId=calendar_id, body=body).execute()
        return _normalize_event(item, calendar_id=calendar_id)

    async def update_event(
        self,
        *,
        event_id: str,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._patch_sync,
                calendar_id,
                event_id,
                summary,
                start,
                end,
                description,
                location,
            )
        except GoogleIntegrationError:
            raise
        except HttpError as exc:
            raise _map_http_error(exc) from exc
        except Exception as exc:
            raise GoogleIntegrationError(
                GoogleFailureState.provider_unavailable,
                f"Calendar update failed: {type(exc).__name__}",
            ) from exc

    def _patch_sync(
        self,
        calendar_id: str,
        event_id: str,
        summary: str | None,
        start: str | None,
        end: str | None,
        description: str | None,
        location: str | None,
    ) -> dict[str, Any]:
        svc = self._service()
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if start is not None:
            body["start"] = _time_body(start)
        if end is not None:
            body["end"] = _time_body(end)
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        item = (
            svc.events()
            .patch(calendarId=calendar_id, eventId=event_id, body=body)
            .execute()
        )
        return _normalize_event(item, calendar_id=calendar_id)

    async def delete_event(
        self,
        *,
        event_id: str,
        calendar_id: str = "primary",
    ) -> None:
        try:
            await asyncio.to_thread(self._delete_sync, calendar_id, event_id)
        except GoogleIntegrationError:
            raise
        except HttpError as exc:
            raise _map_http_error(exc) from exc
        except Exception as exc:
            raise GoogleIntegrationError(
                GoogleFailureState.provider_unavailable,
                f"Calendar delete failed: {type(exc).__name__}",
            ) from exc

    def _delete_sync(self, calendar_id: str, event_id: str) -> None:
        svc = self._service()
        svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()


def _time_body(value: str) -> dict[str, str]:
    """Accept date (YYYY-MM-DD) or RFC3339 datetime."""
    v = value.strip()
    if "T" in v:
        return {"dateTime": v}
    return {"date": v}


def _map_http_error(exc: HttpError) -> GoogleIntegrationError:
    status = getattr(exc.resp, "status", None)
    if status in (401, 403):
        return GoogleIntegrationError(
            GoogleFailureState.authorization_revoked,
            f"Calendar HTTP {status}",
        )
    return GoogleIntegrationError(
        GoogleFailureState.provider_unavailable,
        f"Calendar HTTP {status}",
    )
