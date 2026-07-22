"""Jarvis tool schemas + async dispatcher (14 tools).

Four local tools (memories + reminders) hit shared/db.py only. Ten Google
tools call shared/google_client.py. The three irreversible Google writes —
create_calendar_event, send_email, reschedule_event — create pending_actions
via db.create_pending_action and never execute until execute_confirmed_action
runs after /confirm (Jack wires that in Phase 3).

KNOWN GAP — reminder firing is deliberately not built yet: create_reminder
stores a row and manage_reminders can list/cancel, but nothing fires a due
reminder (no scheduler, no push channel). See README / JARVIS_PLAN.

dispatch_tool takes user_id first so the route can bind it with
functools.partial into the (name, args) shape shared/agent_loop.py expects.
Optional conversation_id is stored on pending_actions when provided.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from shared import db
from shared import google_client
from shared.spoken_readback import build_readback

from .prompt import MODE_NAME

# Pending confirmations expire if the user never answers (missed STT "yes").
_PENDING_TTL = timedelta(minutes=10)

TOOL_SCHEMAS = [
    {
        "name": "get_todays_events",
        "description": "Get the user's Google Calendar events for today.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_events",
        "description": (
            "Search upcoming Google Calendar events by title, attendee, or time "
            "(e.g. 'Jack', '2pm', 'Friday'). Returns event id, title, and times."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for."},
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days ahead to search. Default 7.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_recent_emails",
        "description": "List recent inbox emails for an overview.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Max emails. Default 10.",
                },
                "unread_only": {
                    "type": "boolean",
                    "description": "Only unread. Default false.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_email",
        "description": "Read the full body of one email by its message id.",
        "input_schema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    },
    {
        "name": "recall_memories",
        "description": "Search stored long-term memories about the user.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "create_email_draft",
        "description": "Create a DRAFT email in Gmail. This never sends the email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "reply_to_message_id": {
                    "type": "string",
                    "description": "Optional: message id this is a reply to.",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": (
            "Propose creating a Google Calendar event. This does NOT create the "
            "event; the app reads it back and waits for spoken yes or no."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {"type": "string", "description": "ISO 8601 start time."},
                "end_iso": {"type": "string", "description": "ISO 8601 end time."},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
            },
            "required": ["title", "start_iso", "end_iso"],
        },
    },
    {
        "name": "save_memory",
        "description": "Store a durable fact about the user for later recall.",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Propose sending an existing Gmail draft by draft_id. This does NOT "
            "send; the app reads it back and waits for spoken yes or no."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "Gmail draft id to send.",
                },
            },
            "required": ["draft_id"],
        },
    },
    {
        "name": "get_availability",
        "description": (
            "Check free/busy on the user's primary calendar for a time range. "
            "Returns readable free windows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_iso": {"type": "string", "description": "ISO 8601 range start."},
                "end_iso": {"type": "string", "description": "ISO 8601 range end."},
            },
            "required": ["start_iso", "end_iso"],
        },
    },
    {
        "name": "reschedule_event",
        "description": (
            "Propose moving an existing calendar event to new times. This does NOT "
            "reschedule; the app reads it back and waits for spoken yes or no. "
            "Find event_id via get_todays_events or search_events first — never guess an id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "new_start_iso": {
                    "type": "string",
                    "description": "ISO 8601 new start.",
                },
                "new_end_iso": {
                    "type": "string",
                    "description": "ISO 8601 new end.",
                },
            },
            "required": ["event_id", "new_start_iso", "new_end_iso"],
        },
    },
    {
        "name": "lookup_contact",
        "description": (
            "Search Google Contacts by name. Returns name + email candidates. "
            "If multiple matches, ask the user which one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "create_reminder",
        "description": (
            "Schedule a reminder. For relative times ('in 10 minutes'), pass "
            "minutes_from_now — the server computes the fire time. For absolute "
            "times ('Friday 9am'), pass fire_at_iso. Optional condition for "
            "no-reply chasers. Confirm when you will remind them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What to remind the user about.",
                },
                "minutes_from_now": {
                    "type": "integer",
                    "description": (
                        "Fire this many minutes from now. Use for 'in 10 minutes', "
                        "'in 2 hours' (pass 120), etc. Preferred over fire_at_iso "
                        "for relative reminders."
                    ),
                },
                "fire_at_iso": {
                    "type": "string",
                    "description": (
                        "Future ISO 8601 datetime with timezone offset. Use for "
                        "named calendar times, not for 'in N minutes'."
                    ),
                },
                "condition": {
                    "type": "object",
                    "description": (
                        "Optional. For no-reply chasers: "
                        "{type: 'no_reply', thread_id?, from_email?, since_iso}."
                    ),
                },
            },
            "required": ["description"],
        },
    },
    {
        "name": "manage_reminders",
        "description": "List pending reminders or cancel one by id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "cancel"]},
                "reminder_id": {
                    "type": "string",
                    "description": "Reminder id (UUID). Required when action is cancel.",
                },
            },
            "required": ["action"],
        },
    },
]

READ_ONLY = {
    "get_todays_events",
    "search_events",
    "list_recent_emails",
    "read_email",
    "recall_memories",
    "get_availability",
    "lookup_contact",
    "manage_reminders",
}

_PENDING_MSG = {
    "create_calendar_event": "The calendar event has NOT been created.",
    "send_email": "The email has NOT been sent.",
    "reschedule_event": "The event has NOT been rescheduled.",
}

_IRREVERSIBLE = set(_PENDING_MSG)


async def dispatch_tool(
    user_id: str,
    name: str,
    args: dict,
    *,
    conversation_id: str | None = None,
) -> tuple[str, dict | None]:
    """Run one tool call. Returns (result_text_for_model, pending_action_or_None).

    pending_action (when set) matches the API slot shape plus fields the
    Confirm Gate executor needs: {action_id, description, action_type, payload}.
    """
    if name == "get_todays_events":
        result = await google_client.get_todays_events(user_id)
    elif name == "search_events":
        result = await google_client.search_events(
            user_id, args["query"], args.get("days_ahead", 7)
        )
    elif name == "list_recent_emails":
        result = await google_client.list_recent_emails(
            user_id,
            args.get("max_results", 10),
            args.get("unread_only", False),
        )
    elif name == "read_email":
        result = await google_client.read_email(user_id, args["message_id"])
    elif name == "recall_memories":
        rows = await db.recall_memories(user_id, args["query"])
        result = [
            {
                "id": str(r["id"]),
                "content": r["content"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    elif name == "create_email_draft":
        result = await google_client.create_email_draft(
            user_id,
            args["to"],
            args["subject"],
            args["body"],
            args.get("reply_to_message_id"),
        )
    elif name == "save_memory":
        mem_id = await db.save_memory(user_id, args["content"])
        result = {"saved": True, "id": mem_id}
    elif name == "create_calendar_event":
        return await _propose_calendar_event(user_id, args, conversation_id)
    elif name == "send_email":
        return await _propose_send_email(user_id, args, conversation_id)
    elif name == "get_availability":
        result = await google_client.get_availability(
            user_id, args["start_iso"], args["end_iso"]
        )
    elif name == "reschedule_event":
        return await _propose_reschedule_event(user_id, args, conversation_id)
    elif name == "lookup_contact":
        result = await google_client.lookup_contact(user_id, args["name"])
    elif name == "create_reminder":
        result = await _create_reminder(user_id, args)
    elif name == "manage_reminders":
        result = await _manage_reminders(user_id, args)
    else:
        raise ValueError(f"Unknown tool: {name}")

    await db.log_action(
        user_id,
        MODE_NAME,
        name,
        args,
        _summarize(name, result),
        confirmed=name in READ_ONLY,
    )
    return json.dumps(result), None


def _pending_result(tool_name: str, action_id: str) -> str:
    return json.dumps(
        {
            "status": "pending_confirmation",
            "pending_action_id": action_id,
            "message": (
                f"{_PENDING_MSG[tool_name]} The app will read back the details aloud "
                "and wait for the user's spoken yes or no. Reply with one short sentence "
                "telling them to listen and say yes or no."
            ),
        }
    )


async def _store_pending(
    user_id: str,
    tool_name: str,
    args: dict,
    description: str,
    conversation_id: str | None,
) -> tuple[str, dict]:
    action_id = await db.create_pending_action(
        user_id=user_id,
        conversation_id=conversation_id,
        source_mode=MODE_NAME,
        action_type=tool_name,
        payload=args,
        description=description,
        expires_at=datetime.now(timezone.utc) + _PENDING_TTL,
    )
    await db.log_action(
        user_id,
        MODE_NAME,
        tool_name,
        args,
        "proposed, awaiting confirmation",
        confirmed=False,
    )
    pending = {
        "action_id": action_id,
        "description": description,
        "action_type": tool_name,
        "payload": args,
    }
    return _pending_result(tool_name, action_id), pending


async def _propose_calendar_event(
    user_id: str, args: dict, conversation_id: str | None
) -> tuple[str, dict]:
    description = build_readback("create_calendar_event", args)
    return await _store_pending(
        user_id, "create_calendar_event", args, description, conversation_id
    )


async def _propose_send_email(
    user_id: str, args: dict, conversation_id: str | None
) -> tuple[str, dict]:
    preview = await google_client.get_draft_preview(user_id, args["draft_id"])
    full_args = {**args, **preview}
    description = build_readback("send_email", full_args)
    return await _store_pending(
        user_id, "send_email", full_args, description, conversation_id
    )


async def _propose_reschedule_event(
    user_id: str, args: dict, conversation_id: str | None
) -> tuple[str, dict]:
    event = await google_client.get_event(user_id, args["event_id"])
    full_args = {
        **args,
        "title": event["title"],
        "old_start": event["start"],
        "old_end": event["end"],
    }
    description = build_readback("reschedule_event", full_args)
    return await _store_pending(
        user_id, "reschedule_event", full_args, description, conversation_id
    )


async def execute_confirmed_action(
    user_id: str, pending: dict, via: str = "click"
) -> str:
    """Execute a confirmed pending action. Called from /confirm after approval.

    `pending` must include action_type and payload (the pending_actions columns).
    Phase 3 wiring must load those fields from the DB — see
    docs/JARVIS_PHASE3_PROPOSAL.md. Never call this without a confirmed row.
    """
    args = pending.get("payload")
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict):
        raise ValueError("pending action missing payload")
    tool = pending.get("action_type") or pending.get("tool_name")
    if tool not in _IRREVERSIBLE:
        raise ValueError(f"Cannot execute pending tool: {tool}")

    if tool == "create_calendar_event":
        event = await google_client.create_calendar_event(
            user_id,
            title=args["title"],
            start_iso=args["start_iso"],
            end_iso=args["end_iso"],
            attendees=args.get("attendees"),
            description=args.get("description", ""),
        )
        summary = f"created event {event.get('id')} — {event.get('title')}"
        spoken = (
            f"Done. I created {event.get('title')} "
            f"starting {event.get('start')}."
        )
    elif tool == "send_email":
        sent = await google_client.send_draft(user_id, args["draft_id"])
        summary = (
            f"sent email to {args.get('to', '')} — message {sent.get('message_id')}"
        )
        spoken = f"Done. I sent the email to {args.get('to', 'the recipient')}."
    elif tool == "reschedule_event":
        event = await google_client.reschedule_event(
            user_id,
            args["event_id"],
            args["new_start_iso"],
            args["new_end_iso"],
        )
        summary = f"rescheduled {event.get('title')} to {event.get('start')}"
        spoken = (
            f"Done. I rescheduled {event.get('title')} to {event.get('start')}."
        )
    else:
        raise ValueError(f"Cannot execute pending tool: {tool}")

    await db.log_action(
        user_id,
        MODE_NAME,
        tool,
        args,
        f"{summary} (confirmed via {via})",
        confirmed=True,
    )
    return spoken


def _resolve_fire_at(args: dict) -> datetime:
    """UTC fire time from minutes_from_now or fire_at_iso; must be in the future."""
    minutes = args.get("minutes_from_now")
    if minutes is not None:
        if minutes <= 0:
            raise ValueError("minutes_from_now must be a positive integer")
        return datetime.now(timezone.utc) + timedelta(minutes=minutes)
    iso = args.get("fire_at_iso")
    if not iso:
        raise ValueError("provide minutes_from_now or fire_at_iso")
    fire_at = datetime.fromisoformat(iso).astimezone(timezone.utc)
    if fire_at <= datetime.now(timezone.utc):
        raise ValueError(
            f"fire_at_iso {iso} is in the past. "
            "For relative times use minutes_from_now instead."
        )
    return fire_at


async def _create_reminder(user_id: str, args: dict) -> dict:
    condition = args.get("condition")
    if condition and condition.get("type") == "no_reply":
        if not condition.get("thread_id") and not condition.get("from_email"):
            raise ValueError("no_reply condition needs thread_id or from_email")
        if not condition.get("since_iso"):
            raise ValueError("no_reply condition needs since_iso")
    fire_at = _resolve_fire_at(args)
    rid = await db.create_reminder(user_id, args["description"], fire_at, condition)
    return {
        "id": rid,
        "description": args["description"],
        "fire_at": fire_at.isoformat(),
        "fire_at_local": fire_at.astimezone().isoformat(),
        "condition": condition,
        "status": "pending",
    }


async def _manage_reminders(user_id: str, args: dict) -> dict | list:
    action = args["action"]
    if action == "list":
        return await db.list_reminders(user_id, "pending")
    if action == "cancel":
        rid = args.get("reminder_id")
        if not rid:
            raise ValueError("reminder_id required for cancel")
        reminder = await db.get_reminder(user_id, rid)
        if not reminder:
            raise ValueError(f"Reminder {rid} not found")
        if reminder["status"] != "pending":
            raise ValueError(f"Reminder {rid} is already {reminder['status']}")
        await db.set_reminder_status(user_id, rid, "cancelled")
        return {"cancelled": True, "id": rid}
    raise ValueError(f"Unknown manage_reminders action: {action}")


def _summarize(name: str, result) -> str:
    if name == "create_reminder" and isinstance(result, dict):
        return f"create_reminder: id {result.get('id')} at {result.get('fire_at')}"
    if name == "manage_reminders":
        if isinstance(result, list):
            return f"manage_reminders: {len(result)} pending"
        if isinstance(result, dict) and result.get("cancelled"):
            return f"manage_reminders: cancelled {result.get('id')}"
    if isinstance(result, list):
        return f"{name}: {len(result)} item(s)"
    if isinstance(result, dict):
        if name == "get_availability" and "free_windows" in result:
            return f"{name}: {len(result['free_windows'])} free window(s)"
        return f"{name}: {result.get('id') or result.get('saved')}"
    return f"{name}: ok"
