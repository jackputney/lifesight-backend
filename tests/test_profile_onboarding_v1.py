"""Adaptive onboarding profile fields V1.

Run:  python -m unittest tests.test_profile_onboarding_v1 -v
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from shared.profile_schema import (
    MAX_PRIMARY_GOALS,
    PRIMARY_GOAL_VALUES,
    TRAINING_FREQUENCY_VALUES,
    ProfileOut,
    ProfilePatch,
    compact_profile_for_context,
    empty_profile,
)
from shared.profile_service import row_to_profile

REPO = Path(__file__).resolve().parents[1]
MIGRATION_012 = REPO / "migrations" / "012_profile_onboarding_fields.sql"
DEV_USER = "00000000-0000-4000-8000-000000000001"


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


def _load_seed_module():
    path = REPO / "scripts" / "seed_user_profile.py"
    spec = importlib.util.spec_from_file_location("seed_user_profile", path)
    seed = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(seed)
    return seed


class Migration012StaticTests(unittest.TestCase):
    def test_migration_012_exists_and_is_additive(self):
        self.assertTrue(MIGRATION_012.is_file())
        sql = MIGRATION_012.read_text(encoding="utf-8")
        for col in (
            "preferred_units",
            "training_environment",
            "typical_session_minutes",
            "sex_for_physiological_calculations",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {col}", sql)
        self.assertNotIn("DROP COLUMN", sql)
        self.assertIn("imperial", sql)
        self.assertIn("commercial_gym", sql)
        self.assertIn("typical_session_minutes >= 10", sql)
        self.assertIn("unspecified", sql)

    def test_no_duplicate_migration_012(self):
        matches = list((REPO / "migrations").glob("012_*.sql"))
        self.assertEqual(len(matches), 1, matches)


class LegacyNullDecodeTests(unittest.TestCase):
    def test_empty_profile_decodes(self):
        p = empty_profile(DEV_USER)
        self.assertEqual(p.primary_goals, [])
        self.assertIsNone(p.preferred_units)
        self.assertIsNone(p.training_environment)
        self.assertIsNone(p.typical_session_minutes)
        self.assertIsNone(p.sex_for_physiological_calculations)
        self.assertIsNone(p.training_frequency)

    def test_row_missing_new_columns_decodes(self):
        # Pre-012 / sparse rows: only legacy keys present.
        profile = row_to_profile(
            {
                "timezone": "America/Los_Angeles",
                "primary_goals": ["legacy_hypertrophy"],
                "training_frequency": "3x week",
            },
            display_name=None,
            user_id=DEV_USER,
        )
        self.assertEqual(profile.primary_goals, ["legacy_hypertrophy"])
        self.assertEqual(profile.training_frequency, "3x week")
        self.assertIsNone(profile.preferred_units)
        self.assertIsNone(profile.training_environment)
        self.assertIsNone(profile.typical_session_minutes)
        self.assertIsNone(profile.sex_for_physiological_calculations)

    def test_null_new_columns_in_row(self):
        profile = row_to_profile(
            {
                "preferred_units": None,
                "training_environment": None,
                "typical_session_minutes": None,
                "sex_for_physiological_calculations": None,
                "primary_goals": None,
            },
            display_name="Tester",
            user_id=DEV_USER,
        )
        self.assertEqual(profile.display_name, "Tester")
        self.assertEqual(profile.primary_goals, [])
        self.assertIsNone(profile.preferred_units)


class ProfilePatchValidationTests(unittest.TestCase):
    def test_canonical_goal_set(self):
        self.assertEqual(
            PRIMARY_GOAL_VALUES,
            frozenset(
                {
                    "build_muscle",
                    "get_stronger",
                    "lose_body_fat",
                    "improve_endurance",
                    "general_fitness",
                    "longevity_health",
                    "track_nutrition",
                    "return_to_training",
                    "better_habits",
                }
            ),
        )

    def test_canonical_frequency_set(self):
        self.assertEqual(
            TRAINING_FREQUENCY_VALUES,
            frozenset({"0_1", "2", "3", "4", "5", "6_plus"}),
        )

    def test_ordered_goals_max_three(self):
        ok = ProfilePatch(
            primary_goals=["build_muscle", "get_stronger", "better_habits"]
        )
        self.assertEqual(len(ok.primary_goals or []), MAX_PRIMARY_GOALS)
        with self.assertRaises(ValidationError):
            ProfilePatch(
                primary_goals=[
                    "build_muscle",
                    "get_stronger",
                    "lose_body_fat",
                    "general_fitness",
                ]
            )

    def test_unknown_goal_rejected_on_patch(self):
        with self.assertRaises(ValidationError):
            ProfilePatch(primary_goals=["hypertrophy"])

    def test_legacy_goal_still_on_get_model(self):
        out = ProfileOut(user_id=DEV_USER, primary_goals=["hypertrophy", "strength"])
        self.assertEqual(out.primary_goals, ["hypertrophy", "strength"])

    def test_training_frequency_enum(self):
        for value in TRAINING_FREQUENCY_VALUES:
            self.assertEqual(
                ProfilePatch(training_frequency=value).training_frequency, value
            )
        with self.assertRaises(ValidationError):
            ProfilePatch(training_frequency="3x week")

    def test_preferred_units_enum(self):
        self.assertEqual(
            ProfilePatch(preferred_units="imperial").preferred_units, "imperial"
        )
        with self.assertRaises(ValidationError):
            ProfilePatch(preferred_units="us")

    def test_training_environment_enum(self):
        self.assertEqual(
            ProfilePatch(training_environment="home_gym").training_environment,
            "home_gym",
        )
        with self.assertRaises(ValidationError):
            ProfilePatch(training_environment="garage")

    def test_session_minute_bounds(self):
        self.assertEqual(
            ProfilePatch(typical_session_minutes=10).typical_session_minutes, 10
        )
        self.assertEqual(
            ProfilePatch(typical_session_minutes=300).typical_session_minutes, 300
        )
        with self.assertRaises(ValidationError):
            ProfilePatch(typical_session_minutes=9)
        with self.assertRaises(ValidationError):
            ProfilePatch(typical_session_minutes=301)

    def test_sex_enum_optional(self):
        self.assertEqual(
            ProfilePatch(sex_for_physiological_calculations="unspecified")
            .sex_for_physiological_calculations,
            "unspecified",
        )
        with self.assertRaises(ValidationError):
            ProfilePatch(sex_for_physiological_calculations="nonbinary")


class CompactProfileContextTests(unittest.TestCase):
    def test_omits_nulls_and_orders_goals(self):
        text = compact_profile_for_context(
            ProfileOut(
                user_id=DEV_USER,
                preferred_units="metric",
                experience_level="intermediate",
                primary_goals=["build_muscle", "better_habits"],
                training_environment="commercial_gym",
                available_equipment=["barbell"],
                sex_for_physiological_calculations="unspecified",
            )
        )
        self.assertIn("preferred_units: metric", text)
        self.assertIn("primary_goals (ordered): build_muscle, better_habits", text)
        self.assertIn("training_environment: commercial_gym", text)
        self.assertIn("available_equipment: barbell", text)
        self.assertNotIn("sex_for_physiological_calculations", text)
        self.assertNotIn("timezone:", text)

    def test_sex_included_only_when_male_or_female(self):
        male = compact_profile_for_context(
            ProfileOut(
                user_id=DEV_USER,
                sex_for_physiological_calculations="male",
            )
        )
        self.assertIn("sex_for_physiological_calculations: male", male)
        self.assertIn("formula/reference use only", male)
        self.assertIn("not gender identity", male)

        empty = compact_profile_for_context(empty_profile(DEV_USER))
        self.assertIn("(no profile details on file)", empty)


class SeedScriptOnboardingTests(unittest.TestCase):
    def test_refuses_production(self):
        seed = _load_seed_module()
        with patch.dict(
            os.environ,
            {"APP_ENV": "production", "ALLOW_PROFILE_SEED_IN_PRODUCTION": ""},
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                seed._refuse_production()

    def test_parser_accepts_new_fields(self):
        seed = _load_seed_module()
        args = seed.build_parser().parse_args(
            [
                "--user-id",
                DEV_USER,
                "--preferred-units",
                "imperial",
                "--training-environment",
                "mixed",
                "--typical-session-minutes",
                "45",
                "--sex-for-physiological-calculations",
                "female",
                "--training-frequency",
                "6_plus",
                "--primary-goals",
                "build_muscle,track_nutrition",
            ]
        )
        self.assertEqual(args.preferred_units, "imperial")
        self.assertEqual(args.training_environment, "mixed")
        self.assertEqual(args.typical_session_minutes, 45)
        self.assertEqual(args.sex_for_physiological_calculations, "female")
        self.assertEqual(args.training_frequency, "6_plus")
        self.assertEqual(args.primary_goals, "build_muscle,track_nutrition")

    def test_seed_patch_validates_via_schema(self):
        seed = _load_seed_module()
        with self.assertRaises(ValidationError):
            ProfilePatch.model_validate(
                {
                    "preferred_units": "imperial",
                    "typical_session_minutes": 5,
                }
            )
        # Ensure seed module still imports ProfilePatch for validation path.
        self.assertTrue(hasattr(seed, "ProfilePatch"))


class ProfileRouteOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch("shared.db.init_pool", new_callable=AsyncMock),
            patch("shared.db.close_pool", new_callable=AsyncMock),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_get_includes_new_null_fields(self):
        with _env():
            with patch(
                "routers.profile.get_profile",
                new_callable=AsyncMock,
                return_value=empty_profile(DEV_USER),
            ):
                with TestClient(app) as client:
                    resp = client.get(
                        "/profile", headers={"Authorization": "Bearer test"}
                    )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertIsNone(body["preferred_units"])
        self.assertIsNone(body["training_environment"])
        self.assertIsNone(body["typical_session_minutes"])
        self.assertIsNone(body["sex_for_physiological_calculations"])

    def test_patch_all_four_new_fields(self):
        updated = ProfileOut(
            user_id=DEV_USER,
            preferred_units="metric",
            training_environment="limited_equipment",
            typical_session_minutes=40,
            sex_for_physiological_calculations="male",
            primary_goals=["general_fitness"],
            training_frequency="2",
        )
        with _env():
            with patch(
                "routers.profile.patch_profile",
                new_callable=AsyncMock,
                return_value=updated,
            ) as mocked:
                with TestClient(app) as client:
                    resp = client.patch(
                        "/profile",
                        headers={"Authorization": "Bearer test"},
                        json={
                            "preferred_units": "metric",
                            "training_environment": "limited_equipment",
                            "typical_session_minutes": 40,
                            "sex_for_physiological_calculations": "male",
                            "primary_goals": ["general_fitness"],
                            "training_frequency": "2",
                        },
                    )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["preferred_units"], "metric")
        self.assertEqual(body["training_environment"], "limited_equipment")
        self.assertEqual(body["typical_session_minutes"], 40)
        self.assertEqual(body["sex_for_physiological_calculations"], "male")
        self.assertEqual(body["primary_goals"], ["general_fitness"])
        self.assertEqual(body["training_frequency"], "2")
        mocked.assert_awaited_once()
        patch_arg = mocked.await_args.args[1]
        self.assertIsInstance(patch_arg, ProfilePatch)
        self.assertEqual(patch_arg.preferred_units, "metric")

    def test_patch_rejects_bad_enum_and_bounds(self):
        with _env():
            with patch(
                "routers.profile.patch_profile",
                new_callable=AsyncMock,
            ) as mocked:
                with TestClient(app) as client:
                    bad_units = client.patch(
                        "/profile",
                        headers={"Authorization": "Bearer test"},
                        json={"preferred_units": "stone"},
                    )
                    bad_minutes = client.patch(
                        "/profile",
                        headers={"Authorization": "Bearer test"},
                        json={"typical_session_minutes": 9},
                    )
                    bad_goals = client.patch(
                        "/profile",
                        headers={"Authorization": "Bearer test"},
                        json={"primary_goals": ["build_muscle"] * 4},
                    )
        self.assertEqual(bad_units.status_code, 422)
        self.assertEqual(bad_minutes.status_code, 422)
        self.assertEqual(bad_goals.status_code, 422)
        mocked.assert_not_awaited()


class ProfileColumnsContractTests(unittest.TestCase):
    def test_db_profile_columns_include_onboarding_fields(self):
        from shared.db import _PROFILE_COLUMNS

        for col in (
            "preferred_units",
            "training_environment",
            "typical_session_minutes",
            "sex_for_physiological_calculations",
        ):
            self.assertIn(col, _PROFILE_COLUMNS)


if __name__ == "__main__":
    unittest.main()
