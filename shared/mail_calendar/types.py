"""Public Mail & Calendar types (no tokens on the wire)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ConnectionStatus(str, Enum):
    disconnected = "disconnected"
    connected_read = "connected_read"
    reauth_required = "reauth_required"
    error = "error"


class MailCalendarStatusOut(BaseModel):
    status: ConnectionStatus
    provider: str = "google"
    scopes: list[str] = Field(default_factory=list)
    detail: str | None = None


class ConnectOut(BaseModel):
    authorization_url: str
    # Opaque state for debugging only — browser round-trips it via Google.
    # Not a secret the client must store; server validates HMAC on callback.
    state: str


class DisconnectOut(BaseModel):
    status: ConnectionStatus = ConnectionStatus.disconnected
    detail: str = "Disconnected."


class MailMessageSummary(BaseModel):
    id: str
    thread_id: str | None = None
    subject: str | None = None
    from_address: str | None = None
    snippet: str | None = None
    internal_date: str | None = None


class MailMessage(BaseModel):
    id: str
    thread_id: str | None = None
    subject: str | None = None
    from_address: str | None = None
    to_addresses: list[str] = Field(default_factory=list)
    date: str | None = None
    body_text: str | None = None
    snippet: str | None = None


class CalendarEventSummary(BaseModel):
    id: str
    calendar_id: str = "primary"
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    status: str | None = None


class CalendarEvent(BaseModel):
    id: str
    calendar_id: str = "primary"
    summary: str | None = None
    description: str | None = None
    start: str | None = None
    end: str | None = None
    status: str | None = None
    location: str | None = None
    attendees: list[dict[str, Any]] = Field(default_factory=list)


class FreeBusySlot(BaseModel):
    start: str
    end: str


class FreeBusyOut(BaseModel):
    calendar_id: str = "primary"
    busy: list[FreeBusySlot] = Field(default_factory=list)
    time_min: str
    time_max: str
