"""HealthKit sync + status + bounded health tool — memory store + TestClient.

Run:  python -m unittest tests.test_healthkit_sync_v1 -v
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from shared.health import store as health_store
from shared.health.service import (
    HEALTHKIT_SYNC_MAX_BODY_BYTES,
    MAX_HEALTH_CONTEXT_CHARS,
    MAX_SYNC_BATCH,
    BatchTooLargeError,
    ingest_healthkit_samples,
    ingest_terra_metrics,
    normalize_terra_metrics,
    terra_external_id,
)
from shared.request_limits import HealthKitSyncBodyLimitMiddleware
from shared.health.tools import run_get_recent_health_data
from shared.health.types import ACCEPTED_UNITS, CANONICAL_UNITS, SAMPLE_TYPES
from shared.local_auth.store import use_memory_store

REPO = Path(__file__).resolve().parents[1]
MIGRATION_016 = REPO / "migrations" / "016_health_samples.sql"
CONTRACT_DOC = REPO / "docs" / "HEALTHKIT_SYNC_V1_CONTRACT.md"


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


class ContractDocTests(unittest.TestCase):
    """iOS builds against the doc, so the doc is checked against the models.

    Every claim asserted here is read out of the code, not restated: change a
    limit, a field name, an enum value or a status code and this fails.
    """

    @classmethod
    def setUpClass(cls):
        cls.doc = CONTRACT_DOC.read_text(encoding="utf-8")

    def _row(self, field: str) -> str:
        for line in self.doc.splitlines():
            if line.startswith(f"| {field} |"):
                return line
        self.fail(f"{field} has no row in the contract doc")

    def test_documented_endpoints_exist_exactly_as_spelled(self):
        from routers.healthkit import router

        paths = {route.path for route in router.routes}
        self.assertEqual(paths, {"/healthkit/sync", "/healthkit/status"})
        for path in paths:
            self.assertTrue(path in self.doc, f"{path} is not named in the doc")

    def test_doc_documents_every_request_field_and_its_length_limit(self):
        from routers.healthkit import HealthKitSampleIn

        documented_limits = {}
        for name, field in HealthKitSampleIn.model_fields.items():
            row = self._row(name)
            limit = next(
                (
                    meta.max_length
                    for meta in field.metadata
                    if getattr(meta, "max_length", None) is not None
                ),
                None,
            )
            if limit is not None:
                self.assertIn(
                    str(limit), row, f"{name} row omits its {limit}-char limit"
                )
                documented_limits[name] = limit
        # A field losing its bound silently is the failure mode that matters.
        self.assertEqual(
            documented_limits,
            {
                "sample_id": 200,
                "type": 64,
                "start_at": 64,
                "end_at": 64,
                "unit": 32,
                "value_text": 120,
                "source_bundle": 200,
                "source_name": 200,
            },
        )

    def test_doc_documents_every_response_field(self):
        from routers.healthkit import (
            HealthKitCategoryStatus,
            HealthKitStatusResponse,
            HealthKitSyncResponse,
        )

        for model in (
            HealthKitSyncResponse,
            HealthKitStatusResponse,
            HealthKitCategoryStatus,
        ):
            for name in model.model_fields:
                self.assertIn(name, self.doc, f"{model.__name__}.{name} undocumented")

    def test_doc_matches_the_closed_vocabulary_and_accepted_units(self):
        for sample_type in SAMPLE_TYPES:
            self.assertIn(f"`{sample_type}`", self.doc)
            row = self._row(sample_type)
            self.assertIn(CANONICAL_UNITS[sample_type], row)
            for unit in ACCEPTED_UNITS[sample_type]:
                self.assertIn(f"`{unit}`", row, f"{sample_type} omits unit {unit}")

    def test_doc_states_the_batch_cap_and_the_status_code_it_returns(self):
        self.assertIn(f"At most **{MAX_SYNC_BATCH} samples per request**", self.doc)
        self.assertIn(
            f"| samples | required array, **{MAX_SYNC_BATCH} items max**", self.doc
        )
        # The sample-count cap is a 422. 400 is reserved for an invalid
        # Content-Length; 413 is the 2 MiB body ceiling.
        self.assertIn("**422, not 400**", self.doc)
        self.assertIn("| 400 |", self.doc)
        self.assertIn("| 413 |", self.doc)
        self.assertIn("2 MiB", self.doc)
        self.assertIn(str(HEALTHKIT_SYNC_MAX_BODY_BYTES), self.doc)

    def test_doc_states_the_real_terra_id_length(self):
        digest = terra_external_id(
            metric_type="steps",
            recorded_at=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
            source_device="oura",
            value=9100.0,
        )
        self.assertIn(f"truncated to {len(digest)} hex characters", self.doc)


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

    def test_oversized_batch_rejected_without_building_the_samples(self):
        """422 from the field cap, before pydantic constructs a single sample.

        Every sample here is *also* individually invalid (empty sample_id, which
        the field's min_length rejects). If the cap were only enforced
        downstream in the service, the response would carry one per-item error
        per sample — proof each item was validated and allocated. With the cap
        on the field the list length is the only error reported.
        """
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                samples = [self._sample(sample_id="") for _ in range(MAX_SYNC_BATCH + 1)]

                response = self._sync(client, h, samples)

                self.assertEqual(response.status_code, 422, response.text)
                errors = response.json()["detail"]
                self.assertEqual(len(errors), 1, errors[:3])
                self.assertEqual(errors[0]["type"], "too_long")
                self.assertEqual(errors[0]["loc"], ["body", "samples"])
                self.assertEqual(errors[0]["ctx"]["max_length"], MAX_SYNC_BATCH)
                self.assertEqual(health_store._memory.samples, {})

    def test_rejection_does_not_echo_the_batch_back(self):
        """The 422 stays small instead of mirroring the oversized body.

        Pydantic attaches the offending input to the error, so an un-stripped
        handler answers a multi-megabyte batch with a multi-megabyte error.
        """
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                samples = [
                    self._sample(sample_id=f"bulk-{i}", source_name="X" * 180)
                    for i in range(MAX_SYNC_BATCH + 1)
                ]
                request_bytes = len(json.dumps({"samples": samples}))

                response = self._sync(client, h, samples)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertGreater(request_bytes, 200_000)
                self.assertLess(len(response.content), 1_000)
                self.assertNotIn("input", response.json()["detail"][0])
                self.assertNotIn("bulk-0", response.text)

    def test_batch_of_exactly_the_cap_is_accepted(self):
        """The cap is inclusive — 1000 samples must still sync."""
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                samples = [
                    self._sample(sample_id=f"bulk-{i}") for i in range(MAX_SYNC_BATCH)
                ]
                response = self._sync(client, h, samples)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["accepted"], MAX_SYNC_BATCH)

    def test_service_still_guards_the_batch_size_for_non_http_callers(self):
        """Defence in depth: the cap does not live only in the request model."""
        oversized = [
            self._sample(sample_id=f"bulk-{i}") for i in range(MAX_SYNC_BATCH + 1)
        ]
        with self.assertRaises(BatchTooLargeError):
            asyncio.run(
                ingest_healthkit_samples(
                    "00000000-0000-4000-8000-000000000001", oversized
                )
            )
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

    def test_metric_without_a_usable_timestamp_is_dropped(self):
        """No recorded_at → ignored, never stamped with now().

        The timestamp is hashed into the synthesized external_id, so inventing
        one gives the same metric a fresh id on every delivery. It would also
        record a reading at a time it was not taken.
        """
        for recorded_at in (None, "", "not-a-date", {}):
            with self.subTest(recorded_at=recorded_at):
                samples, ignored = normalize_terra_metrics(
                    [
                        {
                            "metric_type": "steps",
                            "value": 1200.0,
                            "source_device": "oura",
                            "recorded_at": recorded_at,
                        }
                    ]
                )
                self.assertEqual(samples, [])
                self.assertEqual(ignored, 1)


class TerraIngestIdempotencyTests(unittest.TestCase):
    def setUp(self):
        health_store.use_memory_store(True)
        self.addCleanup(lambda: health_store.use_memory_store(False))

    USER = "00000000-0000-4000-8000-0000000000aa"

    def _payload(self) -> list[dict]:
        return [
            {
                "metric_type": "steps",
                "value": 9100.0,
                "source_device": "oura",
                "recorded_at": "2026-08-15T12:00:00Z",
            },
            # Same shape, but Terra sent no timestamp for this one.
            {
                "metric_type": "heart_rate_data.avg_hr_bpm",
                "value": 64.0,
                "source_device": "oura",
                "recorded_at": None,
            },
        ]

    def test_replayed_webhook_writes_nothing_the_second_time(self):
        """`written` is 0 on replay — the claim the webhook docstring makes.

        The un-timestamped metric is what used to break this: a now() fallback
        hashed a new external_id on every delivery, so a replay inserted a
        second row and reported written=1.
        """
        first_written, first_ignored = asyncio.run(
            ingest_terra_metrics(self.USER, self._payload())
        )
        self.assertEqual(first_written, 1)
        self.assertEqual(first_ignored, 1)
        self.assertEqual(len(health_store._memory.samples), 1)

        replay_written, replay_ignored = asyncio.run(
            ingest_terra_metrics(self.USER, self._payload())
        )
        self.assertEqual(replay_written, 0)
        self.assertEqual(replay_ignored, 2)
        self.assertEqual(len(health_store._memory.samples), 1)


class _ProbeApp:
    """Inner ASGI app that records whether it was invoked."""

    def __init__(self):
        self.called = False
        self.bodies: list[bytes] = []

    async def __call__(self, scope, receive, send):
        self.called = True
        message = await receive()
        self.bodies.append(message.get("body", b""))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})


def _run_asgi(app, method, path, headers, chunks):
    """Drive a raw ASGI HTTP request; return (status, body_bytes)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    queue = list(chunks)
    status_holder = {"status": None, "body": b""}

    async def receive():
        if queue:
            body, more = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": more}
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]
        elif message["type"] == "http.response.body":
            status_holder["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return status_holder["status"], status_holder["body"]


class HealthKitBodyLimitMiddlewareTests(unittest.TestCase):
    """Prove the ASGI guard rejects before the inner app / JSON parser runs."""

    MARKER = b"SECRET_BODY_MARKER_SHOULD_NOT_ECHO"

    def _wrapped(self):
        inner = _ProbeApp()
        return HealthKitSyncBodyLimitMiddleware(inner), inner

    def test_oversized_content_length_is_413_and_never_calls_the_app(self):
        wrapped, inner = self._wrapped()
        with patch("json.loads") as loads:
            status, body = _run_asgi(
                wrapped,
                "POST",
                "/healthkit/sync",
                [
                    ("content-length", str(HEALTHKIT_SYNC_MAX_BODY_BYTES + 1)),
                    ("content-type", "application/json"),
                ],
                [(self.MARKER, False)],
            )
        self.assertEqual(status, 413)
        self.assertFalse(inner.called)
        loads.assert_not_called()
        self.assertNotIn(self.MARKER, body)

    def test_invalid_content_length_is_400_and_does_not_echo_the_body(self):
        wrapped, inner = self._wrapped()
        status, body = _run_asgi(
            wrapped,
            "POST",
            "/healthkit/sync",
            [("content-length", "not-a-number"), ("content-type", "application/json")],
            [(self.MARKER, False)],
        )
        self.assertEqual(status, 400)
        self.assertFalse(inner.called)
        self.assertNotIn(self.MARKER, body)

    def test_chunked_body_over_the_ceiling_is_413_without_calling_the_app(self):
        wrapped, inner = self._wrapped()
        chunk = b"x" * (HEALTHKIT_SYNC_MAX_BODY_BYTES // 2 + 1)
        with patch("json.loads") as loads:
            status, body = _run_asgi(
                wrapped,
                "POST",
                "/healthkit/sync",
                [("content-type", "application/json")],
                [(self.MARKER + chunk, True), (chunk, False)],
            )
        self.assertEqual(status, 413)
        self.assertFalse(inner.called)
        loads.assert_not_called()
        self.assertNotIn(self.MARKER, body)

    def test_body_just_under_the_ceiling_is_forwarded(self):
        wrapped, inner = self._wrapped()
        payload = b"y" * HEALTHKIT_SYNC_MAX_BODY_BYTES
        status, _body = _run_asgi(
            wrapped,
            "POST",
            "/healthkit/sync",
            [("content-type", "application/json")],
            [(payload, False)],
        )
        self.assertEqual(status, 200)
        self.assertTrue(inner.called)
        self.assertEqual(inner.bodies, [payload])

    def test_guard_is_path_scoped_so_chat_is_not_limited(self):
        wrapped, inner = self._wrapped()
        huge = b"z" * (HEALTHKIT_SYNC_MAX_BODY_BYTES + 4096)
        status, _body = _run_asgi(
            wrapped,
            "POST",
            "/chat",
            [("content-type", "application/json")],
            [(huge, False)],
        )
        self.assertEqual(status, 200)
        self.assertTrue(inner.called)

    def test_websocket_scopes_pass_through_untouched(self):
        seen = {"called": False}

        async def inner(scope, receive, send):
            seen["called"] = True
            self.assertEqual(scope["type"], "websocket")

        wrapped = HealthKitSyncBodyLimitMiddleware(inner)
        asyncio.run(wrapped({"type": "websocket", "path": "/chat/stream"}, None, None))
        self.assertTrue(seen["called"])


class HealthKitSyncBodyLimitRouteTests(unittest.TestCase):
    """TestClient proofs: 413 before parse, small sync still works, path-scoped."""

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
        a = self._register(client, "limit_a").json()
        b = self._register(client, "limit_b").json()
        return a["access_token"], b["access_token"]

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

    def test_huge_content_length_is_413_and_pydantic_never_runs(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                marker = "SECRET_BODY_MARKER_SHOULD_NOT_ECHO"
                oversized = (
                    marker.encode("utf-8")
                    + b"x" * (HEALTHKIT_SYNC_MAX_BODY_BYTES + 1 - len(marker))
                )
                with patch("json.loads") as loads:
                    with patch(
                        "routers.healthkit.HealthKitSyncRequest.model_validate"
                    ) as validate:
                        response = client.post(
                            "/healthkit/sync",
                            headers={
                                **h,
                                "Content-Type": "application/json",
                                "Content-Length": str(len(oversized)),
                            },
                            content=oversized,
                        )
                self.assertEqual(response.status_code, 413, response.text[:300])
                validate.assert_not_called()
                loads.assert_not_called()
                self.assertNotIn(marker, response.text)

    def test_chunked_oversize_body_is_413_without_parsing(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                marker = b"SECRET_BODY_MARKER_SHOULD_NOT_ECHO"

                def gen():
                    yield marker
                    yield b"x" * (HEALTHKIT_SYNC_MAX_BODY_BYTES // 2)
                    yield b"x" * (HEALTHKIT_SYNC_MAX_BODY_BYTES // 2 + 1)

                with patch("json.loads") as loads:
                    with patch(
                        "routers.healthkit.HealthKitSyncRequest.model_validate"
                    ) as validate:
                        response = client.post(
                            "/healthkit/sync",
                            headers={**h, "Content-Type": "application/json"},
                            content=gen(),
                        )
                self.assertEqual(response.status_code, 413, response.text[:300])
                validate.assert_not_called()
                loads.assert_not_called()
                self.assertNotIn(marker.decode("ascii"), response.text)

    def test_body_just_under_the_ceiling_with_one_sample_still_syncs(self):
        with _env():
            with self._client() as client:
                token, _ = self._two_users(client)
                h = self._auth(token)
                sample = self._sample(sample_id="under-ceiling")
                skeleton = json.dumps({"samples": [sample], "pad": ""})
                pad = HEALTHKIT_SYNC_MAX_BODY_BYTES - len(skeleton.encode("utf-8")) - 2
                self.assertGreater(pad, 0)
                payload = json.dumps({"samples": [sample], "pad": "x" * pad})
                self.assertLessEqual(len(payload.encode("utf-8")), HEALTHKIT_SYNC_MAX_BODY_BYTES)
                response = client.post(
                    "/healthkit/sync",
                    headers={**h, "Content-Type": "application/json"},
                    content=payload.encode("utf-8"),
                )
                self.assertEqual(response.status_code, 200, response.text[:300])
                self.assertEqual(response.json()["accepted"], 1)

    def test_ordinary_json_route_is_not_blocked_by_the_healthkit_ceiling(self):
        with _env():
            with self._client() as client:
                # Larger than a one-sample HealthKit payload; far under 2 MiB.
                padding = "n" * 8000
                response = client.post(
                    "/auth/register",
                    json={
                        "username": "ordinary_json_user",
                        "password": "password123",  # pragma: allowlist secret
                        "display_name": padding,
                    },
                )
                self.assertNotEqual(response.status_code, 413, response.text[:300])
                self.assertEqual(response.status_code, 200, response.text[:300])


if __name__ == "__main__":
    unittest.main()
