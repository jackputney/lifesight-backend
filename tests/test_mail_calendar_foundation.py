"""Mail & Calendar foundation — fakes / mocked OAuth (no live Google).

Run:  python -m unittest tests.test_mail_calendar_foundation -v
"""

from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError

from main import app
from shared.mail_calendar import bounds, oauth, service, transactions
from shared.mail_calendar.sanitize import plain_text
from shared.mail_calendar.types import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventSummary,
    ConnectionStatus,
    EventListOut,
    FreeBusyOut,
    FreeBusySlot,
    MailListOut,
    MailMessage,
    MailMessageSummary,
)

APP_RETURN = "lifesight://mail-calendar/oauth-complete"
USER_A = "00000000-0000-4000-8000-0000000000aa"
USER_B = "00000000-0000-4000-8000-0000000000bb"
DEV_USER = "00000000-0000-4000-8000-000000000001"


def _fernet_key() -> str:
    return Fernet.generate_key().decode()


def _env(**overrides: str):
    base = {
        "GOOGLE_CLIENT_ID": "client-id",
        "GOOGLE_CLIENT_SECRET": "client-secret",  # pragma: allowlist secret
        "GOOGLE_MAIL_CALENDAR_REDIRECT_URI": (
            "http://127.0.0.1:8000/mail-calendar/oauth/callback"
        ),
        "MAIL_CALENDAR_OAUTH_ENV": "development",
        "MAIL_CALENDAR_APP_RETURN_URI_ALLOWLIST": APP_RETURN,
        "OAUTH_STATE_SECRET": "state-secret-for-tests",  # pragma: allowlist secret
        "TOKEN_ENCRYPTION_KEY": "0" * 43 + "=",
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class FakeMailProvider:
    last_max_results: int | None = None

    async def list_messages(self, *, query=None, max_results=20, page_token=None):
        FakeMailProvider.last_max_results = max_results
        return MailListOut(
            items=[
                MailMessageSummary(
                    id="m1",
                    thread_id="t1",
                    subject="Hello",
                    from_address="a@example.com",
                    snippet="Hi",
                )
            ],
            next_page_token="page-2" if not page_token else None,
        )

    async def get_message(self, message_id: str):
        return MailMessage(
            id=message_id,
            subject="Hello",
            from_address="a@example.com",
            body_text="Body",
        )


class FakeCalendarProvider:
    last_max_results: int | None = None

    async def list_events(
        self,
        *,
        time_min,
        time_max,
        max_results=50,
        calendar_id="primary",
        page_token=None,
    ):
        FakeCalendarProvider.last_max_results = max_results
        return EventListOut(
            items=[
                CalendarEventSummary(
                    id="e1",
                    calendar_id=calendar_id,
                    summary="Standup",
                    start=time_min,
                    end=time_max,
                )
            ],
            next_page_token=None,
        )

    async def get_event(self, event_id: str, *, calendar_id="primary"):
        return CalendarEvent(
            id=event_id,
            calendar_id=calendar_id,
            summary="Standup",
            start="2026-08-05T15:00:00Z",
            end="2026-08-05T15:30:00Z",
            attendees=[
                CalendarAttendee(email="a@example.com", response_status="accepted")
            ],
        )

    async def freebusy(self, *, time_min, time_max, calendar_id="primary"):
        return FreeBusyOut(
            calendar_id=calendar_id,
            busy=[FreeBusySlot(start=time_min, end=time_max)],
            time_min=time_min,
            time_max=time_max,
        )


class _TxnCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        transactions.use_memory_transactions(True)
        transactions.clear_memory_transactions()

    def tearDown(self):
        transactions.clear_memory_transactions()
        transactions.use_memory_transactions(False)


# ---------------------------------------------------------------------------
# Original foundation suite (adapted to hardened API)
# ---------------------------------------------------------------------------


class ConfigAndStatusTests(unittest.TestCase):
    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    def test_missing_config_fails_safely(self, _c, _i):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLIENT_ID": "",
                "GOOGLE_CLIENT_SECRET": "",
                "GOOGLE_MAIL_CALENDAR_REDIRECT_URI": "",
                "GOOGLE_REDIRECT_URI": "",
                "MAIL_CALENDAR_APP_RETURN_URI_ALLOWLIST": "",
                "OAUTH_STATE_SECRET": "",
                "TOKEN_ENCRYPTION_KEY": "",
            },
            clear=False,
        ):
            with self.assertRaises(oauth.OAuthConfigError):
                oauth.require_oauth_config()
            with TestClient(app) as client:
                resp = client.get(
                    "/mail-calendar/status",
                    headers={"Authorization": "Bearer test"},
                )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["status"], "error")
            self.assertNotIn("access_token", body)
            self.assertNotIn("refresh_token", body)

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.db.get_oauth_credentials", new_callable=AsyncMock, return_value=None)
    def test_disconnected_status(self, _get, _c, _i):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with TestClient(app) as client:
                resp = client.get(
                    "/mail-calendar/status",
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "disconnected")


class OAuthStateTests(_TxnCase):
    async def test_oauth_start_url_construction(self):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            started = await oauth.start_authorization(
                DEV_USER, app_return_uri=APP_RETURN
            )
        url = started["authorization_url"]
        self.assertTrue(url.startswith("https://accounts.google.com/"))
        qs = parse_qs(urlparse(url).query)
        scopes = qs["scope"][0]
        self.assertIn("gmail.readonly", scopes)
        self.assertIn("calendar.readonly", scopes)
        self.assertNotIn("gmail.send", scopes)
        self.assertEqual(qs["code_challenge_method"][0], "S256")
        self.assertTrue(qs["code_challenge"][0])
        self.assertEqual(qs["state"][0], started["state"])

    def test_invalid_oauth_state_rejected(self):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with self.assertRaises(ValueError):
                oauth.verify_oauth_state_signature("not:a:valid:state:value")
            with self.assertRaises(ValueError):
                oauth.verify_oauth_state_signature("uid:1:nonce:deadbeef")


class OAuthCallbackTests(_TxnCase):
    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.mail_calendar.service.persist_tokens", new_callable=AsyncMock)
    async def test_callback_success_mocked_exchange(self, persist, _c, _i):
        key = _fernet_key()
        with _env(TOKEN_ENCRYPTION_KEY=key):
            started = await oauth.start_authorization(
                DEV_USER, app_return_uri=APP_RETURN
            )
            with patch.object(
                oauth,
                "_exchange_code_pkce",
                new_callable=AsyncMock,
                return_value={
                    "access_token": "access-secret-token",
                    "refresh_token": "refresh-secret-token",
                    "scopes": list(oauth.READ_SCOPES),
                    "expiry": datetime.now(timezone.utc) + timedelta(hours=1),
                },
            ):
                with TestClient(app) as client:
                    resp = client.get(
                        "/mail-calendar/oauth/callback",
                        params={"code": "auth-code", "state": started["state"]},
                        follow_redirects=False,
                    )
        self.assertEqual(resp.status_code, 302)
        loc = resp.headers["location"]
        self.assertEqual(loc, f"{APP_RETURN}?result=success")
        self.assertNotIn("mail_calendar=", loc)
        self.assertNotIn("access-secret-token", loc)
        self.assertNotIn("refresh-secret-token", loc)
        self.assertNotIn("auth-code", loc)
        self.assertNotIn("code=", loc)
        persist.assert_awaited()
        self.assertEqual(persist.await_args.args[0], DEV_USER)

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    def test_callback_rejects_bad_state(self, _c, _i):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with TestClient(app) as client:
                resp = client.get(
                    "/mail-calendar/oauth/callback",
                    params={"code": "auth-code", "state": "forged"},
                    follow_redirects=False,
                )
        self.assertEqual(resp.status_code, 400)


class OwnershipAndReauthTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_ownership_isolation(self):
        async def get_creds(uid, provider=oauth.PROVIDER_ID):
            if uid == USER_A:
                return {
                    "user_id": USER_A,
                    "provider": provider,
                    "access_token_enc": "enc-a",
                    "refresh_token_enc": "enc-r",
                    "scopes": list(oauth.READ_SCOPES),
                    "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                }
            return None

        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with patch("shared.db.get_oauth_credentials", side_effect=get_creds):
                with patch("shared.crypto.decrypt", side_effect=lambda c: "token"):
                    with patch(
                        "shared.mail_calendar.oauth.credentials_from_tokens",
                        return_value=MagicMock(),
                    ):
                        status_b = await service.get_status(USER_B)
                        self.assertEqual(status_b.status, ConnectionStatus.disconnected)
                        status_a = await service.get_status(USER_A)
                        self.assertEqual(status_a.status, ConnectionStatus.connected_read)

    async def test_revoked_token_reauth_required(self):
        row = {
            "user_id": USER_A,
            "provider": oauth.PROVIDER_ID,
            "access_token_enc": "enc-a",
            "refresh_token_enc": "enc-r",
            "scopes": list(oauth.READ_SCOPES),
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        }
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with patch("shared.db.get_oauth_credentials", AsyncMock(return_value=row)):
                with patch("shared.crypto.decrypt", side_effect=lambda c: "token"):
                    with patch(
                        "shared.mail_calendar.oauth.refresh_access_token",
                        side_effect=RefreshError("revoked"),
                    ):
                        status = await service.get_status(USER_A)
        self.assertEqual(status.status, ConnectionStatus.reauth_required)


class ReadPathTests(unittest.TestCase):
    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch(
        "shared.mail_calendar.service.get_mail_provider",
        new_callable=AsyncMock,
        return_value=FakeMailProvider(),
    )
    def test_mail_list_and_read(self, _mail, _c, _i):
        with TestClient(app) as client:
            listed = client.get(
                "/mail-calendar/mail",
                headers={"Authorization": "Bearer test"},
            )
            one = client.get(
                "/mail-calendar/mail/m1",
                headers={"Authorization": "Bearer test"},
            )
        self.assertEqual(listed.status_code, 200)
        body = listed.json()
        self.assertEqual(body["items"][0]["id"], "m1")
        self.assertEqual(body["next_page_token"], "page-2")
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["body_text"], "Body")
        self.assertNotIn("pending_action", body["items"][0])
        self.assertNotIn("access_token", one.json())

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch(
        "shared.mail_calendar.service.get_calendar_provider",
        new_callable=AsyncMock,
        return_value=FakeCalendarProvider(),
    )
    def test_calendar_list_read_freebusy(self, _cal, _c, _i):
        with TestClient(app) as client:
            events = client.get(
                "/mail-calendar/events",
                params={
                    "time_min": "2026-08-05T00:00:00Z",
                    "time_max": "2026-08-06T00:00:00Z",
                },
                headers={"Authorization": "Bearer test"},
            )
            one = client.get(
                "/mail-calendar/events/e1",
                headers={"Authorization": "Bearer test"},
            )
            busy = client.get(
                "/mail-calendar/freebusy",
                params={
                    "time_min": "2026-08-05T00:00:00Z",
                    "time_max": "2026-08-06T00:00:00Z",
                },
                headers={"Authorization": "Bearer test"},
            )
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["items"][0]["id"], "e1")
        self.assertEqual(one.status_code, 200)
        self.assertEqual(busy.status_code, 200)
        self.assertEqual(len(busy.json()["busy"]), 1)
        for payload in (events.json(), one.json(), busy.json()):
            self.assertNotIn("pending_action", payload)

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    def test_connect_missing_config_503(self, _c, _i):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLIENT_ID": "",
                "GOOGLE_CLIENT_SECRET": "",
                "GOOGLE_MAIL_CALENDAR_REDIRECT_URI": "",
                "GOOGLE_REDIRECT_URI": "",
                "MAIL_CALENDAR_APP_RETURN_URI_ALLOWLIST": "",
                "OAUTH_STATE_SECRET": "",
            },
            clear=False,
        ):
            with TestClient(app) as client:
                resp = client.post(
                    "/mail-calendar/connect",
                    headers={"Authorization": "Bearer test"},
                    json={"app_return_uri": APP_RETURN},
                )
        self.assertEqual(resp.status_code, 503)


class RedirectAllowlistTests(unittest.TestCase):
    def test_redirect_must_be_callback_path(self):
        with _env(
            TOKEN_ENCRYPTION_KEY=_fernet_key(),
            GOOGLE_MAIL_CALENDAR_REDIRECT_URI="https://evil.example/callback",
        ):
            with self.assertRaises(oauth.OAuthConfigError):
                oauth.require_oauth_config()


# ---------------------------------------------------------------------------
# Hardening suite
# ---------------------------------------------------------------------------


class HardeningOAuthTests(_TxnCase):
    async def test_expired_oauth_state_rejected(self):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            old = time.time() - oauth.OAUTH_STATE_TTL_SECONDS - 5
            started = await oauth.start_authorization(
                USER_A, app_return_uri=APP_RETURN, now=old
            )
            with self.assertRaises(ValueError) as ctx:
                oauth.verify_oauth_state_signature(started["state"])
            self.assertIn("expired", str(ctx.exception).lower())
            with self.assertRaises(ValueError):
                await oauth.complete_authorization(
                    code="x", state=started["state"], now=time.time()
                )

    async def test_state_replay_rejected(self):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            started = await oauth.start_authorization(
                USER_A, app_return_uri=APP_RETURN
            )
            with patch.object(
                oauth,
                "_exchange_code_pkce",
                new_callable=AsyncMock,
                return_value={
                    "access_token": "a",
                    "refresh_token": "r",
                    "scopes": list(oauth.READ_SCOPES),
                    "expiry": datetime.now(timezone.utc) + timedelta(hours=1),
                },
            ):
                first = await oauth.complete_authorization(
                    code="code-1", state=started["state"]
                )
                self.assertEqual(first["access_token"], "a")
                with self.assertRaises(oauth.OAuthFlowError) as ctx:
                    await oauth.complete_authorization(
                        code="code-2", state=started["state"]
                    )
            self.assertIn("already used", str(ctx.exception).lower())

    async def test_wrong_user_state_rejected(self):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            started = await oauth.start_authorization(
                USER_A, app_return_uri=APP_RETURN
            )
            with self.assertRaises(ValueError) as ctx:
                await oauth.complete_authorization(
                    code="x",
                    state=started["state"],
                    expected_user_id=USER_B,
                )
            self.assertIn("mismatch", str(ctx.exception).lower())

    async def test_invalid_app_return_uri_rejected(self):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with self.assertRaises(ValueError):
                await oauth.start_authorization(
                    USER_A, app_return_uri="https://evil.example/steal"
                )
            with self.assertRaises(ValueError):
                oauth.validate_app_return_uri(
                    "javascript:alert(1)",
                    allowlist_csv="javascript:alert(1)",
                )

    async def test_pkce_challenge_and_verifier_flow(self):
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            started = await oauth.start_authorization(
                USER_A, app_return_uri=APP_RETURN
            )
            qs = parse_qs(urlparse(started["authorization_url"]).query)
            challenge = qs["code_challenge"][0]
            self.assertEqual(qs["code_challenge_method"][0], "S256")
            row = await transactions.get(started["state"])
            self.assertIsNotNone(row)
            self.assertTrue(row["code_verifier_enc"])

            captured: dict = {}

            async def fake_exchange(**kwargs):
                captured.update(kwargs)
                return {
                    "access_token": "a",
                    "refresh_token": "r",
                    "scopes": list(oauth.READ_SCOPES),
                    "expiry": datetime.now(timezone.utc) + timedelta(hours=1),
                }

            with patch.object(oauth, "_exchange_code_pkce", side_effect=fake_exchange):
                await oauth.complete_authorization(
                    code="auth-code", state=started["state"]
                )
            self.assertIn("code_verifier", captured)
            self.assertTrue(captured["code_verifier"])
            # Challenge must be S256 of the verifier used at exchange.
            import base64
            import hashlib

            digest = hashlib.sha256(captured["code_verifier"].encode("ascii")).digest()
            expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
            self.assertEqual(expected, challenge)
            leftover = await transactions.get(started["state"])
            self.assertIsNotNone(leftover)
            self.assertIsNotNone(leftover["consumed_at"])


class HardeningCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_access_token_refresh(self):
        row = {
            "user_id": USER_A,
            "provider": oauth.PROVIDER_ID,
            "access_token_enc": "enc-old",
            "refresh_token_enc": "enc-r",
            "scopes": list(oauth.READ_SCOPES),
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        }
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with patch("shared.db.get_oauth_credentials", AsyncMock(return_value=row)):
                with patch("shared.crypto.decrypt", side_effect=lambda c: "token"):
                    with patch(
                        "shared.mail_calendar.oauth.refresh_access_token",
                        return_value={
                            "access_token": "new-access",
                            "refresh_token": "token",
                            "scopes": list(oauth.READ_SCOPES),
                            "expiry": datetime.now(timezone.utc) + timedelta(hours=1),
                        },
                    ):
                        with patch(
                            "shared.mail_calendar.service.persist_tokens",
                            new_callable=AsyncMock,
                        ) as persist:
                            with patch(
                                "shared.mail_calendar.oauth.credentials_from_tokens",
                                return_value=MagicMock(),
                            ):
                                status = await service.get_status(USER_A)
        self.assertEqual(status.status, ConnectionStatus.connected_read)
        persist.assert_awaited()

    async def test_revoked_refresh_token_reauth_required(self):
        row = {
            "user_id": USER_A,
            "provider": oauth.PROVIDER_ID,
            "access_token_enc": "enc-a",
            "refresh_token_enc": "enc-r",
            "scopes": list(oauth.READ_SCOPES),
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        }
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with patch("shared.db.get_oauth_credentials", AsyncMock(return_value=row)):
                with patch("shared.crypto.decrypt", side_effect=lambda c: "token"):
                    with patch(
                        "shared.mail_calendar.oauth.refresh_access_token",
                        side_effect=RefreshError("invalid_grant"),
                    ):
                        status = await service.get_status(USER_A)
        self.assertEqual(status.status, ConnectionStatus.reauth_required)

    async def test_temporary_provider_failure_error(self):
        row = {
            "user_id": USER_A,
            "provider": oauth.PROVIDER_ID,
            "access_token_enc": "enc-a",
            "refresh_token_enc": "enc-r",
            "scopes": list(oauth.READ_SCOPES),
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        }
        with _env(TOKEN_ENCRYPTION_KEY=_fernet_key()):
            with patch("shared.db.get_oauth_credentials", AsyncMock(return_value=row)):
                with patch("shared.crypto.decrypt", side_effect=lambda c: "token"):
                    with patch(
                        "shared.mail_calendar.oauth.refresh_access_token",
                        side_effect=ConnectionError("dns"),
                    ):
                        status = await service.get_status(USER_A)
        self.assertEqual(status.status, ConnectionStatus.error)


class HardeningApiTests(unittest.TestCase):
    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch(
        "shared.mail_calendar.service.get_mail_provider",
        new_callable=AsyncMock,
        return_value=FakeMailProvider(),
    )
    def test_pagination_limit_enforcement(self, _mail, _c, _i):
        FakeMailProvider.last_max_results = None
        with TestClient(app) as client:
            resp = client.get(
                "/mail-calendar/mail",
                params={"max_results": 9999},
                headers={"Authorization": "Bearer test"},
            )
            bad_q = client.get(
                "/mail-calendar/mail",
                params={"q": "x" * (bounds.MAX_SEARCH_QUERY_LENGTH + 1)},
                headers={"Authorization": "Bearer test"},
            )
            wide = client.get(
                "/mail-calendar/events",
                params={
                    "time_min": "2026-01-01T00:00:00Z",
                    "time_max": "2026-12-31T00:00:00Z",
                },
                headers={"Authorization": "Bearer test"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(FakeMailProvider.last_max_results, bounds.MAX_MAIL_PAGE_SIZE)
        self.assertEqual(bad_q.status_code, 400)
        self.assertEqual(wide.status_code, 400)

    def test_normalized_dtos_no_raw_provider_or_html(self):
        dirty = plain_text(
            '<html><body><img src="https://evil.example/x.png">'
            "Hello <b>world</b></body></html>"
        )
        self.assertIsNotNone(dirty)
        self.assertNotIn("<", dirty)
        self.assertNotIn("img", dirty.lower())
        self.assertIn("Hello", dirty)

        msg = MailMessage(
            id="m1",
            subject=plain_text("<script>x</script>Hi"),
            body_text=dirty,
            snippet=plain_text("ok"),
        )
        dumped = msg.model_dump()
        blob = str(dumped)
        self.assertNotIn("<script", blob)
        self.assertNotIn("access_token", dumped)
        self.assertNotIn("refresh_token", dumped)
        self.assertNotIn("payload", dumped)

        event = CalendarEvent(
            id="e1",
            summary="Meet",
            description=plain_text("<p>notes</p>"),
            attendees=[CalendarAttendee(email="a@example.com")],
        )
        ed = event.model_dump()
        self.assertIsInstance(ed["attendees"][0], dict)
        self.assertEqual(set(ed["attendees"][0].keys()), {
            "email",
            "display_name",
            "response_status",
            "optional",
        })
        self.assertNotIn("htmlLink", ed)
        self.assertNotIn("etag", ed)

    def test_localhost_and_production_callback_configs_distinct(self):
        key = _fernet_key()
        with _env(
            TOKEN_ENCRYPTION_KEY=key,
            MAIL_CALENDAR_OAUTH_ENV="development",
            GOOGLE_MAIL_CALENDAR_REDIRECT_URI=(
                "http://127.0.0.1:8000/mail-calendar/oauth/callback"
            ),
        ):
            cfg_dev = oauth.require_oauth_config()
            self.assertEqual(cfg_dev["env"], "development")

        with _env(
            TOKEN_ENCRYPTION_KEY=key,
            MAIL_CALENDAR_OAUTH_ENV="production",
            GOOGLE_MAIL_CALENDAR_REDIRECT_URI=(
                "http://127.0.0.1:8000/mail-calendar/oauth/callback"
            ),
        ):
            with self.assertRaises(oauth.OAuthConfigError):
                oauth.require_oauth_config()

        with _env(
            TOKEN_ENCRYPTION_KEY=key,
            MAIL_CALENDAR_OAUTH_ENV="production",
            GOOGLE_MAIL_CALENDAR_REDIRECT_URI=(
                "https://api.example.com/mail-calendar/oauth/callback"
            ),
        ):
            cfg_prod = oauth.require_oauth_config()
            self.assertEqual(cfg_prod["env"], "production")
            self.assertNotEqual(
                cfg_prod["redirect_uri"],
                "http://127.0.0.1:8000/mail-calendar/oauth/callback",
            )

    def test_encryption_key_rotation_documented_incomplete(self):
        self.assertEqual(oauth.ENCRYPTION_KEY_ROTATION_STATUS, "incomplete")

    def test_app_return_result_success(self):
        self.assertEqual(
            oauth.build_app_redirect(APP_RETURN, result="success"),
            "lifesight://mail-calendar/oauth-complete?result=success",
        )

    def test_app_return_result_error(self):
        self.assertEqual(
            oauth.build_app_redirect(APP_RETURN, result="error"),
            "lifesight://mail-calendar/oauth-complete?result=error",
        )

    def test_app_return_result_reauth_required(self):
        self.assertEqual(
            oauth.build_app_redirect(APP_RETURN, result="reauth_required"),
            "lifesight://mail-calendar/oauth-complete?result=reauth_required",
        )

    def test_app_return_omits_sensitive_query_parameters(self):
        for result in ("success", "error", "reauth_required"):
            loc = oauth.build_app_redirect(APP_RETURN, result=result)
            parsed = urlparse(loc)
            q = parse_qs(parsed.query, keep_blank_values=True)
            self.assertEqual(list(q.keys()), ["result"])
            self.assertEqual(q["result"], [result])
            self.assertNotIn("mail_calendar", q)
            self.assertNotIn("detail", q)
            self.assertNotIn("code", q)
            self.assertNotIn("access_token", q)
            self.assertNotIn("refresh_token", q)
            self.assertNotIn("state", q)
            self.assertNotIn("user_id", q)
            self.assertNotIn("error", q)
            self.assertNotIn("error_description", q)
            blob = loc.lower()
            self.assertNotIn("access_token", blob)
            self.assertNotIn("refresh_token", blob)
            self.assertNotIn("bearer ", blob)

        with self.assertRaises(ValueError):
            oauth.build_app_redirect(APP_RETURN, result="connected")
        with self.assertRaises(ValueError):
            oauth.build_app_redirect(APP_RETURN, result="cancelled")


if __name__ == "__main__":
    unittest.main()
