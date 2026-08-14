"""Mail & Calendar Claude tools — reads execute; writes stage Confirm Gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from shared.google.calendar_service import GoogleCalendarService
from shared.google.errors import GoogleIntegrationError
from shared.google.gmail_service import GmailService

TOOLS: list[dict] = [
    {
        "name": "list_calendar_events",
        "description": (
            "List the user's Google Calendar events for a time range. "
            "Use for upcoming events or a requested date/range. Reads execute "
            "immediately (no Confirm Gate)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time_min": {
                    "type": "string",
                    "description": "RFC3339 or YYYY-MM-DD start (inclusive).",
                },
                "time_max": {
                    "type": "string",
                    "description": "RFC3339 or YYYY-MM-DD end (exclusive preferred).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max events to return (1-50). Default 20.",
                },
            },
            "required": ["time_min", "time_max"],
        },
    },
    {
        "name": "create_pending_action",
        "description": (
            "Stage an irreversible Google Calendar or Gmail change for Confirm "
            "Gate approval. Required for create/update/delete event and send "
            "email. description is read aloud. Include a full payload."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": (
                        "Spoken sentence: what will happen, when, and recipients "
                        "if any (e.g. 'Schedule dinner tomorrow at 7 PM on your "
                        "Google Calendar.')."
                    ),
                },
                "action_type": {
                    "type": "string",
                    "enum": [
                        "create_calendar_event",
                        "update_calendar_event",
                        "delete_calendar_event",
                        "send_email",
                    ],
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "create_calendar_event: summary, start, end, "
                        "description?, location?, attendees?[]; "
                        "update_calendar_event: event_id, summary?, start?, "
                        "end?, description?, location?; "
                        "delete_calendar_event: event_id, summary?; "
                        "send_email: to[], subject, body_text."
                    ),
                },
            },
            "required": ["description", "action_type", "payload"],
        },
    },
]


def _as_rfc3339_start(value: str) -> str:
    v = value.strip()
    if "T" in v:
        return v if v.endswith("Z") or "+" in v[10:] or v.count("-") > 2 else v
    # Date-only → start of day UTC
    return f"{v}T00:00:00Z"


def _as_rfc3339_end(value: str) -> str:
    v = value.strip()
    if "T" in v:
        return v
    # Date-only end → next day start (exclusive-style upper bound)
    day = datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (day + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")


async def run_list_calendar_events(user_id: str, tool_input: dict) -> str:
    time_min = str(tool_input.get("time_min") or "").strip()
    time_max = str(tool_input.get("time_max") or "").strip()
    if not time_min or not time_max:
        return "Error: time_min and time_max are required."
    max_results = tool_input.get("max_results") or 20
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 20

    try:
        svc = await GoogleCalendarService.for_user(user_id)
        events = await svc.list_events(
            time_min=_as_rfc3339_start(time_min),
            time_max=_as_rfc3339_end(time_max),
            max_results=max_results,
        )
    except GoogleIntegrationError as exc:
        return f"Error [{exc.state.value}]: {exc.spoken()}"
    except Exception:
        return (
            "Error [provider_unavailable]: Google Calendar is temporarily "
            "unavailable. Try again shortly."
        )

    if not events:
        return "No calendar events found in that range."

    lines = []
    for ev in events:
        lines.append(
            f"- {ev.get('summary') or '(no title)'} | {ev.get('start')} → "
            f"{ev.get('end')} | id={ev.get('id')}"
        )
    return "Calendar events:\n" + "\n".join(lines)


async def execute_create_calendar_event(user_id: str, payload: dict) -> str:
    summary = str(payload.get("summary") or "").strip()
    start = str(payload.get("start") or "").strip()
    end = str(payload.get("end") or "").strip()
    if not summary or not start or not end:
        return "Could not create the event — summary, start, and end are required."
    svc = await GoogleCalendarService.for_user(user_id)
    ev = await svc.create_event(
        summary=summary,
        start=start,
        end=end,
        description=payload.get("description"),
        location=payload.get("location"),
        attendees=list(payload.get("attendees") or []) or None,
    )
    return (
        f"Created calendar event '{ev.get('summary')}' "
        f"from {ev.get('start')} to {ev.get('end')}."
    )


async def execute_update_calendar_event(user_id: str, payload: dict) -> str:
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        return "Could not update the event — event_id is required."
    svc = await GoogleCalendarService.for_user(user_id)
    ev = await svc.update_event(
        event_id=event_id,
        summary=payload.get("summary"),
        start=payload.get("start"),
        end=payload.get("end"),
        description=payload.get("description"),
        location=payload.get("location"),
    )
    return f"Updated calendar event '{ev.get('summary') or event_id}'."


async def execute_delete_calendar_event(user_id: str, payload: dict) -> str:
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        return "Could not delete the event — event_id is required."
    label = str(payload.get("summary") or event_id).strip()
    svc = await GoogleCalendarService.for_user(user_id)
    await svc.delete_event(event_id=event_id)
    return f"Deleted calendar event '{label}'."


async def execute_send_email(user_id: str, payload: dict) -> str:
    to_raw = payload.get("to") or []
    if isinstance(to_raw, str):
        to_list = [to_raw]
    else:
        to_list = [str(x).strip() for x in to_raw if str(x).strip()]
    subject = str(payload.get("subject") or "").strip()
    body_text = str(payload.get("body_text") or "")
    if not to_list:
        return "Could not send email — recipients are required."
    svc = await GmailService.for_user(user_id)
    result = await svc.send_email(to=to_list, subject=subject, body_text=body_text)
    return (
        f"Sent email to {', '.join(to_list)} with subject '{subject}' "
        f"(id={result.get('id')})."
    )


async def execute_mail_calendar_action(
    action_type: str, user_id: str, payload: dict[str, Any]
) -> str:
    try:
        if action_type == "create_calendar_event":
            return await execute_create_calendar_event(user_id, payload)
        if action_type == "update_calendar_event":
            return await execute_update_calendar_event(user_id, payload)
        if action_type == "delete_calendar_event":
            return await execute_delete_calendar_event(user_id, payload)
        if action_type == "send_email":
            return await execute_send_email(user_id, payload)
    except GoogleIntegrationError as exc:
        return f"Failed [{exc.state.value}]: {exc.spoken()}"
    except Exception:
        return "Failed [provider_unavailable]: Google could not complete that action."
    return f"Confirmed: {action_type}"
