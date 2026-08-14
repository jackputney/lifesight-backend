"""User prompt overrides + Oliver admin DB contract (014).

Run:  python -m unittest tests.test_user_prompt_overrides_admin_v1 -v
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from main import MODE_REGISTRY, _build_system_prompt, app
from shared.epistemic import (
    EPISTEMIC_GROUNDING,
    FEASIBILITY_AND_NON_SYCOPHANCY,
)
from shared.identity import IDENTITY
from shared.prompt_overrides import (
    USER_CUSTOMIZATION_PREAMBLE,
    format_user_customization_block,
    load_active_customization_block,
)

REPO = Path(__file__).resolve().parents[1]
MIGRATION_014 = REPO / "migrations" / "014_user_prompt_overrides_admin_contract.sql"
DEV_USER = "00000000-0000-4000-8000-000000000001"


class Migration014ContractTests(unittest.TestCase):
    def test_migration_exists(self):
        self.assertTrue(MIGRATION_014.is_file())
        sql = MIGRATION_014.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS user_prompt_overrides", sql)
        self.assertIn("user_prompt_overrides_one_active_per_user_mode", sql)
        self.assertIn("CREATE OR REPLACE VIEW admin_audit_events", sql)
        self.assertIn("status_reason", sql)
        self.assertIn("'checkin'", sql)
        self.assertIn("No /admin HTTP surface", sql)

    def test_no_admin_http_routes(self):
        paths = {getattr(r, "path", "") for r in app.routes}
        adminish = [p for p in paths if "/admin" in p]
        self.assertEqual(adminish, [])


class FormatCustomizationTests(unittest.TestCase):
    def test_empty_when_no_overrides(self):
        self.assertEqual(
            format_user_customization_block(
                global_instructions=None, mode_instructions=None
            ),
            "",
        )

    def test_preamble_marks_subordinate(self):
        block = format_user_customization_block(
            global_instructions="Prefer short replies.",
            mode_instructions="Call out rest times.",
        )
        self.assertTrue(block.startswith(USER_CUSTOMIZATION_PREAMBLE))
        self.assertIn("must not weaken", block.lower())
        self.assertIn("Global (all modes):", block)
        self.assertIn("Mode-specific:", block)


class LoadCustomizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_global_and_mode_specific(self):
        rows = [
            {
                "id": "1",
                "user_id": DEV_USER,
                "mode": None,
                "instructions": "Keep answers brief.",
                "version": 2,
                "is_active": True,
            },
            {
                "id": "2",
                "user_id": DEV_USER,
                "mode": "fitness",
                "instructions": "Announce PRs clearly.",
                "version": 1,
                "is_active": True,
            },
        ]
        with patch(
            "shared.prompt_overrides.db.get_active_prompt_overrides",
            new_callable=AsyncMock,
            return_value=rows,
        ):
            block = await load_active_customization_block(DEV_USER, "fitness")
        self.assertIn("Keep answers brief.", block)
        self.assertIn("Announce PRs clearly.", block)

    async def test_ignores_blank_instructions(self):
        with patch(
            "shared.prompt_overrides.db.get_active_prompt_overrides",
            new_callable=AsyncMock,
            return_value=[{"mode": None, "instructions": "   "}],
        ):
            block = await load_active_customization_block(DEV_USER, "diet")
        self.assertEqual(block, "")


class PromptOrderTests(unittest.TestCase):
    def test_runtime_order_user_customization_after_mode_before_context(self):
        mode_prompt = MODE_REGISTRY["fitness"]
        user_block = format_user_customization_block(
            global_instructions="User note.",
            mode_instructions=None,
        )
        built = _build_system_prompt(
            "fitness",
            profile_block="User profile:\n- occupation: Engineer",
            checkin_block="Today's daily check-in:\n- energy: 2",
            user_customization_block=user_block,
        )
        self.assertTrue(built.startswith(mode_prompt))
        # Shared layers are embedded inside MODE_REGISTRY composition.
        self.assertIn(IDENTITY, mode_prompt)
        self.assertIn(EPISTEMIC_GROUNDING, mode_prompt)
        self.assertIn(FEASIBILITY_AND_NON_SYCOPHANCY, mode_prompt)
        # Subordinate user block then date/profile/check-in.
        self.assertLess(
            built.index("User-specific customization"),
            built.index("Today's date is"),
        )
        self.assertLess(
            built.index("Today's date is"),
            built.index("occupation: Engineer"),
        )
        self.assertLess(
            built.index("occupation: Engineer"),
            built.index("energy: 2"),
        )

    def test_every_registry_mode_still_embeds_shared_layers(self):
        for mode, prompt in MODE_REGISTRY.items():
            with self.subTest(mode=mode):
                self.assertIn(IDENTITY, prompt)
                self.assertIn(EPISTEMIC_GROUNDING, prompt)
                self.assertIn(FEASIBILITY_AND_NON_SYCOPHANCY, prompt)


class AccountStatusEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_assert_session_active_rejects_disabled_user(self):
        from shared.local_auth.service import AuthError, AuthService

        service = AuthService(store=AsyncMock())
        service.store.get_session = AsyncMock(
            return_value={
                "id": "sess",
                "user_id": DEV_USER,
                "revoked_at": None,
                "expires_at": __import__("datetime").datetime(
                    2099, 1, 1, tzinfo=__import__("datetime").timezone.utc
                ),
            }
        )
        service.store.get_user_by_id = AsyncMock(
            return_value={"id": DEV_USER, "is_active": False}
        )
        service.store.touch_session = AsyncMock()
        with self.assertRaises(AuthError):
            await service.assert_session_active("sess", DEV_USER)
        service.store.touch_session.assert_not_awaited()

    async def test_assert_session_active_allows_active_user(self):
        from shared.local_auth.service import AuthService

        service = AuthService(store=AsyncMock())
        service.store.get_session = AsyncMock(
            return_value={
                "id": "sess",
                "user_id": DEV_USER,
                "revoked_at": None,
                "expires_at": __import__("datetime").datetime(
                    2099, 1, 1, tzinfo=__import__("datetime").timezone.utc
                ),
            }
        )
        service.store.get_user_by_id = AsyncMock(
            return_value={"id": DEV_USER, "is_active": True}
        )
        service.store.touch_session = AsyncMock()
        await service.assert_session_active("sess", DEV_USER)
        service.store.touch_session.assert_awaited_once()


class OliverDocTests(unittest.TestCase):
    def test_handoff_doc_exists(self):
        path = REPO / "docs" / "OLIVER_ADMIN_DATABASE_CONTRACT.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("user_prompt_overrides", text)
        self.assertIn("admin_audit_log", text)
        self.assertIn("is_active", text)
        self.assertIn("Do not invent a parallel", text)


if __name__ == "__main__":
    unittest.main()
