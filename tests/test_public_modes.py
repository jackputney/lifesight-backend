"""Focused contract test: GET /modes advertises only the active v2 modes.

Run:  python -m unittest tests.test_public_modes -v
"""

from __future__ import annotations

import unittest

from main import MODE_REGISTRY, PUBLIC_MODE_IDS, modes


class PublicModesTests(unittest.TestCase):
    def test_public_modes_exactly_fitness_diet_author(self) -> None:
        self.assertEqual(PUBLIC_MODE_IDS, ("author", "diet", "fitness"))
        self.assertEqual(modes(), {"modes": ["author", "diet", "fitness"]})

    def test_jarvis_remains_registered_but_not_public(self) -> None:
        self.assertIn("jarvis", MODE_REGISTRY)
        self.assertNotIn("jarvis", PUBLIC_MODE_IDS)
        self.assertNotIn("jarvis", modes()["modes"])

    def test_health_is_retired_from_registry_and_public_list(self) -> None:
        self.assertNotIn("health", MODE_REGISTRY)
        self.assertNotIn("health", PUBLIC_MODE_IDS)
        self.assertNotIn("health", modes()["modes"])


if __name__ == "__main__":
    unittest.main()
