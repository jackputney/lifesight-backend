#!/usr/bin/env python3
"""Track A: standalone smoke test for shared/google_docs.py.

Exercises create → read → append → read-back against the live Google Docs API.
No Postgres, no shared/crypto.py, no main.py — token stays in memory unless you
set GOOGLE_ACCESS_TOKEN or GOOGLE_REFRESH_TOKEN in .env for a pre-obtained token.

Usage (from repo root):
    ./venv/bin/python scripts/track_a_google_docs.py

If no token env vars are set, opens a browser for one-time Google consent using
GOOGLE_REDIRECT_URI from .env (must match Google Cloud Console). Token is never
written to the database.
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv(".env")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import google_docs

SCOPES = google_docs.SCOPES


def _fail(step: str, exc: BaseException) -> None:
    print(f"FAIL at {step}: {type(exc).__name__}: {exc}", flush=True)
    traceback.print_exc()
    sys.exit(1)


def _obtain_tokens_via_browser() -> tuple[str, str | None, list[str]]:
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI")
    if not redirect_uri:
        print("FAIL: GOOGLE_REDIRECT_URI not set in .env.", flush=True)
        sys.exit(1)

    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        print(
            "FAIL: Track A browser flow expects a localhost http redirect URI "
            f"(got {redirect_uri!r}). Set GOOGLE_ACCESS_TOKEN manually instead.",
            flush=True,
        )
        sys.exit(1)

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    callback_path = parsed.path or "/"
    auth_code: dict[str, str | None] = {"value": None}
    server_error: dict[str, str | None] = {"value": None}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # quiet default logging
            return

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != callback_path:
                self.send_error(404)
                return
            params = parse_qs(urlparse(self.path).query)
            if params.get("error"):
                server_error["value"] = params["error"][0]
                body = b"Authorization failed. You can close this tab."
                self.send_response(400)
            elif params.get("code"):
                auth_code["value"] = params["code"][0]
                body = b"Authorization complete. Return to the terminal."
                self.send_response(200)
            else:
                self.send_error(400)
                return
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer((parsed.hostname, port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = google_docs.build_auth_url(state="track-a-local-smoke")
    print("Opening browser for Google consent (token stays in memory, not DB)…", flush=True)
    print(f"If the browser does not open, visit this URL manually:\n{auth_url}\n", flush=True)
    webbrowser.open(auth_url)
    thread.join(timeout=300)
    server.server_close()

    if server_error["value"]:
        print(f"FAIL: Google returned error={server_error['value']!r}", flush=True)
        sys.exit(1)
    if not auth_code["value"]:
        print("FAIL: Timed out waiting for OAuth callback (5 min).", flush=True)
        sys.exit(1)

    tokens = google_docs.exchange_code(auth_code["value"], "track-a-local-smoke")
    return tokens["access_token"], tokens.get("refresh_token"), list(tokens.get("scopes") or SCOPES)


def _resolve_tokens() -> tuple[str, str | None, list[str]]:
    access = os.environ.get("GOOGLE_ACCESS_TOKEN")
    refresh = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if access:
        return access, refresh, SCOPES
    if refresh:
        for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
            if not os.environ.get(key):
                print(f"FAIL: GOOGLE_REFRESH_TOKEN set but {key} missing.", flush=True)
                sys.exit(1)
        refreshed = google_docs.refresh_access_token(refresh, SCOPES)
        return (
            refreshed["access_token"],
            refreshed.get("refresh_token") or refresh,
            SCOPES,
        )
    return _obtain_tokens_via_browser()


def main() -> None:
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"):
        if not os.environ.get(key):
            print(f"FAIL: {key} not set in .env.", flush=True)
            sys.exit(1)

    marker = f"[LIFESIGHT Track A {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}]"
    access, refresh, scopes = _resolve_tokens()
    title = f"LIFESIGHT Track A smoke {datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    print("Step 1/4: create_document …", flush=True)
    try:
        doc_id = google_docs.create_document(access, refresh, scopes, title)
    except Exception as exc:
        _fail("create_document", exc)
    print(f"  OK — document created (id length {len(doc_id)})", flush=True)

    print("Step 2/4: get_document_text (empty doc) …", flush=True)
    try:
        text_before = google_docs.get_document_text(access, refresh, scopes, doc_id)
    except Exception as exc:
        _fail("get_document_text (before append)", exc)
    print(f"  OK — read {len(text_before)} chars", flush=True)

    print("Step 3/4: append_text …", flush=True)
    try:
        revision_id = google_docs.append_text(access, refresh, scopes, doc_id, marker + "\n")
    except Exception as exc:
        _fail("append_text", exc)
    print(f"  OK — revisionId present: {bool(revision_id)}", flush=True)

    print("Step 4/4: get_document_text (after append) …", flush=True)
    try:
        text_after = google_docs.get_document_text(access, refresh, scopes, doc_id)
    except Exception as exc:
        _fail("get_document_text (after append)", exc)

    if marker not in text_after:
        print("FAIL: appended marker not found in read-back.", flush=True)
        sys.exit(1)

    print("  OK — marker found in read-back", flush=True)
    print("", flush=True)
    print("TRACK A: PASS — create, read, append, read-back all succeeded.", flush=True)
    print("(Test doc left in Google Drive — delete manually if desired.)", flush=True)


if __name__ == "__main__":
    main()
