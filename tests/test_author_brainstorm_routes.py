"""Slice 1A: Author brainstorm-session path + compatibility alias.

Run:  python -m unittest tests.test_author_brainstorm_routes -v

Caller search before rename (both repos):
- Backend: routers/v2.py (implementation), modes/author/prompt.py, docs
- iOS: plan doc only (IOS_PLAN_BRAINSTORM_MAILCALENDAR.md) — no Swift caller
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from main import app
from routers.v2 import (
    _run_author_brainstorm_session,
    author_brainstorm_compat,
    author_brainstorm_session,
)

FIXED_RESPONSE = {
    "reply": "A plot fork: the mentor is lying.",
    "brainstorm_session_id": "00000000-0000-4000-8000-0000000000aa",
    "pending_action": None,
    "visual_panel": None,
    "research": None,
}

REQUEST_BODY = {
    "manuscript_id": "00000000-0000-4000-8000-0000000000bb",
    "transcript": "What if the mentor is the villain?",
}


def _post_routes_by_path() -> dict[str, APIRoute]:
    found: dict[str, APIRoute] = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and "POST" in route.methods:
            found[route.path] = route
    return found


class AuthorBrainstormRouteTests(unittest.TestCase):
    def test_canonical_and_compat_paths_are_registered(self) -> None:
        routes = _post_routes_by_path()
        self.assertIn("/author/brainstorm-session", routes)
        self.assertIn("/author/brainstorm", routes)

    def test_both_paths_share_the_same_implementation(self) -> None:
        """Alias must not diverge into a second copy of the logic."""
        session_src = author_brainstorm_session.__code__.co_names
        compat_src = author_brainstorm_compat.__code__.co_names
        self.assertIn("_run_author_brainstorm_session", session_src)
        self.assertIn("_run_author_brainstorm_session", compat_src)
        self.assertTrue(callable(_run_author_brainstorm_session))

    def test_compat_alias_hidden_from_openapi_schema(self) -> None:
        routes = _post_routes_by_path()
        self.assertFalse(routes["/author/brainstorm"].include_in_schema)
        self.assertTrue(routes["/author/brainstorm-session"].include_in_schema)

    @patch("shared.db.init_pool", new_callable=AsyncMock)
    @patch("shared.db.close_pool", new_callable=AsyncMock)
    @patch(
        "routers.v2._run_author_brainstorm_session",
        new_callable=AsyncMock,
        return_value=FIXED_RESPONSE,
    )
    def test_both_paths_return_equivalent_behavior(
        self,
        mock_run: AsyncMock,
        _close: AsyncMock,
        _init: AsyncMock,
    ) -> None:
        headers = {"Authorization": "Bearer test"}
        with patch.dict(os.environ, {"AUTH_MODE": "dev"}, clear=False):
            with TestClient(app) as client:
                canonical = client.post(
                    "/author/brainstorm-session",
                    json=REQUEST_BODY,
                    headers=headers,
                )
                compat = client.post(
                    "/author/brainstorm",
                    json=REQUEST_BODY,
                    headers=headers,
                )

        self.assertEqual(canonical.status_code, 200, canonical.text)
        self.assertEqual(compat.status_code, 200, compat.text)
        self.assertEqual(canonical.json(), FIXED_RESPONSE)
        self.assertEqual(compat.json(), FIXED_RESPONSE)
        self.assertEqual(canonical.json(), compat.json())
        self.assertEqual(mock_run.await_count, 2)


if __name__ == "__main__":
    unittest.main()
