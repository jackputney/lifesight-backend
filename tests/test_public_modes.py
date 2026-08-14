"""Focused contract test: GET /modes ordered public catalog.

Run:  python -m unittest tests.test_public_modes -v
"""

from __future__ import annotations

import unittest

from main import MODE_REGISTRY, MODE_TOOLS, PUBLIC_MODE_IDS, modes


EXPECTED_PUBLIC_MODES = [
    "fitness",
    "diet",
    "author",
    "brainstorm",
    "mail_calendar",
]


class PublicModesTests(unittest.TestCase):
    def test_public_modes_exact_ordered_response(self) -> None:
        self.assertEqual(list(PUBLIC_MODE_IDS), EXPECTED_PUBLIC_MODES)
        self.assertEqual(modes(), {"modes": EXPECTED_PUBLIC_MODES})
        # Guard against accidental sorted() regressions.
        self.assertNotEqual(modes()["modes"], sorted(EXPECTED_PUBLIC_MODES))

    def test_new_modes_registered_with_personal_context_tool(self) -> None:
        self.assertIn("brainstorm", MODE_REGISTRY)
        self.assertIn("mail_calendar", MODE_REGISTRY)
        self.assertEqual(
            [t["name"] for t in MODE_TOOLS.get("brainstorm") or []],
            ["update_personal_context"],
        )
        self.assertEqual(
            [t["name"] for t in MODE_TOOLS.get("mail_calendar") or []],
            [
                "list_calendar_events",
                "create_pending_action",
                "update_personal_context",
            ],
        )

    def test_jarvis_remains_registered_but_not_public(self) -> None:
        self.assertIn("jarvis", MODE_REGISTRY)
        self.assertNotIn("jarvis", PUBLIC_MODE_IDS)
        self.assertNotIn("jarvis", modes()["modes"])

    def test_checkin_registered_but_not_public(self) -> None:
        self.assertIn("checkin", MODE_REGISTRY)
        self.assertNotIn("checkin", PUBLIC_MODE_IDS)
        self.assertNotIn("checkin", modes()["modes"])
        self.assertEqual(
            [t["name"] for t in MODE_TOOLS["checkin"]],
            ["update_daily_checkin"],
        )

    def test_health_is_retired_from_registry_and_public_list(self) -> None:
        self.assertNotIn("health", MODE_REGISTRY)
        self.assertNotIn("health", PUBLIC_MODE_IDS)
        self.assertNotIn("health", modes()["modes"])


if __name__ == "__main__":
    unittest.main()
