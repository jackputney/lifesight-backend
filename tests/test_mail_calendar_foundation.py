"""Mail & Calendar foundation — fake providers / mocked OAuth (no live Google).

Run:  python -m unittest tests.test_mail_calendar_foundation -v
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError

from main import app
from shared.mail_calendar import oauth, service
from shared.mail_calendar.types import (
    CalendarEvent,
    CalendarEventSummary,
    ConnectionStatus,
    FreeBusyOut,
    FreeBusySlot,
    MailMessage,
    MailMessageSummary,
)


def _env(**overrides: str):
    base = {
        "GOOGLE_CLIENT_ID": "client-id",
        "GOOGLE_CLIENT_SECRET": "client-secret",  # pragma: allowlist secret
        "GOOGLE_MAIL_CALENDAR_REDIRECT_URI": "http://127.0.0.1:8000/mail-calendar/oauth/callback",
        "OAUTH_STATE_SECRET": "state-secret-for-tests",  # pragma: allowlist secret
        "TOKEN_ENCRYPTION_KEY": "0" * 43 + "=",  # invalid Fernet — tests that need crypto mock it
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class FakeMailProvider:
    async def list_messages(self, *, query=None, max_results=20):
        return [
            MailMessageSummary(
                id="m1",
                thread_id="t1",
                subject="Hello",
                from_address="a@example.com",
                snippet="Hi",
            )
        ]

    async def get_message(self, message_id: str):
        return MailMessage(
            id=message_id,
            subject="Hello",
            from_address="a@example.com",
            body_text="Body",
        )


class FakeCalendarProvider:
    async def list_events(self, *, time_min, time_max, max_results=50, calendar_id="primary"):
        return [
            CalendarEventSummary(
                id="e1",
                calendar_id=calendar_id,
                summary="Standup",
                start=time_min,
                end=time_max,
            )
        ]

    async def get_event(self, event_id: str, *, calendar_id="primary"):
        return CalendarEvent(
            id=event_id,
            calendar_id=calendar_id,
            summary="Standup",
            start="2026-08-05T15:00:00Z",
            end="2026-08-05T15:30:00Z",
        )

    async def freebusy(self, *, time_min, time_max, calendar_id="primary"):
        return FreeBusyOut(
            calendar_id=calendar_id,
            busy=[FreeBusySlot(start=time_min, end=time_max)],
            time_min=time_min,
            time_max=time_max,
        )


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
        # Valid Fernet key for require_oauth_config path
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        with _env(TOKEN_ENCRYPTION_KEY=key):
            with TestClient(app) as client:
                resp = client.get(
                    "/mail-calendar/status",
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "disconnected")


class OAuthStateTests(unittest.TestCase):
    def test_oauth_start_url_construction(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        with _env(TOKEN_ENCRYPTION_KEY=key):
            state = oauth.sign_oauth_state("00000000-0000-4000-8000-000000000001")
            with patch.object(
                oauth.Flow,
                "from_client_config",
                return_value=MagicMock(
                    authorization_url=MagicMock(
                        return_value=("https://accounts.google.com/o/oauth2/auth?x=1", state)
                    ),
                    redirect_uri=None,
                ),
            ) as flow_factory:
                url = oauth.build_authorization_url(state)
        self.assertTrue(url.startswith("https://accounts.google.com/"))
        flow_factory.assert_called()
        # Read scopes only
        called_scopes = flow_factory.call_args.args[1] if len(flow_factory.call_args.args) > 1 else flow_factory.call_args.kwargs.get("scopes")
        # from_client_config(config, scopes=...)
        scopes = flow_factory.call_args.kwargs.get("scopes") or flow_factory.call_args.args[1]
        self.assertTrue(any("gmail.readonly" in s for s in scopes))
        self.assertTrue(any("calendar.readonly" in s for s in scopes))
        self.assertFalse(any("gmail.send" in s for s in scopes))

    def test_invalid_oauth_state_rejected(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        with _env(TOKEN_ENCRYPTION_KEY=key):
            with self.assertRaises(ValueError):
                oauth.verify_oauth_state("not:a:valid:state:value")
            with self.assertRaises(ValueError):
                oauth.verify_oauth_state("uid:1:nonce:deadbeef")


class OAuthCallbackTests(unittest.TestCase):
    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch("shared.mail_calendar.service.persist_tokens", new_callable=AsyncMock)
    def test_callback_success_mocked_exchange(self, persist, _c, _i):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        user = "00000000-0000-4000-8000-000000000001"
        with _env(TOKEN_ENCRYPTION_KEY=key):
            state = oauth.sign_oauth_state(user)
            with patch.object(
                oauth,
                "exchange_code",
                return_value={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "scopes": list(oauth.READ_SCOPES),
                    "expiry": datetime.now(timezone.utc) + timedelta(hours=1),
                },
            ):
                with TestClient(app) as client:
                    resp = client.get(
                        "/mail-calendar/oauth/callback",
                        params={"code": "auth-code", "state": state},
                    )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("connected", resp.text.lower())
        persist.assert_awaited()
        self.assertEqual(persist.await_args.args[0], user)
        # Never echo tokens in HTML
        self.assertNotIn("access", resp.text)
        self.assertNotIn("refresh", resp.text)

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    def test_callback_rejects_bad_state(self, _c, _i):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        with _env(TOKEN_ENCRYPTION_KEY=key):
            with TestClient(app) as client:
                resp = client.get(
                    "/mail-calendar/oauth/callback",
                    params={"code": "auth-code", "state": "forged"},
                )
        self.assertEqual(resp.status_code, 400)


class OwnershipAndReauthTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_ownership_isolation(self):
        """Credentials for user A are not used for user B."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        user_a = "00000000-0000-4000-8000-0000000000aa"
        user_b = "00000000-0000-4000-8000-0000000000bb"

        async def get_creds(uid, provider=oauth.PROVIDER_ID):
            if uid == user_a:
                return {
                    "user_id": user_a,
                    "provider": provider,
                    "access_token_enc": "enc-a",
                    "refresh_token_enc": "enc-r",
                    "scopes": list(oauth.READ_SCOPES),
                    "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                }
            return None

        with _env(TOKEN_ENCRYPTION_KEY=key):
            with patch("shared.db.get_oauth_credentials", side_effect=get_creds):
                with patch("shared.crypto.decrypt", side_effect=lambda c: "token"):
                    with patch(
                        "shared.mail_calendar.oauth.credentials_from_tokens",
                        return_value=MagicMock(),
                    ):
                        status_b = await service.get_status(user_b)
                        self.assertEqual(status_b.status, ConnectionStatus.disconnected)
                        # A is connected (valid decrypt path)
                        status_a = await service.get_status(user_a)
                        self.assertEqual(status_a.status, ConnectionStatus.connected_read)

    async def test_revoked_token_reauth_required(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        user = "00000000-0000-4000-8000-0000000000aa"
        row = {
            "user_id": user,
            "provider": oauth.PROVIDER_ID,
            "access_token_enc": "enc-a",
            "refresh_token_enc": "enc-r",
            "scopes": list(oauth.READ_SCOPES),
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
        }
        with _env(TOKEN_ENCRYPTION_KEY=key):
            with patch("shared.db.get_oauth_credentials", AsyncMock(return_value=row)):
                with patch("shared.crypto.decrypt", side_effect=lambda c: "token"):
                    with patch(
                        "shared.mail_calendar.oauth.refresh_access_token",
                        side_effect=RefreshError("revoked"),
                    ):
                        status = await service.get_status(user)
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
        self.assertEqual(listed.json()[0]["id"], "m1")
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["body_text"], "Body")
        self.assertNotIn("pending_action", listed.json()[0])
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
        self.assertEqual(events.json()[0]["id"], "e1")
        self.assertEqual(one.status_code, 200)
        self.assertEqual(busy.status_code, 200)
        self.assertEqual(len(busy.json()["busy"]), 1)
        # Read paths never create Confirm Gate pending actions
        for payload in (events.json(), one.json(), busy.json()):
            if isinstance(payload, dict):
                self.assertNotIn("pending_action", payload)
            elif isinstance(payload, list) and payload:
                self.assertNotIn("pending_action", payload[0])

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
                "OAUTH_STATE_SECRET": "",
            },
            clear=False,
        ):
            with TestClient(app) as client:
                resp = client.post(
                    "/mail-calendar/connect",
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 503)


class RedirectAllowlistTests(unittest.TestCase):
    def test_redirect_must_be_callback_path(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        with _env(
            TOKEN_ENCRYPTION_KEY=key,
            GOOGLE_MAIL_CALENDAR_REDIRECT_URI="https://evil.example/callback",
        ):
            with self.assertRaises(oauth.OAuthConfigError):
                oauth.require_oauth_config()


if __name__ == "__main__":
    unittest.main()
