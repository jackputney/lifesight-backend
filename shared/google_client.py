"""Per-user Google Calendar / Gmail / People client.

Adapted from Oliver_Jarvis_V2/app/google_client.py: credentials live in
oauth_credentials (Fernet-encrypted), keyed by (user_id, provider), instead of
a single token.json. OAuth is the web flow in main.py — this module builds the
authorization URL, exchanges the code, and loads/refreshes tokens for API calls.

Public Calendar/Gmail/People helpers take user_id and are async (DB + refresh).
"""
from __future__ import annotations

import base64
import functools
import html as html_lib
import os
import re
import uuid
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from typing import Optional

from googleapiclient.errors import HttpError

from shared import db
from shared.crypto import (
    decrypt_token,
    encrypt_token,
    sign_oauth_state,
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/contacts.other.readonly",
]

PROVIDER = "google"

# (user_id, api, version) → built discovery service
_services: dict[tuple[str, str, str], object] = {}


class GoogleClientError(Exception):
    """Readable error surfaced to the agent instead of a raw traceback."""


# ---------------------------------------------------------------------------
# Env / OAuth web-flow helpers
# ---------------------------------------------------------------------------

def _require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise GoogleClientError(
            f"Missing Google OAuth env var(s): {', '.join(missing)}"
        )
    return {n: os.environ[n] for n in names}


def _make_flow(state: Optional[str] = None):
    from google_auth_oauthlib.flow import Flow

    env = _require_env(
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"
    )
    client_config = {
        "web": {
            "client_id": env["GOOGLE_CLIENT_ID"],
            "client_secret": env["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [env["GOOGLE_REDIRECT_URI"]],
        }
    }
    flow = Flow.from_client_config(
        client_config, scopes=SCOPES, state=state
    )
    flow.redirect_uri = env["GOOGLE_REDIRECT_URI"]
    return flow


def build_authorization_url(user_id: str) -> str:
    """Return Google's consent URL with a signed state carrying user_id."""
    state = sign_oauth_state(user_id)
    flow = _make_flow(state=state)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return auth_url


def exchange_code(code: str):
    """Exchange an authorization code for Credentials (sync)."""
    flow = _make_flow()
    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        raise GoogleClientError(f"Google token exchange failed: {exc}") from exc
    return flow.credentials


# ---------------------------------------------------------------------------
# Credential load / save / refresh (per user)
# ---------------------------------------------------------------------------

async def save_credentials(user_id: str, creds) -> None:
    """Encrypt and upsert tokens for this user. Keeps prior refresh_token if
    Google omitted a new one on re-consent."""
    access = creds.token
    if not access:
        raise GoogleClientError("Google returned no access token")

    refresh = creds.refresh_token
    if not refresh:
        existing = await db.get_oauth_credentials(user_id, PROVIDER)
        if existing and existing.get("refresh_token_enc"):
            refresh = decrypt_token(existing["refresh_token_enc"])

    expires_at = creds.expiry
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    scopes = list(creds.scopes) if creds.scopes else list(SCOPES)
    await db.upsert_oauth_credentials(
        user_id=user_id,
        provider=PROVIDER,
        access_token_enc=encrypt_token(access),
        refresh_token_enc=encrypt_token(refresh) if refresh else None,
        scopes=scopes,
        expires_at=expires_at,
    )
    _clear_services(user_id)


async def complete_oauth(user_id: str, code: str) -> None:
    """Callback half: exchange code and persist encrypted credentials."""
    creds = exchange_code(code)
    await save_credentials(user_id, creds)


async def is_authenticated(user_id: str) -> bool:
    try:
        return await load_credentials(user_id) is not None
    except GoogleClientError:
        return False


async def load_credentials(user_id: str):
    """Load Credentials for user_id, refreshing + re-encrypting when expired.

    Returns None if no row exists. Raises GoogleClientError if a row exists
    but cannot be used (missing refresh, refresh failed, etc.).
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    row = await db.get_oauth_credentials(user_id, PROVIDER)
    if row is None:
        return None
    if not row.get("access_token_enc"):
        raise GoogleClientError(
            "Google account row exists but has no access token — reconnect Google."
        )

    env = _require_env("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    refresh_plain = (
        decrypt_token(row["refresh_token_enc"])
        if row.get("refresh_token_enc")
        else None
    )
    scopes = list(row["scopes"]) if row.get("scopes") else list(SCOPES)

    creds = Credentials(
        token=decrypt_token(row["access_token_enc"]),
        refresh_token=refresh_plain,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env["GOOGLE_CLIENT_ID"],
        client_secret=env["GOOGLE_CLIENT_SECRET"],
        scopes=scopes,
    )
    if row.get("expires_at") is not None:
        exp = row["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        creds.expiry = exp.replace(tzinfo=None)  # google-auth expects naive UTC

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GoogleClientError(
                f"Google token refresh failed — reconnect Google: {exc}"
            ) from exc
        await save_credentials(user_id, creds)
        return creds

    raise GoogleClientError(
        "Google credentials expired and no refresh token is stored. "
        "Reconnect Google from the app."
    )


def _clear_services(user_id: Optional[str] = None) -> None:
    if user_id is None:
        _services.clear()
        return
    for key in [k for k in _services if k[0] == user_id]:
        del _services[key]


async def _service(user_id: str, api: str, version: str):
    from googleapiclient.discovery import build

    creds = await load_credentials(user_id)
    if creds is None:
        raise GoogleClientError(
            "Google account not connected. Open Connect Google in the app."
        )
    key = (user_id, api, version)
    if key not in _services:
        _services[key] = build(api, version, credentials=creds, cache_discovery=False)
    return _services[key]


def _api_call(fn):
    """Retry once on 401/403 after clearing the cached service for that user."""

    @functools.wraps(fn)
    async def wrapper(user_id: str, *args, **kwargs):
        for attempt in range(2):
            try:
                return await fn(user_id, *args, **kwargs)
            except GoogleClientError:
                raise
            except HttpError as exc:
                if attempt == 0 and exc.resp.status in (401, 403):
                    _clear_services(user_id)
                    continue
                hint = ""
                if exc.resp.status in (401, 403):
                    hint = " Reconnect Google if this persists."
                raise GoogleClientError(
                    f"Google API error in {fn.__name__}: "
                    f"{getattr(exc, 'reason', str(exc))}.{hint}"
                ) from exc
            except Exception as exc:
                raise GoogleClientError(f"{fn.__name__} failed: {exc}") from exc

    return wrapper


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _ensure_tz(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return dt.isoformat()


def _parse_dt(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    return dt


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _format_event(e: dict) -> dict:
    start = e.get("start", {})
    end = e.get("end", {})
    return {
        "id": e.get("id"),
        "title": e.get("summary", "(no title)"),
        "start": start.get("dateTime", start.get("date")),
        "end": end.get("dateTime", end.get("date")),
        "attendees": [a.get("email") for a in e.get("attendees", []) if a.get("email")],
        "location": e.get("location", ""),
        "htmlLink": e.get("htmlLink", ""),
    }


async def _list_events_in_range(
    user_id: str, time_min: str, time_max: str, query: str | None = None
) -> list[dict]:
    kwargs: dict = {
        "calendarId": "primary",
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 50,
    }
    if query:
        kwargs["q"] = query
    svc = await _service(user_id, "calendar", "v3")
    resp = svc.events().list(**kwargs).execute()
    return [_format_event(e) for e in resp.get("items", [])]


def _event_matches_query(event: dict, query: str) -> bool:
    q = query.lower()
    hay = " ".join(
        [
            event.get("title") or "",
            " ".join(event.get("attendees") or []),
            event.get("start") or "",
            event.get("location") or "",
        ]
    ).lower()

    hour_match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", q)
    if hour_match:
        hour = int(hour_match.group(1))
        minute = int(hour_match.group(2) or 0)
        ampm = hour_match.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        start = event.get("start") or ""
        if start:
            try:
                dt = _parse_dt(start)
                if dt.hour == hour and (not hour_match.group(2) or dt.minute == minute):
                    return True
            except ValueError:
                pass

    weekdays = (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    )
    for day in weekdays:
        if day in q:
            try:
                dt = _parse_dt(event.get("start") or "")
                if dt.strftime("%A").lower() == day:
                    return True
            except ValueError:
                pass

    tokens = [
        t
        for t in re.findall(r"\w+", q)
        if len(t) > 2 and t not in weekdays and t not in {"july", "june", "august"}
    ]
    if not tokens:
        return False
    return any(tok in hay for tok in tokens)


@_api_call
async def get_todays_events(user_id: str) -> list[dict]:
    tz = _local_tz()
    start = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    svc = await _service(user_id, "calendar", "v3")
    resp = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return [_format_event(e) for e in resp.get("items", [])]


@_api_call
async def search_events(user_id: str, query: str, days_ahead: int = 7) -> list[dict]:
    tz = _local_tz()
    now = datetime.now(tz)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()

    events = await _list_events_in_range(user_id, time_min, time_max, query=query)
    if events:
        return events

    all_events = await _list_events_in_range(user_id, time_min, time_max, query=None)
    if not query.strip():
        return all_events
    return [e for e in all_events if _event_matches_query(e, query)]


@_api_call
async def create_calendar_event(
    user_id: str,
    title: str,
    start_iso: str,
    end_iso: str,
    attendees: list[str] | None = None,
    description: str = "",
) -> dict:
    body = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": _ensure_tz(start_iso)},
        "end": {"dateTime": _ensure_tz(end_iso)},
        "attendees": [{"email": a} for a in (attendees or [])],
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    svc = await _service(user_id, "calendar", "v3")
    event = (
        svc.events()
        .insert(
            calendarId="primary",
            body=body,
            conferenceDataVersion=1,
            sendUpdates="all",
        )
        .execute()
    )
    return {
        "id": event.get("id"),
        "title": event.get("summary"),
        "start": event.get("start", {}).get("dateTime"),
        "end": event.get("end", {}).get("dateTime"),
        "attendees": [a.get("email") for a in event.get("attendees", [])],
        "htmlLink": event.get("htmlLink"),
        "meet_link": event.get("hangoutLink"),
    }


@_api_call
async def get_event(user_id: str, event_id: str) -> dict:
    svc = await _service(user_id, "calendar", "v3")
    e = svc.events().get(calendarId="primary", eventId=event_id).execute()
    return _format_event(e)


@_api_call
async def reschedule_event(
    user_id: str, event_id: str, new_start_iso: str, new_end_iso: str
) -> dict:
    svc = await _service(user_id, "calendar", "v3")
    event = svc.events().get(calendarId="primary", eventId=event_id).execute()
    event["start"] = {"dateTime": _ensure_tz(new_start_iso)}
    event["end"] = {"dateTime": _ensure_tz(new_end_iso)}
    updated = svc.events().update(
        calendarId="primary", eventId=event_id, body=event
    ).execute()
    return _format_event(updated)


def _format_window(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start.strftime('%a %b %d, %I:%M %p')} – {end.strftime('%I:%M %p')}"
    return f"{start.isoformat()} – {end.isoformat()}"


def compute_free_windows(start_iso: str, end_iso: str, busy: list[dict]) -> list[dict]:
    start = _parse_dt(start_iso)
    end = _parse_dt(end_iso)
    intervals = sorted((_parse_dt(b["start"]), _parse_dt(b["end"])) for b in busy)
    windows: list[dict] = []
    cursor = start
    for busy_start, busy_end in intervals:
        if busy_end <= cursor:
            continue
        if busy_start > cursor:
            win_end = min(busy_start, end)
            windows.append(
                {
                    "start": cursor.isoformat(),
                    "end": win_end.isoformat(),
                    "label": _format_window(cursor, win_end),
                }
            )
        cursor = max(cursor, busy_end)
    if cursor < end:
        windows.append(
            {
                "start": cursor.isoformat(),
                "end": end.isoformat(),
                "label": _format_window(cursor, end),
            }
        )
    return windows


@_api_call
async def get_availability(user_id: str, start_iso: str, end_iso: str) -> dict:
    body = {
        "timeMin": _ensure_tz(start_iso),
        "timeMax": _ensure_tz(end_iso),
        "items": [{"id": "primary"}],
    }
    svc = await _service(user_id, "calendar", "v3")
    resp = svc.freebusy().query(body=body).execute()
    busy = resp.get("calendars", {}).get("primary", {}).get("busy", [])
    free = compute_free_windows(start_iso, end_iso, busy)
    return {"free_windows": free, "busy_periods": len(busy)}


# ---------------------------------------------------------------------------
# People (Contacts)
# ---------------------------------------------------------------------------

def _person_name_strings(person: dict) -> list[str]:
    parts: list[str] = []
    for entry in person.get("names", []):
        for key in ("displayName", "givenName", "familyName", "unstructuredName"):
            val = entry.get(key)
            if val:
                parts.append(val)
    return parts


def _name_matches_query(query: str, *name_parts: str) -> bool:
    q = query.lower().strip()
    if not q:
        return False
    hay = " ".join(name_parts).lower()
    tokens = [t for t in re.findall(r"\w+", q) if len(t) > 1]
    if not tokens:
        return q in hay
    return all(tok in hay for tok in tokens)


def _person_candidates(person: dict, fallback_name: str = "") -> list[dict]:
    names = person.get("names", [])
    display = fallback_name
    if names:
        display = (
            names[0].get("displayName")
            or names[0].get("givenName")
            or fallback_name
        )
    out: list[dict] = []
    for addr in person.get("emailAddresses", []):
        email = addr.get("value", "")
        if email:
            out.append({"name": display, "email": email})
    return out


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in candidates:
        email = item.get("email", "")
        if email and email not in seen:
            seen.add(email)
            out.append(item)
    return out


def _search_contacts_api(people, query: str) -> list[dict]:
    people.people().searchContacts(
        query="", readMask="names,emailAddresses", pageSize=1
    ).execute()
    resp = (
        people.people()
        .searchContacts(query=query, readMask="names,emailAddresses", pageSize=25)
        .execute()
    )
    out: list[dict] = []
    for row in resp.get("results", []):
        out.extend(_person_candidates(row.get("person", {}), query))
    return out


def _search_other_contacts_api(people, query: str) -> list[dict]:
    people.otherContacts().search(
        query="", readMask="names,emailAddresses", pageSize=1
    ).execute()
    resp = (
        people.otherContacts()
        .search(query=query, readMask="names,emailAddresses", pageSize=25)
        .execute()
    )
    out: list[dict] = []
    for row in resp.get("results", []):
        person = row.get("person", row)
        out.extend(_person_candidates(person, query))
    return out


def _scan_connections(people, query: str, limit: int = 10) -> list[dict]:
    out: list[dict] = []
    token = None
    while len(out) < limit:
        kwargs: dict = {
            "resourceName": "people/me",
            "personFields": "names,emailAddresses",
            "pageSize": 200,
        }
        if token:
            kwargs["pageToken"] = token
        page = people.people().connections().list(**kwargs).execute()
        for person in page.get("connections", []):
            if not _name_matches_query(query, *_person_name_strings(person)):
                continue
            out.extend(_person_candidates(person, query))
            if len(out) >= limit:
                break
        token = page.get("nextPageToken")
        if not token:
            break
    return out[:limit]


@_api_call
async def lookup_contact(user_id: str, name: str) -> list[dict]:
    people = await _service(user_id, "people", "v1")
    candidates: list[dict] = []

    def _try_extend(fetcher) -> None:
        try:
            candidates.extend(fetcher())
        except HttpError:
            pass

    _try_extend(lambda: _scan_connections(people, name))
    if not candidates:
        _try_extend(lambda: _search_contacts_api(people, name))
    if not candidates:
        _try_extend(lambda: _search_other_contacts_api(people, name))

    return _dedupe_candidates(candidates)[:10]


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def _headers(payload: dict) -> dict:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


@_api_call
async def list_recent_emails(
    user_id: str, max_results: int = 10, unread_only: bool = False
) -> list[dict]:
    gmail = await _service(user_id, "gmail", "v1")
    query = "in:inbox" + (" is:unread" if unread_only else "")
    listed = (
        gmail.users()
        .messages()
        .list(userId="me", maxResults=max_results, q=query)
        .execute()
    )
    out = []
    for ref in listed.get("messages", []):
        msg = (
            gmail.users()
            .messages()
            .get(
                userId="me",
                id=ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        hdr = _headers(msg.get("payload", {}))
        out.append(
            {
                "id": ref["id"],
                "from": hdr.get("from", ""),
                "subject": hdr.get("subject", "(no subject)"),
                "date": hdr.get("date", ""),
                "snippet": msg.get("snippet", ""),
                "unread": "UNREAD" in msg.get("labelIds", []),
            }
        )
    return out


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _find_mime(part: dict, mime: str) -> str | None:
    if part.get("mimeType") == mime:
        data = part.get("body", {}).get("data")
        if data:
            return _decode(data)
    for sub in part.get("parts", []) or []:
        found = _find_mime(sub, mime)
        if found is not None:
            return found
    return None


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def _extract_plain_body(payload: dict) -> str:
    plain = _find_mime(payload, "text/plain")
    if plain is not None:
        return plain
    html = _find_mime(payload, "text/html")
    if html is not None:
        return _html_to_text(html)
    return ""


def _strip_quoted(body: str) -> str:
    delimiters = [
        r"\nOn .*? wrote:",
        r"\n-----Original Message-----",
        r"\n________________________________",
        r"\nFrom: .*?\nSent: ",
    ]
    cut = len(body)
    for pattern in delimiters:
        match = re.search(pattern, body, flags=re.DOTALL)
        if match:
            cut = min(cut, match.start())
    body = body[:cut]
    kept = [ln for ln in body.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(kept).strip()


@_api_call
async def has_reply_since(
    user_id: str,
    *,
    thread_id: str | None = None,
    from_email: str | None = None,
    since_iso: str,
) -> bool:
    if not thread_id and not from_email:
        raise GoogleClientError("has_reply_since needs thread_id or from_email")
    gmail = await _service(user_id, "gmail", "v1")
    since = _parse_dt(since_iso).astimezone(timezone.utc)
    my_email = (
        gmail.users().getProfile(userId="me").execute().get("emailAddress", "").lower()
    )

    if thread_id:
        thread = (
            gmail.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        for msg in thread.get("messages", []):
            internal = int(msg.get("internalDate", 0)) / 1000
            msg_dt = datetime.fromtimestamp(internal, tz=timezone.utc)
            if msg_dt <= since:
                continue
            hdr = _headers(msg.get("payload", {}))
            from_hdr = hdr.get("from", "")
            if my_email and my_email in from_hdr.lower():
                continue
            return True
        return False

    after = since.strftime("%Y/%m/%d")
    q = f"from:{from_email} after:{after}"
    listed = gmail.users().messages().list(userId="me", maxResults=5, q=q).execute()
    for ref in listed.get("messages", []):
        msg = (
            gmail.users()
            .messages()
            .get(userId="me", id=ref["id"], format="metadata", metadataHeaders=["Date"])
            .execute()
        )
        internal = int(msg.get("internalDate", 0)) / 1000
        msg_dt = datetime.fromtimestamp(internal, tz=timezone.utc)
        if msg_dt > since:
            return True
    return False


@_api_call
async def read_email(user_id: str, message_id: str) -> dict:
    gmail = await _service(user_id, "gmail", "v1")
    msg = gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = msg.get("payload", {})
    hdr = _headers(payload)
    return {
        "id": message_id,
        "thread_id": msg.get("threadId"),
        "from": hdr.get("from", ""),
        "to": hdr.get("to", ""),
        "subject": hdr.get("subject", "(no subject)"),
        "date": hdr.get("date", ""),
        "body": _strip_quoted(_extract_plain_body(payload)),
    }


@_api_call
async def create_email_draft(
    user_id: str,
    to: str,
    subject: str,
    body: str,
    reply_to_message_id: str | None = None,
) -> dict:
    gmail = await _service(user_id, "gmail", "v1")
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    thread_id = None
    if reply_to_message_id:
        orig = (
            gmail.users()
            .messages()
            .get(
                userId="me",
                id=reply_to_message_id,
                format="metadata",
                metadataHeaders=["Message-ID", "References"],
            )
            .execute()
        )
        thread_id = orig.get("threadId")
        hdr = _headers(orig.get("payload", {}))
        parent_id = hdr.get("message-id")
        if parent_id:
            message["In-Reply-To"] = parent_id
            message["References"] = f"{hdr.get('references', '')} {parent_id}".strip()

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    msg_payload: dict = {"raw": raw}
    if thread_id:
        msg_payload["threadId"] = thread_id
    draft = gmail.users().drafts().create(userId="me", body={"message": msg_payload}).execute()
    return {
        "id": draft.get("id"),
        "to": to,
        "subject": subject,
        "reply_to_message_id": reply_to_message_id,
        "thread_id": thread_id,
    }


@_api_call
async def get_draft_preview(user_id: str, draft_id: str) -> dict:
    gmail = await _service(user_id, "gmail", "v1")
    draft = (
        gmail.users()
        .drafts()
        .get(userId="me", id=draft_id, format="full")
        .execute()
    )
    payload = draft.get("message", {}).get("payload", {})
    hdr = _headers(payload)
    body = _extract_plain_body(payload)
    preview = body[:200] + ("…" if len(body) > 200 else "")
    return {
        "draft_id": draft_id,
        "to": hdr.get("to", ""),
        "subject": hdr.get("subject", "(no subject)"),
        "body_preview": preview,
        "body": body,
    }


@_api_call
async def send_draft(user_id: str, draft_id: str) -> dict:
    gmail = await _service(user_id, "gmail", "v1")
    sent = gmail.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}
