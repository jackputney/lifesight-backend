"""Google OAuth + Docs API wrapper for Author Mode.

Pure API mechanics only — this module never touches Postgres or encryption.
It takes and returns plain token dicts; main.py (which already owns identity
+ storage orchestration for every other route) is responsible for reading/
writing shared/db.py and encrypting/decrypting via shared/crypto.py. Keeping
those concerns out of this file means it can be tested against Google alone.

Every function here is synchronous — google-auth / google-api-python-client
are both blocking under the hood. Callers run these via asyncio.to_thread,
the same pattern main.py already uses for the Anthropic SDK.

Scopes are the minimum needed: `documents` (read/write Docs content) and
`drive.file` (only files this app creates/opens — not the user's whole
Drive). Both are enabled in the same Google Cloud project as the OAuth
client (see README for setup steps).
"""
import os
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

# PKCE code_verifier lives on the Flow instance. build_auth_url and exchange_code
# must share the same Flow keyed by OAuth state — a fresh Flow on exchange fails
# with "Missing code verifier".
_pending_oauth_flows: dict[str, Flow] = {}


def _client_config() -> dict:
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI must "
            "all be set in .env — see the Google Cloud Console setup in the README."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def build_auth_url(state: str) -> str:
    """URL to redirect the user to for Google's consent screen. `state`
    round-trips through Google unmodified — the caller sets it to the
    LIFESIGHT user_id so the callback knows whose credentials these are."""
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]
    auth_url, _ = flow.authorization_url(
        access_type="offline",  # request a refresh token, not just a short-lived access token
        prompt="consent",       # force the consent screen every time, so a refresh token is issued
        state=state,
    )
    _pending_oauth_flows[state] = flow
    return auth_url


def exchange_code(code: str, state: str) -> dict:
    """Exchange an authorization code (from the OAuth callback) for tokens.
    `state` must match the value passed to build_auth_url — it selects the
    pending Flow that holds the PKCE verifier. Returns a plain dict;
    encrypting/storing it is the caller's job."""
    flow = _pending_oauth_flows.pop(state, None)
    if flow is None:
        raise ValueError(
            "No pending OAuth flow for this state — authorization may have "
            "expired or already been exchanged. Start again from /oauth/google/start."
        )
    flow.fetch_token(code=code)
    creds = flow.credentials
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "scopes": list(creds.scopes or SCOPES),
        "expiry": creds.expiry,  # naive UTC datetime, or None
    }


def refresh_access_token(refresh_token: str, scopes: list[str]) -> dict:
    """Use a refresh token to get a fresh access token. Google doesn't
    rotate refresh tokens on a plain refresh, so the same one is echoed
    back — callers should keep the original unless this returns a new one."""
    creds = _credentials_from_tokens(access_token="", refresh_token=refresh_token, scopes=scopes)
    creds.refresh(Request())
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token or refresh_token,
        "scopes": scopes,
        "expiry": creds.expiry,
    }


def _credentials_from_tokens(access_token: str, refresh_token: Optional[str], scopes: list[str]) -> Credentials:
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=scopes,
    )


def create_document(access_token: str, refresh_token: Optional[str], scopes: list[str], title: str) -> str:
    """Create a fresh, blank Google Doc. Returns its document id."""
    creds = _credentials_from_tokens(access_token, refresh_token, scopes)
    docs = build("docs", "v1", credentials=creds)
    doc = docs.documents().create(body={"title": title}).execute()
    return doc["documentId"]


def get_document_text(access_token: str, refresh_token: Optional[str], scopes: list[str], doc_id: str) -> str:
    """Plain-text content of the document, for Claude to read/summarize."""
    creds = _credentials_from_tokens(access_token, refresh_token, scopes)
    docs = build("docs", "v1", credentials=creds)
    doc = docs.documents().get(documentId=doc_id).execute()
    parts: list[str] = []
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts)


def append_text(access_token: str, refresh_token: Optional[str], scopes: list[str], doc_id: str, text: str) -> str:
    """Insert text at the end of the document via batchUpdate — never a
    full-document overwrite (CONTEXT.md: Docs is source of truth; an
    overwrite would silently delete concurrent web edits). Returns the
    document's new revisionId so the caller can stamp last_known_revision_id.

    First cut: always inserts at end-of-document. The full anchor/session
    machinery (writing_sessions/writing_drafts, mid-document insertion at a
    saved index) is drafted in the schema but intentionally not built yet —
    this is the simplest testable step, upgradeable later without a rewrite.
    """
    creds = _credentials_from_tokens(access_token, refresh_token, scopes)
    docs = build("docs", "v1", credentials=creds)
    doc = docs.documents().get(documentId=doc_id).execute()
    # The document body always ends with an implicit trailing newline that
    # can't be deleted; inserting at endIndex - 1 appends right before it
    # instead of after it.
    end_index = doc["body"]["content"][-1]["endIndex"] - 1
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": end_index}, "text": text}}]},
    ).execute()
    updated = docs.documents().get(documentId=doc_id).execute()
    return updated.get("revisionId", "")
