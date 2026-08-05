"""GoogleCalendarProvider — Calendar readonly. No create/update/delete/invite."""

from __future__ import annotations

import asyncio
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from shared.mail_calendar.sanitize import plain_text
from shared.mail_calendar.types import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventSummary,
    EventListOut,
    FreeBusyOut,
    FreeBusySlot,
)


class GoogleCalendarProvider:
    def __init__(self, credentials: Credentials) -> None:
        self._creds = credentials

    def _service(self):
        return build("calendar", "v3", credentials=self._creds, cache_discovery=False)

    async def list_events(
        self,
        *,
        time_min: str,
        time_max: str,
        max_results: int = 50,
        calendar_id: str = "primary",
        page_token: str | None = None,
    ) -> EventListOut:
        return await asyncio.to_thread(
            self._list_events_sync,
            time_min,
            time_max,
            max_results,
            calendar_id,
            page_token,
        )

    def _list_events_sync(
        self,
        time_min: str,
        time_max: str,
        max_results: int,
        calendar_id: str,
        page_token: str | None,
    ) -> EventListOut:
        svc = self._service()
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": max(1, min(int(max_results), 100)),
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = svc.events().list(**kwargs).execute()
        out: list[CalendarEventSummary] = []
        for item in result.get("items") or []:
            out.append(
                CalendarEventSummary(
                    id=item["id"],
                    calendar_id=calendar_id,
                    summary=plain_text(item.get("summary"), max_len=500),
                    start=_event_time(item.get("start")),
                    end=_event_time(item.get("end")),
                    status=item.get("status"),
                )
            )
        return EventListOut(
            items=out,
            next_page_token=result.get("nextPageToken"),
        )

    async def get_event(
        self,
        event_id: str,
        *,
        calendar_id: str = "primary",
    ) -> CalendarEvent:
        return await asyncio.to_thread(self._get_event_sync, event_id, calendar_id)

    def _get_event_sync(self, event_id: str, calendar_id: str) -> CalendarEvent:
        svc = self._service()
        item = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
        return CalendarEvent(
            id=item["id"],
            calendar_id=calendar_id,
            summary=plain_text(item.get("summary"), max_len=500),
            description=plain_text(item.get("description")),
            start=_event_time(item.get("start")),
            end=_event_time(item.get("end")),
            status=item.get("status"),
            location=plain_text(item.get("location"), max_len=500),
            attendees=_normalize_attendees(item.get("attendees") or []),
        )

    async def freebusy(
        self,
        *,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
    ) -> FreeBusyOut:
        return await asyncio.to_thread(
            self._freebusy_sync, time_min, time_max, calendar_id
        )

    def _freebusy_sync(
        self, time_min: str, time_max: str, calendar_id: str
    ) -> FreeBusyOut:
        svc = self._service()
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": calendar_id}],
        }
        result = svc.freebusy().query(body=body).execute()
        cal = (result.get("calendars") or {}).get(calendar_id) or {}
        busy = [
            FreeBusySlot(start=b["start"], end=b["end"])
            for b in (cal.get("busy") or [])
            if b.get("start") and b.get("end")
        ]
        return FreeBusyOut(
            calendar_id=calendar_id,
            busy=busy,
            time_min=time_min,
            time_max=time_max,
        )


def _normalize_attendees(raw: list[Any]) -> list[CalendarAttendee]:
    out: list[CalendarAttendee] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            CalendarAttendee(
                email=plain_text(item.get("email"), max_len=320),
                display_name=plain_text(item.get("displayName"), max_len=200),
                response_status=item.get("responseStatus"),
                optional=bool(item.get("optional") or False),
            )
        )
    return out


def _event_time(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    return node.get("dateTime") or node.get("date")
