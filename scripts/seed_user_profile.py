#!/usr/bin/env python3
"""Seed/update user_profiles for local/staging tester accounts.

No public HTTP admin surface. Refuses production by default.

Usage examples:
  ADMIN_SEED_TOKEN=... python scripts/seed_user_profile.py \\
    --username smoke_tester --timezone America/Los_Angeles \\
    --experience-level intermediate --primary-goals build_muscle,get_stronger \\
    --preferred-units imperial --training-environment commercial_gym \\
    --typical-session-minutes 60 --training-frequency 3

Requires DATABASE_URL and matching ADMIN_SEED_TOKEN in the environment.
Never prints passwords or secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Repo root on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from shared import db  # noqa: E402
from shared.profile_schema import ProfilePatch  # noqa: E402
from shared.profile_service import get_profile, patch_profile  # noqa: E402


def _deploy_env() -> str:
    return (
        os.environ.get("APP_ENV")
        or os.environ.get("ENVIRONMENT")
        or "development"
    ).strip().lower()


def _refuse_production() -> None:
    env = _deploy_env()
    if env in {"production", "prod"}:
        # Explicit future escape hatch — off by default.
        if (os.environ.get("ALLOW_PROFILE_SEED_IN_PRODUCTION") or "").strip() != "1":
            raise SystemExit(
                "seed_user_profile refuses production "
                "(set ALLOW_PROFILE_SEED_IN_PRODUCTION=1 only if intentionally enabled)."
            )


def _require_seed_token(cli_token: str | None) -> None:
    expected = (os.environ.get("ADMIN_SEED_TOKEN") or "").strip()
    if not expected:
        raise SystemExit("ADMIN_SEED_TOKEN must be set in the environment.")
    provided = (cli_token or os.environ.get("ADMIN_SEED_TOKEN_CLI") or "").strip()
    # Allow invoking with env alone when CLI flag omitted (same process env).
    if not provided:
        provided = expected
    if provided != expected:
        raise SystemExit("ADMIN_SEED_TOKEN mismatch — refusing to run.")


def _parse_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    items = [part.strip() for part in raw.split(",") if part.strip()]
    return items


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Seed/update a LifeSight user_profiles row.")
    id_group = p.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--user-id")
    id_group.add_argument("--username")
    id_group.add_argument("--email")
    p.add_argument("--seed-token", help="Must match ADMIN_SEED_TOKEN")
    p.add_argument("--timezone")
    p.add_argument("--date-of-birth", help="YYYY-MM-DD")
    p.add_argument("--height-cm", type=float)
    p.add_argument("--weight-kg", type=float)
    p.add_argument(
        "--interaction-style",
        choices=["standard", "voice_first", "high_accessibility"],
    )
    p.add_argument("--vision-preference")
    p.add_argument("--spoken-response-preference")
    p.add_argument("--experience-level")
    p.add_argument(
        "--primary-goals",
        help=(
            "Comma-separated ordered goals (max 3): index 0 = primary. "
            "Values: build_muscle,get_stronger,lose_body_fat,improve_endurance,"
            "general_fitness,longevity_health,track_nutrition,return_to_training,"
            "better_habits"
        ),
    )
    p.add_argument(
        "--training-frequency",
        choices=["0_1", "2", "3", "4", "5", "6_plus"],
        help="Canonical weekly frequency wire value",
    )
    p.add_argument("--available-equipment", help="Comma-separated")
    p.add_argument("--injuries-limitations")
    p.add_argument("--nutrition-goal")
    p.add_argument("--dietary-preferences", help="Comma-separated")
    p.add_argument("--allergies-restrictions", help="Comma-separated")
    p.add_argument("--preferred-units", choices=["imperial", "metric"])
    p.add_argument(
        "--training-environment",
        choices=[
            "commercial_gym",
            "home_gym",
            "limited_equipment",
            "bodyweight_outdoors",
            "mixed",
        ],
    )
    p.add_argument(
        "--typical-session-minutes",
        type=int,
        help="Typical session length in minutes (10–300)",
    )
    p.add_argument(
        "--sex-for-physiological-calculations",
        choices=["male", "female", "unspecified"],
        help="Formula/reference use only — not gender identity",
    )
    p.add_argument("--occupation", help="Work/role (<=120 chars)")
    p.add_argument("--industry", help="Industry/field (<=120 chars)")
    p.add_argument("--education-context", help="School/education (<=240 chars)")
    p.add_argument("--interests", help="Comma-separated interests (max 20)")
    p.add_argument("--typical-schedule", help="Typical schedule free text (<=500)")
    return p


async def _run(args: argparse.Namespace) -> int:
    _refuse_production()
    _require_seed_token(args.seed_token)

    await db.init_pool()
    try:
        user = await db.find_user_for_seed(
            user_id=args.user_id,
            username=args.username,
            email=args.email,
        )
        if user is None:
            print("error: user not found", file=sys.stderr)
            return 1

        patch_data = {
            "timezone": args.timezone,
            "date_of_birth": args.date_of_birth,
            "height_cm": args.height_cm,
            "weight_kg": args.weight_kg,
            "interaction_style": args.interaction_style,
            "vision_preference": args.vision_preference,
            "spoken_response_preference": args.spoken_response_preference,
            "experience_level": args.experience_level,
            "primary_goals": _parse_list(args.primary_goals),
            "training_frequency": args.training_frequency,
            "available_equipment": _parse_list(args.available_equipment),
            "injuries_limitations": args.injuries_limitations,
            "nutrition_goal": args.nutrition_goal,
            "dietary_preferences": _parse_list(args.dietary_preferences),
            "allergies_restrictions": _parse_list(args.allergies_restrictions),
            "preferred_units": args.preferred_units,
            "training_environment": args.training_environment,
            "typical_session_minutes": args.typical_session_minutes,
            "sex_for_physiological_calculations": (
                args.sex_for_physiological_calculations
            ),
            "occupation": args.occupation,
            "industry": args.industry,
            "education_context": args.education_context,
            "interests": _parse_list(args.interests),
            "typical_schedule": args.typical_schedule,
        }
        # Drop Nones so ProfilePatch exclude_unset works via model_validate partial
        compact = {k: v for k, v in patch_data.items() if v is not None}
        patch = ProfilePatch.model_validate(compact)
        before = await get_profile(str(user["id"]))
        after = await patch_profile(str(user["id"]), patch)
        await db.insert_admin_audit(
            actor="seed_user_profile.py",
            action="upsert_user_profile",
            target_user_id=str(user["id"]),
            detail={
                "username": user.get("username"),
                "fields": sorted(compact.keys()),
                "app_env": _deploy_env(),
            },
        )
        print(
            f"ok user_id={after.user_id} username={user.get('username')} "
            f"fields={sorted(compact.keys()) or ['(ensure row)']}"
        )
        # Avoid dumping full PII; only confirm presence of key scalars.
        print(
            f"profile timezone={after.timezone!r} "
            f"experience_level={after.experience_level!r} "
            f"had_row_before={before.updated_at is not None}"
        )
        return 0
    finally:
        await db.close_pool()


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
