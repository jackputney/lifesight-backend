"""Staging/production startup guards + Terra webhook + /voice/speech.

Run:  python -m unittest tests.test_staging_hardening -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from shared.auth import assert_auth_mode_allowed, cors_allow_origins
from shared.local_auth.store import use_memory_store
from shared.terra import verify_webhook_signature


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "dev",
        "APP_ENV": "test",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "",
        "CORS_ALLOW_ORIGINS": "",
        "TERRA_WEBHOOK_SECRET": "",
        "ELEVENLABS_API_KEY": "",
        "ELEVENLABS_VOICE_ID": "",
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class StagingAuthGuardTests(unittest.TestCase):
    def test_staging_rejects_auth_mode_dev(self):
        with _env(AUTH_MODE="dev", APP_ENV="staging", CORS_ALLOW_ORIGINS="https://app.example"):
            with self.assertRaises(RuntimeError) as ctx:
                assert_auth_mode_allowed()
            self.assertIn("AUTH_MODE must be 'self'", str(ctx.exception))

    def test_production_rejects_auth_mode_dev(self):
        with _env(AUTH_MODE="dev", APP_ENV="production", CORS_ALLOW_ORIGINS="https://app.example"):
            with self.assertRaises(RuntimeError):
                assert_auth_mode_allowed()

    def test_staging_requires_self_and_jwt_secret(self):
        with _env(
            AUTH_MODE="self",
            APP_ENV="staging",
            AUTH_JWT_SECRET="",
            CORS_ALLOW_ORIGINS="https://app.example",
        ):
            with self.assertRaises(RuntimeError) as ctx:
                assert_auth_mode_allowed()
            self.assertIn("AUTH_JWT_SECRET", str(ctx.exception))

    def test_staging_self_with_secret_and_cors_ok(self):
        with _env(
            AUTH_MODE="self",
            APP_ENV="staging",
            AUTH_JWT_SECRET="staging-secret",  # pragma: allowlist secret
            CORS_ALLOW_ORIGINS="https://app.example,https://admin.example",
        ):
            assert_auth_mode_allowed()
            self.assertEqual(
                cors_allow_origins(),
                ["https://app.example", "https://admin.example"],
            )

    def test_staging_rejects_wildcard_cors(self):
        with _env(
            AUTH_MODE="self",
            APP_ENV="stage",
            AUTH_JWT_SECRET="staging-secret",  # pragma: allowlist secret
            CORS_ALLOW_ORIGINS="*",
        ):
            with self.assertRaises(RuntimeError) as ctx:
                assert_auth_mode_allowed()
            self.assertIn("CORS_ALLOW_ORIGINS", str(ctx.exception))

    def test_local_dev_allows_wildcard_default(self):
        with _env(AUTH_MODE="dev", APP_ENV="development", CORS_ALLOW_ORIGINS=""):
            assert_auth_mode_allowed()
            self.assertEqual(cors_allow_origins(), ["*"])

    def test_local_self_requires_jwt_secret(self):
        with _env(AUTH_MODE="self", APP_ENV="development", AUTH_JWT_SECRET=""):
            with self.assertRaises(RuntimeError) as ctx:
                assert_auth_mode_allowed()
            self.assertIn("AUTH_JWT_SECRET", str(ctx.exception))


class TerraWebhookHardeningTests(unittest.TestCase):
    def test_dev_allows_unsigned_when_secret_unset(self):
        with _env(AUTH_MODE="dev", APP_ENV="development", TERRA_WEBHOOK_SECRET=""):
            self.assertTrue(verify_webhook_signature(b"{}", None))

    def test_self_rejects_unsigned_when_secret_unset(self):
        with _env(AUTH_MODE="self", APP_ENV="development", TERRA_WEBHOOK_SECRET=""):
            self.assertFalse(verify_webhook_signature(b"{}", None))

    def test_staging_rejects_unsigned_when_secret_unset(self):
        with _env(
            AUTH_MODE="self",
            APP_ENV="staging",
            AUTH_JWT_SECRET="x",  # pragma: allowlist secret
            CORS_ALLOW_ORIGINS="https://app.example",
            TERRA_WEBHOOK_SECRET="",
        ):
            self.assertFalse(verify_webhook_signature(b"{}", "abc"))


class VoiceSpeechRouteTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
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

    def test_speech_requires_auth(self):
        with _env(AUTH_MODE="self"):
            with self._client() as client:
                resp = client.post("/voice/speech", json={"text": "Hello."})
        self.assertEqual(resp.status_code, 401)

    def test_speech_missing_elevenlabs_config(self):
        with _env(AUTH_MODE="dev", ELEVENLABS_API_KEY="", ELEVENLABS_VOICE_ID=""):
            with self._client() as client:
                resp = client.post(
                    "/voice/speech",
                    json={"text": "Opening Author."},
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 503)
        self.assertIn("ELEVENLABS", resp.json()["detail"])

    def test_speech_streams_mpeg(self):
        async def fake_stream(_text: str):
            async def _gen():
                yield b"ID3"
                yield b"fake-mp3-bytes"

            return _gen()

        with _env(
            AUTH_MODE="dev",
            ELEVENLABS_API_KEY="el-test-key",  # pragma: allowlist secret
            ELEVENLABS_VOICE_ID="voice-test-id",
        ):
            with patch(
                "routers.voice.stream_speech_mp3",
                new_callable=AsyncMock,
                side_effect=fake_stream,
            ):
                with self._client() as client:
                    resp = client.post(
                        "/voice/speech",
                        json={"text": "Opening Author."},
                        headers={"Authorization": "Bearer test"},
                    )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers.get("content-type"), "audio/mpeg")
        self.assertEqual(resp.content, b"ID3fake-mp3-bytes")

    def test_health_routes_still_ok(self):
        with _env(AUTH_MODE="dev"):
            with self._client() as client:
                self.assertEqual(client.get("/health").status_code, 200)
                # /health/db may 503 without a real pool — still must not crash.
                db = client.get("/health/db")
                self.assertIn(db.status_code, (200, 503))


if __name__ == "__main__":
    unittest.main()
