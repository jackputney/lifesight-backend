"""GoogleCalendarProvider — Calendar readonly. No create/update/delete/invite."""

from __future__ import annotations

import asyncio
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from shared.mail_calendar.types import (
    CalendarEvent,
    CalendarEventSummary,
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
    ) -> list[CalendarEventSummary]:
        return await asyncio.to_thread(
            self._list_events_sync, time_min, time_max, max_results, calendar_id
        )

    def _list_events_sync(
        self,
        time_min: str,
        time_max: str,
        max_results: int,
        calendar_id: str,
    ) -> list[CalendarEventSummary]:
        svc = self._service()
        result = (
            svc.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max(1, min(max_results, 100)),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        out: list[CalendarEventSummary] = []
        for item in result.get("items") or []:
            out.append(
                CalendarEventSummary(
                    id=item["id"],
                    calendar_id=calendar_id,
                    summary=item.get("summary"),
                    start=_event_time(item.get("start")),
                    end=_event_time(item.get("end")),
                    status=item.get("status"),
                )
            )
        return out

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
        attendees = item.get("attendees") or []
        return CalendarEvent(
            id=item["id"],
            calendar_id=calendar_id,
            summary=item.get("summary"),
            description=item.get("description"),
            start=_event_time(item.get("start")),
            end=_event_time(item.get("end")),
            status=item.get("status"),
            location=item.get("location"),
            attendees=list(attendees),
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


def _event_time(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    return node.get("dateTime") or node.get("date")
