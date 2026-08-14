"""Google per-user OAuth + isolation (mocked/deterministic — no live Google).

Live Google OAuth is NOT exercised here. See docs/GOOGLE_INTEGRATIONS_SETUP.md
for manual Cloud console steps and TestFlight verification.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from shared import crypto
from shared.google import capabilities as caps
from shared.google import connection_store, oauth, transactions
from shared.google.connection_service import GoogleConnectionService
from shared.google.errors import GoogleFailureState, GoogleIntegrationError
from shared.google.token_service import GoogleTokenService


USER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
APP_RETURN = "lifesight://google-oauth"


def _env(**overrides: str):
    key = Fernet.generate_key().decode()
    base = {
        "AUTH_MODE": "dev",
        "APP_ENV": "test",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "unittest-placeholder",  # pragma: allowlist secret
        "TOKEN_ENCRYPTION_KEY": key,
        "GOOGLE_CLIENT_ID": "test-client-id",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",  # pragma: allowlist secret
        "GOOGLE_INTEGRATIONS_REDIRECT_URI": (
            "http://127.0.0.1:8000/integrations/google/callback"
        ),
        "GOOGLE_APP_RETURN_URI_ALLOWLIST": APP_RETURN,
        "GOOGLE_OAUTH_ENV": "development",
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class CapabilityMappingTests(unittest.TestCase):
    def test_default_capabilities_identity_and_calendar(self):
        self.assertEqual(
            caps.normalize_capabilities(None),
            ["google_identity", "calendar"],
        )

    def test_rejects_arbitrary_scope_urls_as_capabilities(self):
        with self.assertRaises(caps.UnknownCapabilityError):
            caps.normalize_capabilities(
                ["https://www.googleapis.com/auth/gmail.modify"]
            )

    def test_scopes_do_not_include_gmail_by_default(self):
        scopes = caps.scopes_for_capabilities(["google_identity", "calendar"])
        blob = " ".join(scopes)
        self.assertIn("calendar.events", blob)
        self.assertNotIn("gmail", blob)

    def test_capabilities_from_scopes(self):
        granted = caps.scopes_for_capabilities(
            ["google_identity", "calendar", "gmail_send"]
        )
        flags = caps.capabilities_from_scopes(granted)
        self.assertTrue(flags["calendar"])
        self.assertTrue(flags["gmail_send"])
        self.assertFalse(flags["gmail_read"])


class EncryptionReuseTests(unittest.TestCase):
    def test_fernet_roundtrip_via_token_service(self):
        with _env():
            enc = GoogleTokenService.encrypt_refresh_token("refresh-plain")
            self.assertNotEqual(enc, "refresh-plain")
            self.assertEqual(
                GoogleTokenService.decrypt_refresh_token(enc), "refresh-plain"
            )
            # shared.crypto is the underlying abstraction.
            self.assertEqual(crypto.decrypt(enc), "refresh-plain")


class IsolationAndOAuthStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env_ctx = _env()
        self._env_ctx.start()
        transactions.use_memory_transactions(True)
        connection_store.use_memory_connections(True)
        transactions.clear_memory_transactions()
        connection_store.clear_memory_connections()

    async def asyncTearDown(self):
        transactions.use_memory_transactions(False)
        connection_store.use_memory_connections(False)
        self._env_ctx.stop()

    async def test_a_connected_b_disconnected(self):
        await GoogleConnectionService.persist_authorized_connection(
            user_id=USER_A,
            google_subject="sub-a",
            google_email="a@example.com",
            display_name="A",
            refresh_token="refresh-a",
            granted_scopes=caps.scopes_for_capabilities(None),
        )
        status_a = await GoogleConnectionService.get_status(USER_A)
        status_b = await GoogleConnectionService.get_status(USER_B)
        self.assertTrue(status_a["connected"])
        self.assertEqual(status_a["email"], "a@example.com")
        self.assertFalse(status_b["connected"])
        self.assertIsNone(status_b["email"])

    async def test_a_and_b_different_google_identities(self):
        await GoogleConnectionService.persist_authorized_connection(
            user_id=USER_A,
            google_subject="sub-a",
            google_email="a@example.com",
            display_name="A",
            refresh_token="refresh-a",
            granted_scopes=caps.scopes_for_capabilities(None),
        )
        await GoogleConnectionService.persist_authorized_connection(
            user_id=USER_B,
            google_subject="sub-b",
            google_email="b@example.com",
            display_name="B",
            refresh_token="refresh-b",
            granted_scopes=caps.scopes_for_capabilities(None),
        )
        row_a = await GoogleConnectionService.get_active_connection(USER_A)
        row_b = await GoogleConnectionService.get_active_connection(USER_B)
        self.assertEqual(row_a["google_subject"], "sub-a")
        self.assertEqual(row_b["google_subject"], "sub-b")
        self.assertNotEqual(
            row_a["encrypted_refresh_token"], row_b["encrypted_refresh_token"]
        )
        self.assertEqual(
            GoogleTokenService.decrypt_refresh_token(row_a["encrypted_refresh_token"]),
            "refresh-a",
        )
        self.assertEqual(
            GoogleTokenService.decrypt_refresh_token(row_b["encrypted_refresh_token"]),
            "refresh-b",
        )

    async def test_disconnect_a_leaves_b_untouched(self):
        await GoogleConnectionService.persist_authorized_connection(
            user_id=USER_A,
            google_subject="sub-a",
            google_email="a@example.com",
            display_name="A",
            refresh_token="refresh-a",
            granted_scopes=caps.scopes_for_capabilities(None),
        )
        await GoogleConnectionService.persist_authorized_connection(
            user_id=USER_B,
            google_subject="sub-b",
            google_email="b@example.com",
            display_name="B",
            refresh_token="refresh-b",
            granted_scopes=caps.scopes_for_capabilities(None),
        )
        await GoogleConnectionService.disconnect(USER_A)
        self.assertFalse((await GoogleConnectionService.get_status(USER_A))["connected"])
        self.assertTrue((await GoogleConnectionService.get_status(USER_B))["connected"])
        self.assertEqual(
            (await GoogleConnectionService.get_active_connection(USER_B))[
                "google_subject"
            ],
            "sub-b",
        )

    async def test_oauth_state_for_a_cannot_be_redeemed_for_b(self):
        started = await oauth.start_authorization(
            USER_A, app_return_uri=APP_RETURN, requested_capabilities=None
        )
        # Pull state from memory (not returned to clients in start response).
        state = next(iter(transactions._MEMORY.keys()))
        self.assertIn("accounts.google.com", started["authorization_url"])
        self.assertIn(state, started["authorization_url"])

        # Consume as if Google called back — binding is USER_A from server row.
        with patch.object(
            oauth,
            "_exchange_code_pkce",
            new=AsyncMock(
                return_value={
                    "access_token": "access",
                    "refresh_token": "refresh-new",
                    "scopes": caps.scopes_for_capabilities(None),
                }
            ),
        ), patch.object(
            oauth,
            "_fetch_userinfo",
            new=AsyncMock(
                return_value={
                    "sub": "sub-a",
                    "email": "a@example.com",
                    "name": "A",
                }
            ),
        ):
            material = await oauth.complete_authorization(code="code", state=state)

        self.assertEqual(material["user_id"], USER_A)
        self.assertNotEqual(material["user_id"], USER_B)
        # There is no API to pass user_id on callback; binding is server-side only.

    async def test_expired_state_fails(self):
        await transactions.create_transaction(
            state="expired-state",
            user_id=USER_A,
            code_verifier_enc=crypto.encrypt("verifier"),
            app_return_uri=APP_RETURN,
            requested_capabilities=["google_identity", "calendar"],
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        with self.assertRaises(oauth.OAuthFlowError) as ctx:
            await oauth.complete_authorization(code="code", state="expired-state")
        self.assertIn("expired", str(ctx.exception).lower())

    async def test_reused_state_fails(self):
        await transactions.create_transaction(
            state="once-state",
            user_id=USER_A,
            code_verifier_enc=crypto.encrypt("verifier"),
            app_return_uri=APP_RETURN,
            requested_capabilities=["google_identity", "calendar"],
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        with patch.object(
            oauth,
            "_exchange_code_pkce",
            new=AsyncMock(
                return_value={
                    "access_token": "access",
                    "refresh_token": "refresh-new",
                    "scopes": caps.scopes_for_capabilities(None),
                }
            ),
        ), patch.object(
            oauth,
            "_fetch_userinfo",
            new=AsyncMock(return_value={"sub": "sub-a", "email": "a@example.com"}),
        ):
            await oauth.complete_authorization(code="code", state="once-state")
        with self.assertRaises(oauth.OAuthFlowError) as ctx:
            await oauth.complete_authorization(code="code", state="once-state")
        self.assertIn("already used", str(ctx.exception).lower())

    async def test_arbitrary_user_id_cannot_select_another_connection(self):
        await GoogleConnectionService.persist_authorized_connection(
            user_id=USER_A,
            google_subject="sub-a",
            google_email="a@example.com",
            display_name="A",
            refresh_token="refresh-a",
            granted_scopes=caps.scopes_for_capabilities(None),
        )
        row_a = await GoogleConnectionService.get_active_connection(USER_A)
        # Looking up A's connection id under B's identity returns None.
        stolen = await connection_store.get_connection_by_id_for_user(
            str(row_a["id"]), USER_B
        )
        self.assertIsNone(stolen)
        # B still has no active connection.
        self.assertIsNone(await GoogleConnectionService.get_active_connection(USER_B))

    async def test_tokens_never_appear_in_status_payload(self):
        await GoogleConnectionService.persist_authorized_connection(
            user_id=USER_A,
            google_subject="sub-a",
            google_email="a@example.com",
            display_name="A",
            refresh_token="super-secret-refresh",
            granted_scopes=caps.scopes_for_capabilities(None),
        )
        status = await GoogleConnectionService.get_status(USER_A)
        blob = str(status)
        self.assertNotIn("super-secret-refresh", blob)
        self.assertNotIn("encrypted_refresh_token", blob)
        self.assertNotIn("access_token", blob)
        self.assertIn("capabilities", status)
        self.assertTrue(status["connected"])


class HttpContractTests(unittest.TestCase):
    def test_status_start_disconnect_shapes_and_no_tokens(self):
        with _env():
            transactions.use_memory_transactions(True)
            connection_store.use_memory_connections(True)
            transactions.clear_memory_transactions()
            connection_store.clear_memory_connections()
            try:
                with patch("shared.db.init_pool", new_callable=AsyncMock), patch(
                    "shared.db.close_pool", new_callable=AsyncMock
                ):
                    from main import app

                    with TestClient(app) as client:
                        # AUTH_MODE=dev → fixed user; status disconnected.
                        st = client.get("/integrations/google/status")
                        self.assertEqual(st.status_code, 200)
                        body = st.json()
                        self.assertEqual(
                            set(body.keys()),
                            {"connected", "email", "capabilities"},
                        )
                        self.assertFalse(body["connected"])
                        self.assertEqual(
                            set(body["capabilities"].keys()),
                            set(caps.CAPABILITY_IDS),
                        )

                        start = client.post(
                            "/integrations/google/start",
                            json={
                                "app_return_uri": APP_RETURN,
                                "capabilities": ["google_identity", "calendar"],
                            },
                        )
                        self.assertEqual(start.status_code, 200)
                        sbody = start.json()
                        self.assertEqual(
                            set(sbody.keys()), {"authorization_url", "expires_in"}
                        )
                        joined = str(sbody)
                        self.assertNotIn("super-secret", joined)
                        self.assertNotIn("refresh_token", joined)
                        self.assertNotIn("client_secret", joined)

                        disc = client.post("/integrations/google/disconnect")
                        self.assertEqual(disc.status_code, 200)
                        self.assertEqual(disc.json(), {"disconnected": True})

                        # Abandoned Docs routes stay 410.
                        self.assertEqual(
                            client.get("/oauth/google/start").status_code, 410
                        )
            finally:
                transactions.use_memory_transactions(False)
                connection_store.use_memory_connections(False)

    def test_start_rejects_unknown_capability(self):
        with _env():
            with patch("shared.db.init_pool", new_callable=AsyncMock), patch(
                "shared.db.close_pool", new_callable=AsyncMock
            ):
                from main import app

                with TestClient(app) as client:
                    resp = client.post(
                        "/integrations/google/start",
                        json={
                            "app_return_uri": APP_RETURN,
                            "capabilities": ["drive_full"],
                        },
                    )
                    self.assertEqual(resp.status_code, 400)


class FailureStateTests(unittest.TestCase):
    def test_spoken_messages_cover_all_states(self):
        for state in GoogleFailureState:
            err = GoogleIntegrationError(state)
            self.assertTrue(err.spoken())
            self.assertEqual(err.state.value, state.value)


class ConfirmGateMailCalendarTests(unittest.TestCase):
    def test_confirm_routes_calendar_action_types(self):
        import inspect

        from main import confirm

        src = inspect.getsource(confirm)
        for action in (
            "create_calendar_event",
            "update_calendar_event",
            "delete_calendar_event",
            "send_email",
        ):
            self.assertIn(action, src)


if __name__ == "__main__":
    unittest.main()
