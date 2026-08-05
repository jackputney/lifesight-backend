"""Self-hosted username/password auth — memory store + TestClient.

Run:  python -m unittest tests.test_self_hosted_auth -v
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
from fastapi.testclient import TestClient

from shared.auth import assert_auth_mode_allowed
from shared.local_auth import passwords
from shared.local_auth.rate_limit import LoginRateLimiter
from shared.local_auth.store import use_memory_store
from shared.local_auth.tokens import ACCESS_AUD


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "self",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class SelfHostedAuthTests(unittest.TestCase):
    def setUp(self):
        self.store = use_memory_store(True)
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)
        self.addCleanup(lambda: use_memory_store(False))

    def _client(self):
        from main import app

        return TestClient(app)

    def _register(self, client, username="alice", email="alice@example.com", password="password123"):  # pragma: allowlist secret
        return client.post(
            "/auth/register",
            json={
                "username": username,
                "password": password,
                "email": email,
                "display_name": "Alice",
            },
        )

    def test_01_registration(self):
        with _env():
            with self._client() as client:
                resp = self._register(client)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("access_token", body)
        self.assertIn("refresh_token", body)
        self.assertEqual(body["user"]["username"], "alice")
        self.assertNotIn("password", body)
        self.assertNotIn("password_hash", body)

    def test_02_duplicate_username(self):
        with _env():
            with self._client() as client:
                self.assertEqual(self._register(client).status_code, 200)
                resp = self._register(client, email="other@example.com")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("Username", resp.json()["detail"])

    def test_03_duplicate_email(self):
        with _env():
            with self._client() as client:
                self.assertEqual(self._register(client).status_code, 200)
                resp = self._register(client, username="bob", email="alice@example.com")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("Email", resp.json()["detail"])

    def test_04_password_is_hashed(self):
        with _env():
            with self._client() as client:
                self.assertEqual(self._register(client).status_code, 200)
        user = next(iter(self.store.users.values()))
        self.assertTrue(user["password_hash"].startswith("$argon2id$"))
        self.assertNotEqual(user["password_hash"], "password123")  # pragma: allowlist secret
        self.assertTrue(passwords.verify_password(user["password_hash"], "password123"))  # pragma: allowlist secret

    def test_05_valid_login(self):
        with _env():
            with self._client() as client:
                self._register(client)
                resp = client.post(
                    "/auth/login",
                    json={"username": "alice", "password": "password123"},  # pragma: allowlist secret
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["user"]["username"], "alice")
        self.assertTrue(resp.json()["access_token"])

    def test_06_invalid_login_generic(self):
        with _env():
            with self._client() as client:
                self._register(client)
                resp = client.post(
                    "/auth/login",
                    json={"username": "alice", "password": "wrong-password"},  # pragma: allowlist secret
                )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Invalid credentials")

    def test_07_access_token_authenticates_me(self):
        with _env():
            with self._client() as client:
                reg = self._register(client).json()
                resp = client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {reg['access_token']}"},
                )
                prot = client.get(
                    "/auth/protected",
                    headers={"Authorization": f"Bearer {reg['access_token']}"},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["username"], "alice")
        self.assertEqual(prot.status_code, 200)
        self.assertEqual(prot.json()["user_id"], reg["user"]["id"])

    def test_08_expired_access_token_rejected(self):
        with _env():
            with self._client() as client:
                reg = self._register(client).json()
                user_id = reg["user"]["id"]
                # Pull session id from a fresh mint then overwrite with expired JWT.
                sid = next(iter(self.store.sessions))
                past = datetime.now(timezone.utc) - timedelta(hours=1)
                expired = jwt.encode(
                    {
                        "sub": user_id,
                        "sid": sid,
                        "typ": "access",
                        "aud": ACCESS_AUD,
                        "iat": int(past.timestamp()),
                        "exp": int((past + timedelta(minutes=1)).timestamp()),
                    },
                    os.environ["AUTH_JWT_SECRET"],
                    algorithm="HS256",
                )
                resp = client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {expired}"},
                )
        self.assertEqual(resp.status_code, 401)

    def test_09_refresh_token_rotation(self):
        with _env():
            with self._client() as client:
                reg = self._register(client).json()
                old_refresh = reg["refresh_token"]
                resp = client.post(
                    "/auth/refresh", json={"refresh_token": old_refresh}
                )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotEqual(body["refresh_token"], old_refresh)
        self.assertTrue(body["access_token"])

    def test_10_old_refresh_rejected_after_rotation(self):
        with _env():
            with self._client() as client:
                reg = self._register(client).json()
                old_refresh = reg["refresh_token"]
                self.assertEqual(
                    client.post(
                        "/auth/refresh", json={"refresh_token": old_refresh}
                    ).status_code,
                    200,
                )
                resp = client.post(
                    "/auth/refresh", json={"refresh_token": old_refresh}
                )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["detail"], "Invalid credentials")

    def test_11_logout_revokes_session(self):
        with _env():
            with self._client() as client:
                reg = self._register(client).json()
                headers = {"Authorization": f"Bearer {reg['access_token']}"}
                out = client.post(
                    "/auth/logout",
                    headers=headers,
                    json={"refresh_token": reg["refresh_token"]},
                )
                self.assertEqual(out.status_code, 200)
                me = client.get("/auth/me", headers=headers)
                refresh = client.post(
                    "/auth/refresh",
                    json={"refresh_token": reg["refresh_token"]},
                )
        self.assertEqual(me.status_code, 401)
        self.assertEqual(refresh.status_code, 401)

    def test_12_logout_all_revokes_all_sessions(self):
        with _env():
            with self._client() as client:
                a = self._register(client).json()
                b = client.post(
                    "/auth/login",
                    json={"username": "alice", "password": "password123"},  # pragma: allowlist secret
                ).json()
                headers = {"Authorization": f"Bearer {a['access_token']}"}
                resp = client.post("/auth/logout-all", headers=headers)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(
                    client.get("/auth/me", headers=headers).status_code, 401
                )
                self.assertEqual(
                    client.get(
                        "/auth/me",
                        headers={"Authorization": f"Bearer {b['access_token']}"},
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    client.post(
                        "/auth/refresh", json={"refresh_token": a["refresh_token"]}
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    client.post(
                        "/auth/refresh", json={"refresh_token": b["refresh_token"]}
                    ).status_code,
                    401,
                )

    def test_13_user_a_cannot_access_user_b_data(self):
        with _env():
            with self._client() as client:
                a = self._register(client, username="alice", email="a@example.com").json()
                b = self._register(client, username="bob", email="b@example.com").json()
                a_me = client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {a['access_token']}"},
                ).json()
                b_me = client.get(
                    "/auth/me",
                    headers={"Authorization": f"Bearer {b['access_token']}"},
                ).json()
                # Identity comes only from the token — A never sees B.
                self.assertEqual(a_me["id"], a["user"]["id"])
                self.assertEqual(b_me["id"], b["user"]["id"])
                self.assertNotEqual(a_me["id"], b_me["id"])
                self.assertEqual(a_me["username"], "alice")
                self.assertEqual(b_me["username"], "bob")
                # Forged token with B's sid but A's sub (or swapped) fails session check.
                forged = jwt.encode(
                    {
                        "sub": a["user"]["id"],
                        "sid": next(
                            sid
                            for sid, s in self.store.sessions.items()
                            if str(s["user_id"]) == b["user"]["id"]
                        ),
                        "typ": "access",
                        "aud": ACCESS_AUD,
                        "iat": int(datetime.now(timezone.utc).timestamp()),
                        "exp": int(
                            (datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()
                        ),
                    },
                    os.environ["AUTH_JWT_SECRET"],
                    algorithm="HS256",
                )
                bad = client.get(
                    "/auth/me", headers={"Authorization": f"Bearer {forged}"}
                )
                self.assertEqual(bad.status_code, 401)

    def test_14_rate_limiting(self):
        limiter = LoginRateLimiter(max_failures=3, window_seconds=600)
        with _env():
            with self._client() as client:
                self._register(client)
                with patch("shared.local_auth.service.LOGIN_RATE_LIMITER", limiter):
                    for _ in range(3):
                        resp = client.post(
                            "/auth/login",
                            json={"username": "alice", "password": "nope"},  # pragma: allowlist secret
                        )
                        self.assertEqual(resp.status_code, 401)
                    blocked = client.post(
                        "/auth/login",
                        json={"username": "alice", "password": "nope"},  # pragma: allowlist secret
                    )
                    self.assertEqual(blocked.status_code, 429)
                    still = client.post(
                        "/auth/login",
                        json={"username": "alice", "password": "password123"},  # pragma: allowlist secret
                    )
                    self.assertEqual(still.status_code, 429)

    def test_15_production_refuses_dev_auth_bypass(self):
        with _env(AUTH_MODE="dev", APP_ENV="production"):
            with self.assertRaises(RuntimeError) as ctx:
                assert_auth_mode_allowed()
            self.assertIn("AUTH_MODE=dev", str(ctx.exception))
        with _env(AUTH_MODE="dev", ENVIRONMENT="prod", APP_ENV=""):
            # ENVIRONMENT alone also counts when APP_ENV empty — auth.py ORs both.
            os.environ.pop("APP_ENV", None)
            os.environ["ENVIRONMENT"] = "prod"
            os.environ["AUTH_MODE"] = "dev"
            with self.assertRaises(RuntimeError):
                assert_auth_mode_allowed()
        with _env(AUTH_MODE="dev", APP_ENV="development"):
            assert_auth_mode_allowed()  # allowed


if __name__ == "__main__":
    unittest.main()
