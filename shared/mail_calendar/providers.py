"""MailProvider / CalendarProvider protocols — Google first; Outlook later."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.mail_calendar.types import (
    CalendarEvent,
    EventListOut,
    FreeBusyOut,
    MailListOut,
    MailMessage,
)


@runtime_checkable
class MailProvider(Protocol):
    async def list_messages(
        self,
        *,
        query: str | None = None,
        max_results: int = 20,
        page_token: str | None = None,
    ) -> MailListOut:
        ...

    async def get_message(self, message_id: str) -> MailMessage:
        ...


@runtime_checkable
class CalendarProvider(Protocol):
    async def list_events(
        self,
        *,
        time_min: str,
        time_max: str,
        max_results: int = 50,
        calendar_id: str = "primary",
        page_token: str | None = None,
    ) -> EventListOut:
        ...

    async def get_event(
        self,
        event_id: str,
        *,
        calendar_id: str = "primary",
    ) -> CalendarEvent:
        ...

    async def freebusy(
        self,
        *,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
    ) -> FreeBusyOut:
        ...
