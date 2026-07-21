"""Spoken read-back text built from pending action args — what is spoken is what executes."""
import re
from datetime import datetime


def _friendly_email(addr: str) -> str:
    if not addr or "@" not in addr:
        return addr or "the recipient"
    local = addr.split("@", 1)[0]
    name = re.sub(r"[._]+", " ", local).strip()
    return " ".join(w.capitalize() for w in name.split()) + f" at {addr}"


def _parse_iso(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _speakable_when(iso: str) -> str:
    dt = _parse_iso(iso)
    if not dt:
        return iso
    day = f"{dt.strftime('%A')} {dt.strftime('%B')} {dt.day}"
    if dt.hour == 0 and dt.minute == 0:
        time_part = "midnight"
    elif dt.hour == 12 and dt.minute == 0:
        time_part = "noon"
    else:
        h = dt.hour % 12 or 12
        m = dt.minute
        ampm = "AM" if dt.hour < 12 else "PM"
        time_part = f"{h}" if m == 0 else f"{h}:{m:02d}"
        time_part += f" {ampm}"
    return f"{day} at {time_part}"


def _body_gist(body: str, max_words: int = 60) -> str:
    text = re.sub(r"\s+", " ", (body or "").strip())
    if not text:
        return "no body text"
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:20]) + "."


def build_readback(tool_name: str, args: dict) -> str:
    """Plain-text read-back for TTS from tool args only."""
    if tool_name == "send_email":
        to = _friendly_email(args.get("to", ""))
        subject = args.get("subject") or "no subject"
        body = args.get("body") or args.get("body_preview") or ""
        gist = _body_gist(body)
        return (
            f"I'm about to send an email to {to}. "
            f"Subject: {subject}. It says: {gist}. Should I send it?"
        )
    if tool_name == "create_calendar_event":
        title = args.get("title") or "Untitled event"
        start = _speakable_when(args.get("start_iso", ""))
        end = _speakable_when(args.get("end_iso", ""))
        who = args.get("attendees") or []
        att = ""
        if who:
            names = [_friendly_email(a) if "@" in str(a) else str(a) for a in who]
            att = f" With {', '.join(names)}."
        return f"I'm about to create a calendar event: {title}. {start} to {end}.{att} Should I create it?"
    if tool_name == "reschedule_event":
        title = args.get("title") or "your event"
        old = _speakable_when(args.get("old_start", ""))
        new = _speakable_when(args.get("new_start_iso", ""))
        new_end = _speakable_when(args.get("new_end_iso", ""))
        return (
            f"I'm about to reschedule {title}. It was {old}. "
            f"The new time is {new} to {new_end}. Should I reschedule it?"
        )
    desc = args.get("description") or "this action"
    return f"I'm about to {desc}. Should I go ahead?"
