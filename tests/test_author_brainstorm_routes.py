"""Slice 1A: Author brainstorm-session path + compatibility alias.

Run:  python -m unittest tests.test_author_brainstorm_routes -v

Caller search before rename (both repos):
- Backend: routers/v2.py (implementation), modes/author/prompt.py, docs
- iOS: plan doc only (IOS_PLAN_BRAINSTORM_MAILCALENDAR.md) — no Swift caller
"""

from __future__ import annotations

import unittest

from fastapi.routing import APIRoute

from main import app
from routers.v2 import (
    _run_author_brainstorm_session,
    author_brainstorm_compat,
    author_brainstorm_session,
)


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
        self.assertIs(
            author_brainstorm_session.__wrapped__
            if hasattr(author_brainstorm_session, "__wrapped__")
            else author_brainstorm_session,
            author_brainstorm_session,
        )
        # Endpoint callables are thin wrappers; both must call the shared core.
        session_src = author_brainstorm_session.__code__.co_names
        compat_src = author_brainstorm_compat.__code__.co_names
        self.assertIn("_run_author_brainstorm_session", session_src)
        self.assertIn("_run_author_brainstorm_session", compat_src)
        self.assertTrue(callable(_run_author_brainstorm_session))

    def test_compat_alias_hidden_from_openapi_schema(self) -> None:
        routes = _post_routes_by_path()
        self.assertFalse(routes["/author/brainstorm"].include_in_schema)
        self.assertTrue(routes["/author/brainstorm-session"].include_in_schema)


if __name__ == "__main__":
    unittest.main()
