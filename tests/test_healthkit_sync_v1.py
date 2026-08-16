"""HealthKit sync + status + bounded health tool — memory store + TestClient.

Run:  python -m unittest tests.test_healthkit_sync_v1 -v
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from shared.health import store as health_store
from shared.health.service import (
    MAX_HEALTH_CONTEXT_CHARS,
    MAX_SYNC_BATCH,
    normalize_terra_metrics,
    terra_external_id,
)
from shared.health.tools import run_get_recent_health_data
from shared.local_auth.store import use_memory_store

REPO = Path(__file__).resolve().parents[1]
MIGRATION_016 = REPO / "migrations" / "016_health_samples.sql"


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "self",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Migration016Tests(unittest.TestCase):
    def test_schema_references_public_users_and_dedupes(self):
        text = MIGRATION_016.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS health_samples", text)
        self.assertIn("CREATE TABLE IF NOT EXISTS health_sync_state", text)
        self.assertGreaterEqual(text.count("REFERENCES users(id) ON DELETE CASCADE"), 2)
        self.assertNotIn("REFERENCES auth.", text)
        self.assertIn("UNIQUE (user_id, provider, external_id)", text)
        self.assertIn("CHECK (end_at >= start_at)", text)
        self.assertIn("provider IN ('healthkit', 'terra')", text)
        self.assertIn("health_samples_user_type_start_idx", text)
        # health_metrics is deprecated in place, never dropped.
        self.assertIn("COMMENT ON TABLE health_metrics", text)
        self.assertNotIn("DROP TABLE", text)


class HealthKitSyncTests(unittest.TestCase):
    def setUp(self):
        use_memory_store(True)
        health_store.use_memory_store(True)
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)
        self.addCleanup(lambda: use_memory_store(False))
        self.addCleanup(lambda: health_store.use_memory_store(False))

    def _client(self):
        from main import app
        from routers.healthkit import router as healthkit_router

        # main.py mounts this router in the coordinator's commit; mounting here
        # keeps the suite green either way and is a no-op once it lands.
        if not any(
            getattr(route, "path", "").startswith("/healthkit") for route in app.routes
        ):
            app.include_router(healthkit_router)
        return TestClient(app)

    def _register(self, client, username: str, password: str = "password123"):  # pragma: allowlist secret
        return client.post(
            "/auth/register",
            json={"username": username, "password": password},
        )

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _two_users(self, client):
        a = self._register(client, "health_a").json()
        b = self._register(client, "health_b").json()
        return a["access_token"], b["access_token"]

    def _user_id(self, client, headers: dict) -> str:
        return client.get("/auth/protected", headers=headers).json()["user_id"]

    def _sample(self, **overrides) -> dict:
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        sample = {
            "sample_id": "sample-1",
            "type": "heart_rate",
            "start_at": _iso(start),
            "end_at": _iso(start),
            "value": 62.0,
            "unit": "count/min",
            "source_bundle": "com.apple.health",
            "source_name": "Test Watch",
        }
        sample.update(overrides)
        return sample

    def _sync(self, client, headers: dict, samples: list[dict]):
        return client.post("/healthkit/sync", headers=headers, json={"samples": samples})

    def test_sync_accepts_dedupes_and_updates(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sample = self._sample()

                first = self._sync(client, h, [sample])
                self.assertEqual(first.status_code, 200, first.text)
                body = first.json()
                self.assertEqual(body["accepted"], 1)
                self.assertEqual(body["updated"], 0)
                self.assertEqual(body["ignored"], 0)
                self.assertTrue(body["server_time"].endswith("Z"))

                # Same sample_id, identical payload → not double-counted.
                replay = self._sync(client, h, [sample]).json()
                self.assertEqual(replay["accepted"], 0)
                self.assertEqual(replay["updated"], 0)
                self.assertEqual(replay["ignored"], 1)

                # Same sample_id, changed value → update in place, no new row.
                changed = self._sync(client, h, [{**sample, "value": 71.0}]).json()
                self.assertEqual(changed["accepted"], 0)
                self.assertEqual(changed["updated"], 1)
                self.assertEqual(changed["ignored"], 0)

                rows = [
                    row
                    for key, row in health_store._memory.samples.items()
                    if key[2] == "sample-1"
                ]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["value"], 71.0)

                status = client.get("/healthkit/status", headers=h)
                self.assertEqual(status.status_code, 200)
                categories = status.json()["categories"]
                self.assertEqual(categories["heart_rate"]["count_last_30d"], 1)
                self.assertIsNotNone(categories["heart_rate"]["latest_sample_at"])
                self.assertEqual(categories["steps"]["count_last_30d"], 0)
                self.assertIsNone(categories["steps"]["latest_sample_at"])

    def test_duplicate_sample_id_within_one_batch_counts_once(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                body = self._sync(
                    client,
                    h,
                    [self._sample(value=60.0), self._sample(value=64.0)],
                ).json()
                self.assertEqual(body["accepted"], 1)
                self.assertEqual(body["ignored"], 1)
                self.assertEqual(len(health_store._memory.samples), 1)

    def test_units_are_canonicalized(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                start = datetime.now(timezone.utc) - timedelta(hours=2)
                body = self._sync(
                    client,
                    h,
                    [
                        self._sample(
                            sample_id="weight-1",
                            type="body_mass",
                            value=200.0,
                            unit="lb",
                            start_at=_iso(start),
                            end_at=_iso(start),
                        )
                    ],
                ).json()
                self.assertEqual(body["accepted"], 1)
                stored = next(iter(health_store._memory.samples.values()))
                self.assertEqual(stored["unit"], "kg")
                self.assertAlmostEqual(stored["value"], 90.718474, places=4)

    def test_malformed_and_unknown_samples_are_ignored_not_fatal(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                start = datetime.now(timezone.utc) - timedelta(hours=3)
                response = self._sync(
                    client,
                    h,
                    [
                        # Unknown type → ignored, not a 500.
                        self._sample(sample_id="unknown-1", type="blood_glucose"),
                        # Malformed unit for a known type → ignored.
                        self._sample(sample_id="bad-unit-1", unit="furlongs"),
                        # Missing unit on a numeric type → ignored.
                        self._sample(sample_id="no-unit-1", unit=None),
                        # end_at < start_at → ignored.
                        self._sample(
                            sample_id="backwards-1",
                            start_at=_iso(start),
                            end_at=_iso(start - timedelta(minutes=30)),
                        ),
                        # Unparseable timestamp → ignored.
                        self._sample(sample_id="bad-time-1", start_at="not-a-date"),
                        # One good sample still lands.
                        self._sample(sample_id="good-1", value=58.0),
                    ],
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["accepted"], 1)
                self.assertEqual(body["ignored"], 5)
                self.assertEqual(len(health_store._memory.samples), 1)

    def test_oversized_batch_rejected_with_400(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                samples = [
                    self._sample(sample_id=f"bulk-{i}")
                    for i in range(MAX_SYNC_BATCH + 1)
                ]
                response = self._sync(client, h, samples)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(str(MAX_SYNC_BATCH), response.json()["detail"])
                self.assertEqual(health_store._memory.samples, {})

    def test_unauthenticated_requests_rejected(self):
        with _env():
            with self._client() as client:
                self.assertEqual(
                    client.post(
                        "/healthkit/sync", json={"samples": [self._sample()]}
                    ).status_code,
                    401,
                )
                self.assertEqual(client.get("/healthkit/status").status_code, 401)
                self.assertEqual(
                    client.get(
                        "/healthkit/status",
                        headers={"Authorization": "Bearer not-a-real-token"},
                    ).status_code,
                    401,
                )
                self.assertEqual(health_store._memory.samples, {})

    def test_cross_user_isolation(self):
        with _env():
            with self._client() as client:
                token_a, token_b = self._two_users(client)
                ha, hb = self._auth(token_a), self._auth(token_b)
                user_a = self._user_id(client, ha)
                user_b = self._user_id(client, hb)

                self.assertEqual(
                    self._sync(client, ha, [self._sample(value=55.0)]).json()["accepted"],
                    1,
                )

                # Same sample_id from B is B's own row, not a hijack of A's.
                b_sync = self._sync(client, hb, [self._sample(value=99.0)]).json()
                self.assertEqual(b_sync["accepted"], 1)
                self.assertEqual(b_sync["updated"], 0)

                a_row = health_store._memory.samples[(user_a, "healthkit", "sample-1")]
                b_row = health_store._memory.samples[(user_b, "healthkit", "sample-1")]
                self.assertEqual(a_row["value"], 55.0)
                self.assertEqual(b_row["value"], 99.0)

                # Status is per-user; B never sees A's counts.
                status_b = client.get("/healthkit/status", headers=hb).json()
                self.assertEqual(status_b["categories"]["heart_rate"]["count_last_30d"], 1)

                # A user with no samples at all sees an empty, non-leaking status.
                token_c = self._register(client, "health_c").json()["access_token"]
                hc = self._auth(token_c)
                status_c = client.get("/healthkit/status", headers=hc).json()
                self.assertIsNone(status_c["last_synced_at"])
                for category in status_c["categories"].values():
                    self.assertEqual(category["count_last_30d"], 0)
                    self.assertIsNone(category["latest_sample_at"])

                # And the AI tool is scoped the same way.
                summary_c = asyncio.run(
                    run_get_recent_health_data(
                        self._user_id(client, hc), {"types": ["heart_rate"], "days": 30}
                    )
                )
                self.assertIn("no data", summary_c)
                self.assertNotIn("55", summary_c)
                self.assertNotIn("99", summary_c)

    def test_health_tool_returns_bounded_aggregates(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                user_id = self._user_id(client, h)

                base = datetime.now(timezone.utc) - timedelta(days=5)
                samples = []
                for day in range(5):
                    for reading in range(20):
                        when = base + timedelta(days=day, minutes=reading)
                        samples.append(
                            self._sample(
                                sample_id=f"hr-{day}-{reading}",
                                value=60.0 + reading,
                                start_at=_iso(when),
                                end_at=_iso(when),
                            )
                        )
                    when = base + timedelta(days=day, hours=12)
                    samples.append(
                        self._sample(
                            sample_id=f"steps-{day}",
                            type="steps",
                            value=8000.0 + day,
                            unit="count",
                            start_at=_iso(when),
                            end_at=_iso(when),
                        )
                    )
                self.assertEqual(
                    self._sync(client, h, samples).json()["accepted"], len(samples)
                )

                summary = asyncio.run(
                    run_get_recent_health_data(
                        user_id, {"types": ["heart_rate", "steps"], "days": 7}
                    )
                )
                self.assertLessEqual(len(summary), MAX_HEALTH_CONTEXT_CHARS)
                self.assertIn("heart_rate", summary)
                self.assertIn("steps", summary)
                self.assertIn("never diagnose", summary)
                # Aggregates only — no sample ids, no per-sample dump.
                self.assertNotIn("hr-0-0", summary)
                self.assertNotIn("sample_id", summary)
                self.assertLessEqual(summary.count("\n"), 6)

                # Window and type allowlist are clamped, never trusted raw.
                clamped = asyncio.run(
                    run_get_recent_health_data(
                        user_id, {"types": ["steps", "not_a_type"], "days": 900}
                    )
                )
                self.assertIn("last 30 day(s)", clamped)
                self.assertNotIn("not_a_type", clamped)

                rejected = asyncio.run(
                    run_get_recent_health_data(user_id, {"types": [], "days": 7})
                )
                self.assertTrue(rejected.startswith("Error:"))


class TerraNormalizationTests(unittest.TestCase):
    def test_unmapped_metrics_dropped_and_ids_are_deterministic(self):
        recorded = "2026-08-15T12:00:00Z"
        metrics = [
            {
                "metric_type": "heart_rate_data.avg_hr_bpm",
                "value": 64.0,
                "source_device": "oura",
                "recorded_at": recorded,
            },
            {
                "metric_type": "some_new_terra_field",
                "value": 1.0,
                "source_device": "oura",
                "recorded_at": recorded,
            },
            {
                "metric_type": "heart_rate_data",
                "value": None,
                "value_json": {"summary": {}},
                "source_device": "oura",
                "recorded_at": recorded,
            },
        ]
        samples, ignored = normalize_terra_metrics(metrics)
        self.assertEqual(len(samples), 1)
        self.assertEqual(ignored, 2)
        sample = samples[0]
        self.assertEqual(sample.sample_type, "heart_rate")
        self.assertEqual(sample.unit, "count/min")
        self.assertEqual(sample.start_at, sample.end_at)

        repeat, _ = normalize_terra_metrics(metrics)
        self.assertEqual(repeat[0].external_id, sample.external_id)
        self.assertEqual(
            sample.external_id,
            terra_external_id(
                metric_type="heart_rate_data.avg_hr_bpm",
                recorded_at=sample.start_at,
                source_device="oura",
                value=64.0,
            ),
        )


if __name__ == "__main__":
    unittest.main()
