"""Jarvis tool schemas + async dispatcher — the four tools that need no Google.

recall_memories, save_memory, create_reminder, and manage_reminders run
against shared/db.py only. All four execute immediately (no Confirm Gate:
memories and reminder rows are cheap to undo), so dispatch_tool always
returns None for the pending-action slot; the Google-backed write tools that
DO create pending actions land in a later phase.

KNOWN GAP — reminder firing is deliberately not built yet: create_reminder
stores a row in the reminders table and manage_reminders can list or cancel
it, but nothing ever fires a due reminder because there is no scheduler and
no push-notification channel to deliver one to. See README "What's NOT here
yet". Wiring a scheduler before delivery exists would fire into the void.

dispatch_tool takes user_id first so the route can bind it with
functools.partial into the (name, args) dispatch shape shared/agent_loop.py
expects.
"""
import json
from datetime import datetime, timedelta, timezone

from shared import db

from .prompt import MODE_NAME

TOOL_SCHEMAS = [
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
        "name": "save_memory",
        "description": "Store a durable fact about the user for later recall.",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
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

READ_ONLY = {"recall_memories", "manage_reminders"}


async def dispatch_tool(user_id: str, name: str, args: dict) -> tuple[str, dict | None]:
    """Run one tool call for this user. Returns (result_text_for_model, None).

    The second element is the pending-action slot agent_loop expects; these
    four tools never propose Confirm Gate actions, so it is always None.
    """
    if name == "recall_memories":
        rows = await db.recall_memories(user_id, args["query"])
        result = [
            {
                "id": str(r["id"]),
                "content": r["content"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    elif name == "save_memory":
        mem_id = await db.save_memory(user_id, args["content"])
        result = {"saved": True, "id": mem_id}
    elif name == "create_reminder":
        result = await _create_reminder(user_id, args)
    elif name == "manage_reminders":
        result = await _manage_reminders(user_id, args)
    else:
        raise ValueError(f"Unknown tool: {name}")

    await db.log_action(
        user_id, MODE_NAME, name, args, _summarize(name, result),
        confirmed=name in READ_ONLY,
    )
    return json.dumps(result), None


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
    # Naive datetimes are treated as server-local time, matching the local
    # clock the system prompt shows the model.
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
        return f"{name}: {result.get('id') or result.get('saved')}"
    return f"{name}: ok"
