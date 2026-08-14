"""Personal context + daily check-in V1.

Run:  python -m unittest tests.test_personal_context_daily_checkin_v1 -v
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import MODE_REGISTRY, MODE_TOOLS, PUBLIC_MODE_IDS, ChatResponse, _build_system_prompt, app
from shared.client_actions import RefreshProfileAction, refresh_profile_action
from shared.daily_checkin import (
    DailyCheckinOut,
    DailyCheckinPatch,
    compact_checkin_for_context,
    resolve_local_date,
)
from shared.personal_context import apply_personal_context_update
from shared.profile_schema import (
    ProfileOut,
    ProfilePatch,
    compact_profile_for_context,
    empty_profile,
)

REPO = Path(__file__).resolve().parents[1]
DEV_USER = "00000000-0000-4000-8000-000000000001"
MIGRATION_013 = REPO / "migrations" / "013_personal_context_daily_checkin.sql"


def _env(**overrides: str):
    base = {
        "AUTH_MODE": "dev",
        "APP_ENV": "test",
        "AUTH_JWT_SECRET": "test-jwt-secret-not-for-production",  # pragma: allowlist secret
        "DATABASE_URL": "postgresql://unused:unused@localhost:5432/unused",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "unittest-placeholder",  # pragma: allowlist secret
    }
    base.update(overrides)
    return patch.dict(os.environ, base, clear=False)


class Migration013Tests(unittest.TestCase):
    def test_migration_exists_and_is_additive(self):
        self.assertTrue(MIGRATION_013.is_file())
        sql = MIGRATION_013.read_text(encoding="utf-8")
        for col in (
            "occupation",
            "industry",
            "education_context",
            "interests",
            "typical_schedule",
        ):
            self.assertIn(col, sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS daily_checkins", sql)
        self.assertIn("'checkin'", sql)
        matches = list((REPO / "migrations").glob("013_*.sql"))
        self.assertEqual(len(matches), 1, matches)


class PersonalContextSchemaTests(unittest.TestCase):
    def test_patch_accepts_personal_context(self):
        patch = ProfilePatch.model_validate(
            {
                "occupation": "Software engineer",
                "industry": "Technology",
                "education_context": "NYU student",
                "interests": ["lifting", "AI", "hiking"],
                "typical_schedule": "Usually work 9-5 weekdays",
            }
        )
        self.assertEqual(patch.occupation, "Software engineer")
        self.assertEqual(patch.interests, ["lifting", "AI", "hiking"])

    def test_interests_max_and_item_length(self):
        with self.assertRaises(ValidationError):
            ProfilePatch.model_validate({"interests": ["x" * 121]})
        with self.assertRaises(ValidationError):
            ProfilePatch.model_validate({"interests": [f"i{i}" for i in range(21)]})

    def test_string_limits(self):
        with self.assertRaises(ValidationError):
            ProfilePatch.model_validate({"occupation": "x" * 121})
        with self.assertRaises(ValidationError):
            ProfilePatch.model_validate({"education_context": "x" * 241})
        with self.assertRaises(ValidationError):
            ProfilePatch.model_validate({"typical_schedule": "x" * 501})

    def test_empty_profile_has_empty_interests(self):
        p = empty_profile(DEV_USER)
        self.assertEqual(p.interests, [])
        self.assertIsNone(p.occupation)

    def test_compact_omits_null_personal_context(self):
        text = compact_profile_for_context(empty_profile(DEV_USER))
        self.assertNotIn("occupation", text)
        self.assertNotIn("interests", text)
        filled = compact_profile_for_context(
            ProfileOut(
                user_id=DEV_USER,
                occupation="Engineer",
                interests=["hiking"],
            )
        )
        self.assertIn("occupation: Engineer", filled)
        self.assertIn("interests: hiking", filled)


class PersonalContextToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_explicit_consent(self):
        text, changed = await apply_personal_context_update(
            DEV_USER, {"occupation": "Engineer", "explicit_consent": False}
        )
        self.assertIn("explicit_consent", text)
        self.assertFalse(changed)

    async def test_explicit_remember_writes(self):
        with patch(
            "shared.personal_context.get_profile",
            new_callable=AsyncMock,
            return_value=empty_profile(DEV_USER),
        ), patch(
            "shared.personal_context.patch_profile",
            new_callable=AsyncMock,
            return_value=empty_profile(DEV_USER),
        ) as mock_patch:
            text, changed = await apply_personal_context_update(
                DEV_USER,
                {
                    "occupation": "Software engineer",
                    "explicit_consent": True,
                },
            )
        self.assertTrue(changed)
        self.assertIn("occupation", text)
        mock_patch.assert_awaited_once()
        patch_arg = mock_patch.await_args.args[1]
        self.assertEqual(patch_arg.occupation, "Software engineer")

    async def test_conflict_blocks_overwrite_without_replace(self):
        current = ProfileOut(user_id=DEV_USER, occupation="Teacher")
        with patch(
            "shared.personal_context.get_profile",
            new_callable=AsyncMock,
            return_value=current,
        ), patch(
            "shared.personal_context.patch_profile",
            new_callable=AsyncMock,
        ) as mock_patch:
            text, changed = await apply_personal_context_update(
                DEV_USER,
                {
                    "occupation": "Software engineer",
                    "explicit_consent": True,
                },
            )
        self.assertFalse(changed)
        self.assertIn("conflict", text.lower())
        mock_patch.assert_not_awaited()

    async def test_replace_existing_allows_overwrite_after_confirm(self):
        current = ProfileOut(user_id=DEV_USER, occupation="Teacher")
        with patch(
            "shared.personal_context.get_profile",
            new_callable=AsyncMock,
            return_value=current,
        ), patch(
            "shared.personal_context.patch_profile",
            new_callable=AsyncMock,
            return_value=current,
        ) as mock_patch:
            text, changed = await apply_personal_context_update(
                DEV_USER,
                {
                    "occupation": "Software engineer",
                    "explicit_consent": True,
                    "replace_existing": True,
                },
            )
        self.assertTrue(changed)
        self.assertIn("saved", text.lower())
        mock_patch.assert_awaited_once()


class RefreshProfileClientActionTests(unittest.TestCase):
    def test_wire_shape(self):
        payload = ChatResponse(
            reply="Got it — I'll remember that.",
            mode="fitness",
            conversation_id="00000000-0000-4000-8000-000000000099",
            client_actions=[refresh_profile_action()],
        ).model_dump(mode="json")
        self.assertEqual(
            payload["client_actions"],
            [{"type": "refresh_profile"}],
        )
        self.assertIsInstance(RefreshProfileAction(), RefreshProfileAction)


class DailyCheckinUnitTests(unittest.TestCase):
    def test_resolve_local_date_timezone(self):
        # 2026-08-14 02:00 UTC → still 2026-08-13 in America/Los_Angeles
        now = datetime(2026, 8, 14, 2, 0, tzinfo=timezone.utc)
        local, tz = resolve_local_date(timezone_name="America/Los_Angeles", now=now)
        self.assertEqual(local, date(2026, 8, 13))
        self.assertEqual(tz, "America/Los_Angeles")

    def test_resolve_invalid_timezone_falls_back_utc(self):
        local, tz = resolve_local_date(
            timezone_name="Not/AZone",
            now=datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(local, date(2026, 8, 13))
        self.assertEqual(tz, "UTC")

    def test_structured_validation(self):
        ok = DailyCheckinPatch.model_validate(
            {"energy": 3, "sleep_hours": 6.5, "summary": "Low energy day"}
        )
        self.assertEqual(ok.energy, 3)
        with self.assertRaises(ValidationError):
            DailyCheckinPatch.model_validate({"energy": 6})
        with self.assertRaises(ValidationError):
            DailyCheckinPatch.model_validate({"sleep_hours": 25})

    def test_compact_checkin_bounded(self):
        empty = compact_checkin_for_context(None)
        self.assertEqual(empty, "")
        not_started = compact_checkin_for_context(
            DailyCheckinOut(
                user_id=DEV_USER,
                local_date="2026-08-13",
                timezone="UTC",
                status="not_started",
            )
        )
        self.assertEqual(not_started, "")
        completed = compact_checkin_for_context(
            DailyCheckinOut(
                user_id=DEV_USER,
                local_date="2026-08-13",
                timezone="UTC",
                status="completed",
                energy=2,
                summary="Under-recovered from poor sleep",
            )
        )
        self.assertIn("energy: 2", completed)
        self.assertIn("Under-recovered", completed)


class DailyCheckinStartIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_returns_completed(self):
        from shared.daily_checkin import start_today_checkin

        completed = {
            "id": "00000000-0000-4000-8000-0000000000aa",
            "local_date": date(2026, 8, 13),
            "timezone": "UTC",
            "conversation_id": "00000000-0000-4000-8000-0000000000bb",
            "status": "completed",
            "summary": "Done",
        }
        with patch(
            "shared.daily_checkin.get_profile",
            new_callable=AsyncMock,
            return_value=ProfileOut(user_id=DEV_USER, timezone="UTC"),
        ), patch(
            "shared.daily_checkin.resolve_local_date",
            return_value=(date(2026, 8, 13), "UTC"),
        ), patch(
            "shared.db.get_daily_checkin",
            new_callable=AsyncMock,
            return_value=completed,
        ), patch(
            "shared.db.create_conversation",
            new_callable=AsyncMock,
        ) as mock_create:
            out = await start_today_checkin(DEV_USER)
        self.assertEqual(out.status, "completed")
        mock_create.assert_not_awaited()

    async def test_in_progress_resumes(self):
        from shared.daily_checkin import start_today_checkin

        existing = {
            "id": "00000000-0000-4000-8000-0000000000aa",
            "local_date": date(2026, 8, 13),
            "timezone": "UTC",
            "conversation_id": "00000000-0000-4000-8000-0000000000cc",
            "status": "in_progress",
        }
        with patch(
            "shared.daily_checkin.get_profile",
            new_callable=AsyncMock,
            return_value=ProfileOut(user_id=DEV_USER, timezone="UTC"),
        ), patch(
            "shared.daily_checkin.resolve_local_date",
            return_value=(date(2026, 8, 13), "UTC"),
        ), patch(
            "shared.db.get_daily_checkin",
            new_callable=AsyncMock,
            return_value=existing,
        ), patch(
            "shared.db.create_conversation",
            new_callable=AsyncMock,
        ) as mock_create:
            out = await start_today_checkin(DEV_USER)
        self.assertEqual(out.status, "in_progress")
        self.assertEqual(out.conversation_id, existing["conversation_id"])
        mock_create.assert_not_awaited()


class ModeAndPromptTests(unittest.TestCase):
    def test_checkin_hidden_from_public_modes(self):
        self.assertIn("checkin", MODE_REGISTRY)
        self.assertNotIn("checkin", PUBLIC_MODE_IDS)

    def test_enrichment_policy_in_fitness_not_checkin(self):
        fitness = _build_system_prompt("fitness", profile_block="User profile:\n- x")
        self.assertIn("Personal-context enrichment", fitness)
        checkin = _build_system_prompt("checkin", profile_block="User profile:\n- x")
        self.assertNotIn("Personal-context enrichment", checkin)

    def test_fitness_includes_checkin_block(self):
        text = _build_system_prompt(
            "fitness",
            profile_block="User profile:\n- occupation: Eng",
            checkin_block="Today's daily check-in:\n- energy: 2",
        )
        self.assertIn("energy: 2", text)

    def test_tools(self):
        fitness_names = [t["name"] for t in MODE_TOOLS["fitness"]]
        self.assertIn("update_personal_context", fitness_names)
        self.assertEqual(
            [t["name"] for t in MODE_TOOLS["checkin"]],
            ["update_daily_checkin"],
        )


class ProfileRoutePersonalContextTests(unittest.TestCase):
    def setUp(self):
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)

    def test_get_profile_includes_personal_context_defaults(self):
        with _env(), patch(
            "routers.profile.get_profile",
            new_callable=AsyncMock,
            return_value=empty_profile(DEV_USER),
        ):
            with TestClient(app) as client:
                resp = client.get("/profile", headers={"Authorization": "Bearer test"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["interests"], [])
        self.assertIsNone(body["occupation"])

    def test_patch_personal_context(self):
        after = ProfileOut(
            user_id=DEV_USER,
            occupation="Software engineer",
            interests=["hiking"],
        )
        with _env(), patch(
            "routers.profile.patch_profile",
            new_callable=AsyncMock,
            return_value=after,
        ) as mock_patch:
            with TestClient(app) as client:
                resp = client.patch(
                    "/profile",
                    headers={"Authorization": "Bearer test"},
                    json={
                        "occupation": "Software engineer",
                        "interests": ["hiking"],
                    },
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["occupation"], "Software engineer")
        mock_patch.assert_awaited_once()


class DailyCheckinRouteTests(unittest.TestCase):
    def setUp(self):
        self._pool_init = patch("shared.db.init_pool", new_callable=AsyncMock)
        self._pool_close = patch("shared.db.close_pool", new_callable=AsyncMock)
        self._pool_init.start()
        self._pool_close.start()
        self.addCleanup(self._pool_init.stop)
        self.addCleanup(self._pool_close.stop)

    def test_today_not_started(self):
        out = DailyCheckinOut(
            user_id=DEV_USER,
            local_date="2026-08-13",
            timezone="America/Los_Angeles",
            status="not_started",
        )
        with _env(), patch(
            "routers.daily_checkin.get_today_checkin",
            new_callable=AsyncMock,
            return_value=out,
        ):
            with TestClient(app) as client:
                resp = client.get(
                    "/daily-checkin/today",
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "not_started")

    def test_start_returns_conversation(self):
        out = DailyCheckinOut(
            user_id=DEV_USER,
            local_date="2026-08-13",
            timezone="UTC",
            status="in_progress",
            conversation_id="00000000-0000-4000-8000-0000000000dd",
        )
        with _env(), patch(
            "routers.daily_checkin.start_today_checkin",
            new_callable=AsyncMock,
            return_value=out,
        ):
            with TestClient(app) as client:
                resp = client.post(
                    "/daily-checkin/start",
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "in_progress")
        self.assertEqual(
            body["conversation_id"],
            "00000000-0000-4000-8000-0000000000dd",
        )


class SeedScriptPersonalContextTests(unittest.TestCase):
    def test_seed_parser_accepts_personal_context_flags(self):
        path = REPO / "scripts" / "seed_user_profile.py"
        spec = importlib.util.spec_from_file_location("seed_user_profile", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        parser = mod.build_parser()
        args = parser.parse_args(
            [
                "--user-id",
                DEV_USER,
                "--occupation",
                "Engineer",
                "--interests",
                "hiking,AI",
            ]
        )
        self.assertEqual(args.occupation, "Engineer")
        self.assertEqual(args.interests, "hiking,AI")


class RunToolPersonalContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_update_emits_refresh_profile(self):
        from main import _run_tool

        with patch(
            "main.apply_personal_context_update",
            new_callable=AsyncMock,
            return_value=("Personal context saved (occupation).", True),
        ):
            text, pending, panel, actions = await _run_tool(
                "update_personal_context",
                {"occupation": "Engineer", "explicit_consent": True},
                user_id=DEV_USER,
                conversation_id="00000000-0000-4000-8000-000000000099",
                mode="fitness",
            )
        self.assertIn("saved", text.lower())
        self.assertIsNone(pending)
        self.assertIsNone(panel)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "refresh_profile")

    async def test_no_change_emits_no_client_action(self):
        from main import _run_tool

        with patch(
            "main.apply_personal_context_update",
            new_callable=AsyncMock,
            return_value=("No profile changes needed (values already match).", False),
        ):
            _text, _p, _panel, actions = await _run_tool(
                "update_personal_context",
                {"occupation": "Engineer", "explicit_consent": True},
                user_id=DEV_USER,
                conversation_id="00000000-0000-4000-8000-000000000099",
                mode="fitness",
            )
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
