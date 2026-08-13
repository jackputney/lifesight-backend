"""User context / history / exercise panel / profile V1.

Run:  python -m unittest tests.test_user_context_history_v1 -v
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from shared.client_actions import NavigateAction, open_conversation_action, parse_navigate_command
from shared.context_budget import build_model_messages, estimate_tokens
from shared.context_config import context_summary_token_budget
from shared.conversation_summary import (
    build_extractive_summary,
    clamp_summary_text,
    needs_summarization,
)
from shared.conversation_titles import fallback_title, title_from_user_text
from shared.open_conversation import (
    parse_open_conversation_command,
    resolve_open_conversation,
)
from shared.profile_schema import ProfilePatch, empty_profile
from shared.visual_panels import (
    ExercisePanelData,
    exercise_visual_panel,
    parse_exercise_panel_tool_input,
)


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


class ExercisePanelSchemaTests(unittest.TestCase):
    def test_valid_exercise_panel(self):
        data = ExercisePanelData(
            exercise_id=None,
            exercise_name="Bench Press",
            sets=4,
            reps=8,
            rest_seconds=120,
            current_set=1,
            notes=None,
        )
        panel = exercise_visual_panel(data)
        self.assertEqual(panel.type, "exercise")
        self.assertEqual(panel.data["exercise_name"], "Bench Press")
        self.assertIsNone(panel.data["exercise_id"])

    def test_invalid_exercise_id_rejected(self):
        with self.assertRaises(ValidationError):
            ExercisePanelData(
                exercise_id="not-a-uuid",
                exercise_name="Squat",
                sets=3,
                reps=5,
                rest_seconds=90,
            )

    def test_tool_input_strips_unknown_id_tokens(self):
        data = parse_exercise_panel_tool_input(
            {
                "exercise_id": "unknown",
                "exercise_name": "Row",
                "sets": 3,
                "reps": 10,
                "rest_seconds": 60,
            }
        )
        self.assertIsNone(data.exercise_id)


class ProfileSchemaTests(unittest.TestCase):
    def test_empty_profile_defaults(self):
        p = empty_profile("u1")
        self.assertEqual(p.primary_goals, [])
        self.assertIsNone(p.timezone)

    def test_patch_bounds_arrays(self):
        with self.assertRaises(ValidationError):
            ProfilePatch(primary_goals=["x"] * 21)


class TitleTests(unittest.TestCase):
    def test_truncates(self):
        long = "a" * 80
        t = title_from_user_text(long, mode="fitness")
        self.assertLessEqual(len(t), 60)
        self.assertTrue(t.endswith("…"))

    def test_fallback(self):
        self.assertEqual(fallback_title("author"), "Author chat")


class OpenConversationParseTests(unittest.TestCase):
    def test_last_fitness(self):
        intent = parse_open_conversation_command("Open my last fitness chat.")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.mode, "fitness")
        self.assertTrue(intent.most_recent)

    def test_author_yesterday(self):
        intent = parse_open_conversation_command(
            "Go back to my Author chat from yesterday."
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.mode, "author")
        self.assertEqual(intent.day, "yesterday")

    def test_most_recent(self):
        intent = parse_open_conversation_command("Open my most recent chat")
        self.assertIsNotNone(intent)
        self.assertIsNone(intent.mode)

    def test_navigate_not_stolen_for_plain_open_fitness(self):
        self.assertIsNotNone(parse_navigate_command("Open fitness."))
        self.assertIsNone(parse_open_conversation_command("Open fitness."))

    def test_ambiguous_no_action(self):
        intent = parse_open_conversation_command("Open my last fitness chat.")
        res = resolve_open_conversation(
            [
                {"id": "00000000-0000-4000-8000-000000000011", "mode": "fitness"},
                {"id": "00000000-0000-4000-8000-000000000012", "mode": "fitness"},
            ],
            intent=intent,
        )
        self.assertTrue(res.ambiguous)
        self.assertIsNone(res.conversation_id)

    def test_unique_resolves(self):
        intent = parse_open_conversation_command("Open my last fitness chat.")
        cid = "00000000-0000-4000-8000-000000000013"
        res = resolve_open_conversation(
            [{"id": cid, "mode": "fitness"}], intent=intent
        )
        self.assertEqual(res.conversation_id, cid)
        action = open_conversation_action(cid)
        self.assertEqual(action.type, "open_conversation")
        self.assertEqual(action.conversation_id, cid)


class ContextBudgetTests(unittest.TestCase):
    def test_large_messages_reduce_window(self):
        huge = "x" * 50_000
        recent = [{"role": "user", "content": huge} for _ in range(20)]
        built = build_model_messages(
            system_prompt="sys",
            profile_block="profile",
            summary_text=None,
            summary_through_seq=None,
            recent_messages=recent,
            current_user_message={"role": "user", "content": "hi"},
        )
        # Must stay under budget (estimate).
        total = built.estimated_system_tokens + built.estimated_message_tokens
        self.assertLessEqual(total, 24_000 + 500)  # tiny slack for rounding
        self.assertLess(built.raw_messages_included, 21)

    def test_summary_used_flag(self):
        built = build_model_messages(
            system_prompt="sys",
            profile_block="",
            summary_text="Earlier we chose upper body.",
            summary_through_seq=10,
            recent_messages=[{"role": "user", "content": "ok"}],
            current_user_message={"role": "user", "content": "next"},
        )
        self.assertTrue(built.summary_used)
        self.assertEqual(built.summary_through_seq, 10)


class SummaryTests(unittest.TestCase):
    def test_threshold(self):
        msgs = [{"seq": i, "role": "user", "content": f"m{i}"} for i in range(30)]
        self.assertTrue(needs_summarization(msgs, summary_through_seq=None))
        self.assertFalse(
            needs_summarization(msgs[:10], summary_through_seq=None)
        )

    def test_extractive_keeps_recent_out_of_through_seq(self):
        msgs = [
            {"seq": i, "role": "user", "content": f"message {i}"} for i in range(40)
        ]
        text, through = build_extractive_summary(
            msgs, previous_summary=None, keep_recent=20
        )
        self.assertTrue(text)
        self.assertEqual(through, 19)  # last of older span
        self.assertLessEqual(estimate_tokens(text), context_summary_token_budget())

    def test_clamp_summary_hard_bound(self):
        huge = "fact " * 5000
        clamped = clamp_summary_text(huge)
        self.assertLessEqual(
            estimate_tokens(clamped), context_summary_token_budget()
        )

    def test_repeated_compaction_stays_bounded(self):
        """Simulate 100+ turns with multiple summary cycles.

        Invariants:
        - stored summary_text stays under CONTEXT_SUMMARY_TOKEN_BUDGET
        - assembled model context stays under CONTEXT_INPUT_TOKEN_BUDGET
        - recent messages remain verbatim in the builder window
        - raw message list length is unchanged (DB rows would remain intact)
        """
        with patch.dict(
            os.environ,
            {
                "CONTEXT_SUMMARY_THRESHOLD": "30",
                "CONTEXT_RECENT_MESSAGE_CAP": "20",
                "CONTEXT_SUMMARY_TOKEN_BUDGET": "800",
                "CONTEXT_INPUT_TOKEN_BUDGET": "24000",
            },
            clear=False,
        ):
            raw: list[dict] = []
            summary_text: str | None = None
            summary_through: int | None = None
            keep = 20
            threshold = 30

            for i in range(120):
                raw.append(
                    {
                        "seq": i,
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": (
                            f"Turn {i}: concrete workout detail about set {i} "
                            f"and a somewhat long coaching note {'x' * 80}."
                        ),
                    }
                )
                floor = -1 if summary_through is None else int(summary_through)
                unsummarized = [m for m in raw if int(m["seq"]) > floor]
                if len(unsummarized) >= threshold:
                    summary_text, summary_through = build_extractive_summary(
                        raw,
                        previous_summary=summary_text,
                        keep_recent=keep,
                    )
                    self.assertLessEqual(
                        estimate_tokens(summary_text),
                        context_summary_token_budget(),
                    )

            self.assertIsNotNone(summary_text)
            self.assertGreaterEqual(summary_through or -1, 0)
            # Raw history intact (would be unchanged Postgres rows).
            self.assertEqual(len(raw), 120)
            self.assertEqual(raw[0]["content"].startswith("Turn 0:"), True)
            self.assertIn("Turn 119:", raw[-1]["content"])

            floor = int(summary_through)
            prior = [
                {"role": m["role"], "content": m["content"]}
                for m in raw
                if int(m["seq"]) > floor
            ]
            # Recent window must still contain the latest verbatim turns.
            self.assertTrue(any("Turn 119:" in m["content"] for m in prior[-keep:]))

            built = build_model_messages(
                system_prompt="fitness system prompt " + ("y" * 200),
                profile_block="profile block",
                summary_text=summary_text,
                summary_through_seq=summary_through,
                recent_messages=prior,
                current_user_message={"role": "user", "content": "What's next?"},
            )
            total = built.estimated_system_tokens + built.estimated_message_tokens
            self.assertLessEqual(total, 24_000 + 500)
            self.assertTrue(built.summary_used)
            self.assertLessEqual(
                estimate_tokens(summary_text or ""),
                context_summary_token_budget(),
            )


class ChatRouteContextHistoryTests(unittest.TestCase):
    def setUp(self):
        from shared.profile_schema import empty_profile

        self.patches = [
            patch("shared.db.init_pool", new_callable=AsyncMock),
            patch("shared.db.close_pool", new_callable=AsyncMock),
            patch("shared.db.create_conversation", new_callable=AsyncMock),
            patch("shared.db.get_conversation", new_callable=AsyncMock, return_value=None),
            patch("shared.db.load_messages", new_callable=AsyncMock, return_value=[]),
            patch(
                "shared.db.load_messages_with_seq",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("shared.db.append_message", new_callable=AsyncMock),
            patch(
                "shared.db.set_conversation_title_if_empty", new_callable=AsyncMock
            ),
            patch(
                "shared.db.find_conversations_for_open",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "main.get_profile",
                new_callable=AsyncMock,
                return_value=empty_profile(
                    "00000000-0000-4000-8000-000000000001"
                ),
            ),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_ordinary_chat_null_visual_panel(self):
        with _env():
            with patch(
                "main._run_model_turn",
                new_callable=AsyncMock,
                return_value=("Hello.", None, None),
            ):
                with TestClient(app) as client:
                    resp = client.post(
                        "/chat",
                        json={
                            "transcript": "What is progressive overload?",
                            "mode": "fitness",
                            "conversation_id": None,
                        },
                        headers={"Authorization": "Bearer test"},
                    )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["visual_panel"])
        self.assertEqual(resp.json()["client_actions"], [])

    def test_open_conversation_unique(self):
        cid = "00000000-0000-4000-8000-0000000000aa"
        with _env():
            with patch(
                "shared.db.find_conversations_for_open",
                new_callable=AsyncMock,
                return_value=[{"id": cid, "mode": "fitness"}],
            ):
                with TestClient(app) as client:
                    resp = client.post(
                        "/chat",
                        json={
                            "transcript": "Open my last fitness chat.",
                            "mode": "diet",
                            "conversation_id": None,
                        },
                        headers={"Authorization": "Bearer test"},
                    )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(
            body["client_actions"],
            [{"type": "open_conversation", "conversation_id": cid}],
        )
        self.assertNotEqual(body["client_actions"][0].get("type"), "navigate")

    def test_open_conversation_ambiguous(self):
        with _env():
            with patch(
                "shared.db.find_conversations_for_open",
                new_callable=AsyncMock,
                return_value=[
                    {"id": "00000000-0000-4000-8000-0000000000a1", "mode": "fitness"},
                    {"id": "00000000-0000-4000-8000-0000000000a2", "mode": "fitness"},
                ],
            ):
                with TestClient(app) as client:
                    resp = client.post(
                        "/chat",
                        json={
                            "transcript": "Open my last fitness chat.",
                            "mode": "fitness",
                            "conversation_id": None,
                        },
                        headers={"Authorization": "Bearer test"},
                    )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["client_actions"], [])
        self.assertIn("more than one", body["reply"].lower())

    def test_navigate_unchanged(self):
        with _env():
            with TestClient(app) as client:
                resp = client.post(
                    "/chat",
                    json={
                        "transcript": "Open fitness.",
                        "mode": "diet",
                        "conversation_id": None,
                    },
                    headers={"Authorization": "Bearer test"},
                )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json()["client_actions"],
            [{"type": "navigate", "target": "fitness"}],
        )

    def test_resume_uses_stored_mode(self):
        cid = "00000000-0000-4000-8000-0000000000bb"
        with _env():
            with patch(
                "shared.db.get_conversation",
                new_callable=AsyncMock,
                return_value={
                    "id": cid,
                    "user_id": "00000000-0000-4000-8000-000000000001",
                    "mode": "author",
                    "summary_text": None,
                    "summary_through_seq": None,
                },
            ), patch(
                "main._run_model_turn",
                new_callable=AsyncMock,
                return_value=("Continuing.", None, None),
            ) as turn:
                with TestClient(app) as client:
                    resp = client.post(
                        "/chat",
                        json={
                            "transcript": "Keep going.",
                            "mode": "fitness",  # mismatched screen mode
                            "conversation_id": cid,
                        },
                        headers={"Authorization": "Bearer test"},
                    )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["mode"], "author")
        self.assertEqual(turn.await_args.kwargs["mode"], "author")


class SeedScriptGuardTests(unittest.TestCase):
    def test_refuses_production(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "scripts" / "seed_user_profile.py"
        spec = importlib.util.spec_from_file_location("seed_user_profile", path)
        seed = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(seed)

        with patch.dict(
            os.environ,
            {"APP_ENV": "production", "ALLOW_PROFILE_SEED_IN_PRODUCTION": ""},
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                seed._refuse_production()


class ProfileRouteTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch("shared.db.init_pool", new_callable=AsyncMock),
            patch("shared.db.close_pool", new_callable=AsyncMock),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_get_profile_empty_defaults(self):
        with _env():
            with patch(
                "routers.profile.get_profile",
                new_callable=AsyncMock,
                return_value=empty_profile(
                    "00000000-0000-4000-8000-000000000001"
                ),
            ):
                with TestClient(app) as client:
                    resp = client.get(
                        "/profile", headers={"Authorization": "Bearer test"}
                    )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["primary_goals"], [])
        self.assertIsNone(body["timezone"])


if __name__ == "__main__":
    unittest.main()
