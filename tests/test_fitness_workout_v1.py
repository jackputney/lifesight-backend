"""Fitness workout V1 — memory store + TestClient, no network.

Run:  python -m unittest tests.test_fitness_workout_v1 -v
"""

from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from shared.auth import DEV_FAKE_USER_ID
from shared.fitness import progress, service, store
from shared.fitness.tools import run_fitness_tool
from shared.visual_panels import parse_exercise_panel_tool_input

REPO = Path(__file__).resolve().parents[1]
MIGRATION_019 = REPO / "migrations" / "019_fitness_workout_v1.sql"
CONTRACT = REPO / "docs" / "FITNESS_WORKOUT_V1_CONTRACT.md"
USER_B = "bbbbbbbb-0000-4000-8000-000000000002"

PLAN_BODY = {
    "title": "3-day strength",
    "activate": True,
    "days": [
        {
            "title": "Day A",
            "sort_order": 0,
            "exercises": [
                {
                    "name": "Bench Press",
                    "target_sets": 3,
                    "target_reps": 5,
                    "rest_seconds": 120,
                    "sort_order": 0,
                },
                {
                    "name": "Row",
                    "target_sets": 3,
                    "target_reps": 8,
                    "rest_seconds": 90,
                    "sort_order": 1,
                },
            ],
        },
        {
            "title": "Day B",
            "sort_order": 1,
            "exercises": [
                {
                    "name": "Squat",
                    "target_sets": 3,
                    "target_reps": 5,
                    "rest_seconds": 180,
                    "sort_order": 0,
                }
            ],
        },
    ],
}


def _env():
    return patch.dict(
        os.environ,
        {
            "AUTH_MODE": "dev",
            "APP_ENV": "test",
            "DATABASE_URL": "postgresql://unused:unused@localhost:1/unused",  # pragma: allowlist secret
            "ANTHROPIC_API_KEY": "",
            "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        },
        clear=False,
    )


def _headers():
    return {"Authorization": "Bearer test"}


class FitnessClientTests(unittest.TestCase):
    def setUp(self):
        store.use_memory_store()
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)
        self.addCleanup(lambda: store.use_memory_store(False))

    def _client(self):
        from main import app

        return TestClient(app)

    def _create_plan(self, client, body=None):
        resp = client.post("/workouts/plans", headers=_headers(), json=body or PLAN_BODY)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_plan_create_ordering_and_current(self):
        with _env():
            with self._client() as client:
                plan = self._create_plan(client)
                self.assertTrue(plan["is_active"])
                self.assertEqual([d["title"] for d in plan["days"]], ["Day A", "Day B"])
                self.assertEqual(
                    [e["name"] for e in plan["days"][0]["exercises"]],
                    ["Bench Press", "Row"],
                )
                current = client.get("/workouts/plans/current", headers=_headers())
                self.assertEqual(current.status_code, 200)
                self.assertEqual(current.json()["id"], plan["id"])
                listed = client.get("/workouts/plans", headers=_headers())
                self.assertEqual(len(listed.json()["plans"]), 1)

    def test_activate_keeps_historical_plan(self):
        with _env():
            with self._client() as client:
                first = self._create_plan(client)
                second_body = {
                    "title": "Replacement",
                    "activate": True,
                    "days": PLAN_BODY["days"],
                }
                second = self._create_plan(client, second_body)
                listed = client.get("/workouts/plans", headers=_headers()).json()["plans"]
                self.assertEqual(len(listed), 2)
                by_id = {p["id"]: p for p in listed}
                self.assertFalse(by_id[first["id"]]["is_active"])
                self.assertTrue(by_id[second["id"]]["is_active"])
                old = client.get(f"/workouts/plans/{first['id']}", headers=_headers())
                self.assertEqual(old.status_code, 200)

    def test_start_resume_and_conflict(self):
        with _env():
            with self._client() as client:
                plan = self._create_plan(client)
                day_a = plan["days"][0]["id"]
                day_b = plan["days"][1]["id"]
                first = client.post(
                    "/workouts/session/start",
                    headers=_headers(),
                    json={"plan_day_id": day_a},
                )
                self.assertEqual(first.status_code, 200, first.text)
                self.assertFalse(first.json()["resumed"])
                again = client.post(
                    "/workouts/session/start",
                    headers=_headers(),
                    json={"plan_day_id": day_a},
                )
                self.assertTrue(again.json()["resumed"])
                self.assertEqual(again.json()["session_id"], first.json()["session_id"])
                conflict = client.post(
                    "/workouts/session/start",
                    headers=_headers(),
                    json={"plan_day_id": day_b},
                )
                self.assertEqual(conflict.status_code, 409)

    def test_omitted_start_resumes_active_session(self):
        with _env():
            with self._client() as client:
                plan = self._create_plan(client)
                day_a = plan["days"][0]["id"]
                first = client.post(
                    "/workouts/session/start",
                    headers=_headers(),
                    json={"plan_day_id": day_a},
                )
                self.assertEqual(first.status_code, 200, first.text)
                omitted = client.post(
                    "/workouts/session/start",
                    headers=_headers(),
                    json={},
                )
                self.assertEqual(omitted.status_code, 200, omitted.text)
                self.assertTrue(omitted.json()["resumed"])
                self.assertEqual(
                    omitted.json()["session_id"], first.json()["session_id"]
                )

    def test_set_logging_order_complete_abandon_and_pr(self):
        with _env():
            with self._client() as client:
                plan = self._create_plan(client)
                bench = plan["days"][0]["exercises"][0]
                start = client.post(
                    "/workouts/session/start",
                    headers=_headers(),
                    json={"plan_day_id": plan["days"][0]["id"]},
                )
                session_id = start.json()["session_id"]
                self.assertEqual(start.json()["current_exercise"]["name"], "Bench Press")
                self.assertEqual(start.json()["current_set_number"], 1)

                logged = client.post(
                    f"/workouts/session/{session_id}/sets",
                    headers=_headers(),
                    json={"exercise_id": bench["id"], "reps": 5, "weight": 185},
                )
                self.assertEqual(logged.status_code, 200, logged.text)
                self.assertEqual(logged.json()["set"]["set_number"], 1)
                self.assertTrue(logged.json()["is_new_pr"])
                self.assertEqual(logged.json()["visual_panel"]["type"], "exercise")
                self.assertEqual(logged.json()["state"]["current_set_number"], 2)
                self.assertEqual(
                    logged.json()["visual_panel"]["data"]["current_set"],
                    logged.json()["state"]["current_set_number"],
                )

                dup = client.post(
                    f"/workouts/session/{session_id}/sets",
                    headers=_headers(),
                    json={
                        "exercise_id": bench["id"],
                        "set_number": 1,
                        "reps": 5,
                        "weight": 190,
                    },
                )
                self.assertEqual(dup.status_code, 409)

                lighter = client.post(
                    f"/workouts/session/{session_id}/sets",
                    headers=_headers(),
                    json={"exercise_id": bench["id"], "reps": 5, "weight": 175},
                )
                self.assertFalse(lighter.json()["is_new_pr"])

                five_pr = client.post(
                    f"/workouts/session/{session_id}/sets",
                    headers=_headers(),
                    json={"exercise_id": bench["id"], "reps": 1, "weight": 200},
                )
                self.assertTrue(five_pr.json()["is_new_pr"])
                prs = client.get("/workouts/personal-records", headers=_headers()).json()
                ranges = sorted(p["rep_range"] for p in prs["personal_records"])
                self.assertEqual(ranges, [1, 5])
                five = next(p for p in prs["personal_records"] if p["rep_range"] == 5)
                self.assertEqual(five["weight"], 185)

                done = client.post(
                    f"/workouts/session/{session_id}/complete",
                    headers=_headers(),
                )
                self.assertEqual(done.json()["status"], "completed")
                again = client.post(
                    f"/workouts/session/{session_id}/sets",
                    headers=_headers(),
                    json={"reps": 5, "weight": 185},
                )
                self.assertEqual(again.status_code, 409)

                start_b = client.post(
                    "/workouts/session/start",
                    headers=_headers(),
                    json={"plan_day_id": plan["days"][1]["id"]},
                )
                abandoned = client.post(
                    f"/workouts/session/{start_b.json()['session_id']}/abandon",
                    headers=_headers(),
                )
                self.assertEqual(abandoned.json()["status"], "abandoned")
                hist = client.get("/workouts/history", headers=_headers()).json()
                self.assertEqual(len(hist["sessions"]), 2)
                adherence = client.get("/workouts/adherence", headers=_headers()).json()
                self.assertEqual(adherence["sessions_completed"], 1)
                self.assertEqual(adherence["sessions_abandoned"], 1)
                self.assertNotIn("completion_rate", adherence)
                ex_hist = client.get(
                    f"/workouts/exercises/{bench['id']}/history",
                    headers=_headers(),
                )
                self.assertGreaterEqual(len(ex_hist.json()["sets"]), 3)
                self.assertIsNone(ex_hist.json()["weight_unit"])

    def test_cross_user_isolation_and_malformed_ids(self):
        async def seed_other():
            return await store.create_plan(
                USER_B,
                title="B's plan",
                notes=None,
                days=[
                    {
                        "title": "Secret",
                        "sort_order": 0,
                        "exercises": [
                            {
                                "name": "Secret Lift",
                                "target_sets": 1,
                                "target_reps": 1,
                                "rest_seconds": 0,
                                "sort_order": 0,
                                "notes": None,
                            }
                        ],
                    }
                ],
                activate=True,
            )

        with _env():
            with self._client() as client:
                other = asyncio.run(seed_other())
                other_id = str(other["id"])
                hidden = client.get(f"/workouts/plans/{other_id}", headers=_headers())
                self.assertEqual(hidden.status_code, 404)
                listed = client.get("/workouts/plans", headers=_headers()).json()["plans"]
                self.assertEqual(listed, [])
                for path in (
                    "/workouts/plans/not-a-uuid",
                    "/workouts/session/not-a-uuid/state",
                    "/workouts/session/not-a-uuid",
                    "/workouts/exercises/not-a-uuid/history",
                ):
                    resp = client.get(path, headers=_headers())
                    self.assertEqual(resp.status_code, 404, path)
                start = client.post(
                    "/workouts/session/start",
                    headers=_headers(),
                    json={"plan_day_id": "not-a-uuid"},
                )
                self.assertEqual(start.status_code, 404)

    def test_tools_are_owner_scoped_and_bounded(self):
        async def go():
            plan = await store.create_plan(
                DEV_FAKE_USER_ID,
                title="Mine",
                notes=None,
                days=[
                    {
                        "title": "A",
                        "sort_order": 0,
                        "exercises": [
                            {
                                "name": "Bench Press",
                                "target_sets": 2,
                                "target_reps": 5,
                                "rest_seconds": 60,
                                "sort_order": 0,
                                "notes": None,
                            }
                        ],
                    }
                ],
            )
            assembled = await store.assemble_plan(str(plan["id"]), DEV_FAKE_USER_ID)
            day_id = assembled["days"][0]["id"]
            await store.start_or_resume_session(DEV_FAKE_USER_ID, day_id)
            mine = await run_fitness_tool("get_current_workout_plan", DEV_FAKE_USER_ID, {})
            theirs = await run_fitness_tool("get_current_workout_plan", USER_B, {})
            self.assertIn("Bench Press", mine)
            self.assertIn("No active workout plan", theirs)
            checkins = await run_fitness_tool(
                "get_recent_checkins", DEV_FAKE_USER_ID, {"days": 7}
            )
            self.assertIn("daily_checkin", checkins)
            self.assertIn("Do not infer causation", checkins)

        with _env():
            __import__("asyncio").run(go())

    def test_panel_uses_session_state(self):
        async def go():
            plan = await store.create_plan(
                DEV_FAKE_USER_ID,
                title="Mine",
                notes=None,
                days=[
                    {
                        "title": "A",
                        "sort_order": 0,
                        "exercises": [
                            {
                                "name": "Bench Press",
                                "target_sets": 3,
                                "target_reps": 5,
                                "rest_seconds": 90,
                                "sort_order": 0,
                                "notes": "pause at chest",
                            }
                        ],
                    }
                ],
            )
            assembled = await store.assemble_plan(str(plan["id"]), DEV_FAKE_USER_ID)
            day_id = assembled["days"][0]["id"]
            ex_id = assembled["days"][0]["exercises"][0]["id"]
            session, _ = await store.start_or_resume_session(DEV_FAKE_USER_ID, day_id)
            await service.log_set(
                DEV_FAKE_USER_ID,
                str(session["id"]),
                exercise_id=ex_id,
                reps=5,
                weight=135,
            )
            data = parse_exercise_panel_tool_input(
                {
                    "exercise_id": ex_id,
                    "exercise_name": "Bench Press",
                    "sets": 99,
                    "reps": 99,
                    "rest_seconds": 1,
                    "current_set": 1,
                }
            )
            overlaid = await service.overlay_exercise_panel(DEV_FAKE_USER_ID, data)
            self.assertEqual(overlaid.sets, 3)
            self.assertEqual(overlaid.reps, 5)
            self.assertEqual(overlaid.rest_seconds, 90)
            self.assertEqual(overlaid.current_set, 2)
            prog = await progress.session_progress(session, DEV_FAKE_USER_ID)
            self.assertEqual(overlaid.current_set, prog["current_set_number"])

        with _env():
            __import__("asyncio").run(go())

    def test_panel_overlays_named_exercise_on_plan(self):
        async def go():
            plan = await store.create_plan(
                DEV_FAKE_USER_ID,
                title="Two lift",
                notes=None,
                days=[
                    {
                        "title": "A",
                        "sort_order": 0,
                        "exercises": [
                            {
                                "name": "Bench Press",
                                "target_sets": 3,
                                "target_reps": 5,
                                "rest_seconds": 90,
                                "sort_order": 0,
                            },
                            {
                                "name": "Row",
                                "target_sets": 3,
                                "target_reps": 8,
                                "rest_seconds": 60,
                                "sort_order": 1,
                            },
                        ],
                    }
                ],
            )
            assembled = await store.assemble_plan(str(plan["id"]), DEV_FAKE_USER_ID)
            day_id = assembled["days"][0]["id"]
            bench_id = assembled["days"][0]["exercises"][0]["id"]
            session, _ = await store.start_or_resume_session(DEV_FAKE_USER_ID, day_id)
            await service.log_set(
                DEV_FAKE_USER_ID,
                str(session["id"]),
                exercise_id=bench_id,
                reps=5,
                weight=135,
            )
            data = parse_exercise_panel_tool_input(
                {
                    "exercise_name": "Row",
                    "sets": 99,
                    "reps": 99,
                    "rest_seconds": 1,
                    "current_set": 9,
                }
            )
            overlaid = await service.overlay_exercise_panel(DEV_FAKE_USER_ID, data)
            self.assertEqual(overlaid.exercise_name, "Row")
            self.assertEqual(overlaid.sets, 3)
            self.assertEqual(overlaid.reps, 8)
            self.assertEqual(overlaid.rest_seconds, 60)
            self.assertEqual(overlaid.current_set, 1)

        with _env():
            __import__("asyncio").run(go())

    def test_prompt_tools_do_not_write_overrides(self):
        from modes.fitness.prompt import TOOLS

        names = {t["name"] for t in TOOLS}
        self.assertIn("log_workout_set", names)
        self.assertIn("get_recent_checkins", names)
        self.assertNotIn("apply_prompt_override", names)
        self.assertNotIn("write_user_prompt_overrides", names)


class Migration019Tests(unittest.TestCase):
    def test_additive_ownership_and_active_plan(self):
        sql = MIGRATION_019.read_text(encoding="utf-8")
        self.assertIn("workout_plans_one_active_per_user", sql)
        self.assertIn("workout_sessions_one_active_per_user", sql)
        self.assertIn("set_logs_session_exercise_set_uidx", sql)
        self.assertIn("workout_days_plan_user_fkey", sql)
        self.assertIn("planned_exercises_day_user_fkey", sql)
        self.assertIn("set_logs_session_user_fkey", sql)
        self.assertIn("personal_records_exercise_user_fkey", sql)
        self.assertNotIn("DROP TABLE", sql)
        self.assertNotIn("TRUNCATE", sql)
        self.assertIn("ranked_active_sessions", sql)
        self.assertIn("ranked_duplicate_sets", sql)


class ContractDocTests(unittest.TestCase):
    def test_doc_states_real_limits_and_no_e1rm(self):
        doc = CONTRACT.read_text(encoding="utf-8")
        self.assertIn("unitless", doc)
        self.assertIn("rep_range", doc)
        self.assertIn("409", doc)
        self.assertNotIn("e1RM is stored", doc)
        self.assertIn("completion_rate", doc)
        self.assertIn("present_exercise_panel", doc)
        self.assertIn("human admin review", doc)


class ProgressEngineTests(unittest.TestCase):
    def test_set_ordering_from_logs(self):
        exercises = [
            {"id": "e1", "name": "Bench", "target_sets": 3, "target_reps": 5, "rest_seconds": 60},
            {"id": "e2", "name": "Row", "target_sets": 2, "target_reps": 8, "rest_seconds": 60},
        ]
        logs = [
            {"exercise_id": "e1", "set_number": 1},
            {"exercise_id": "e1", "set_number": 2},
        ]
        prog = progress.progress_from_logs(exercises, logs)
        self.assertEqual(str(prog["current_exercise"]["id"]), "e1")
        self.assertEqual(prog["current_set_number"], 3)
        self.assertEqual(prog["remaining_sets_on_current"], 1)


if __name__ == "__main__":
    unittest.main()
