"""Personalization foundation (018) — summaries, pending proposals, invariants.

Fully offline: memory store + patched model functions. The load-bearing case is
that a summary is evidence only and that nothing here can write an active
prompt override.

Run:  python -m unittest tests.test_personalization_v1 -v
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from main import MODE_REGISTRY, _build_system_prompt
from shared.epistemic import (
    EPISTEMIC_GROUNDING,
    FEASIBILITY_AND_NON_SYCOPHANCY,
    compose_system_prompt,
)
from shared.identity import IDENTITY
from shared.personalization import proposals, store, summarize

REPO = Path(__file__).resolve().parents[1]
MIGRATION_018 = REPO / "migrations" / "018_personalization.sql"
PACKAGE_DIR = REPO / "shared" / "personalization"
RUNNER_PATH = REPO / "scripts" / "run_personalization_rollup.py"

USER_A = "00000000-0000-4000-8000-0000000000a1"
USER_B = "00000000-0000-4000-8000-0000000000b2"

DAY_ONE = date(2026, 8, 10)
DAY_TWO = date(2026, 8, 11)

# Unique markers so "did this text leak into a prompt?" is unambiguous.
DAILY_MARKER = "SUMMARY_MARKER_DAILY_7f3a"
ROLLUP_MARKER = "SUMMARY_MARKER_ROLLUP_9c1d"
PROPOSAL_MARKER = "PROPOSAL_MARKER_4b8e"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_personalization_rollup", RUNNER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Migration018SchemaTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MIGRATION_018.is_file(), "018_personalization.sql missing")
        self.sql = MIGRATION_018.read_text(encoding="utf-8")

    def test_summaries_table_shape(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS personalization_summaries", self.sql)
        self.assertIn("REFERENCES public.users(id) ON DELETE CASCADE", self.sql)
        self.assertIn("personalization_summaries_scope_chk", self.sql)
        self.assertIn("scope IN ('daily', 'multi_day', 'weekly')", self.sql)
        self.assertIn("period_end >= period_start", self.sql)
        self.assertIn("source_conversation_ids UUID[] NOT NULL DEFAULT '{}'", self.sql)
        self.assertIn("source_summary_ids      UUID[] NOT NULL DEFAULT '{}'", self.sql)
        self.assertIn(
            "CONSTRAINT personalization_summaries_period_uidx UNIQUE", self.sql
        )
        self.assertIn(
            "personalization_summaries_user_scope_start_idx", self.sql
        )
        self.assertIn("COMMENT ON TABLE personalization_summaries", self.sql)

    def test_proposal_table_guarantees(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS prompt_change_proposals", self.sql)
        self.assertIn("prompt_change_proposals_mode_chk", self.sql)
        self.assertIn(
            "status IN ('pending', 'approved', 'rejected', 'applied')", self.sql
        )
        self.assertIn("DEFAULT 'pending'", self.sql)
        # Human reviewer required before approved/applied.
        self.assertIn("prompt_change_proposals_reviewer_required_chk", self.sql)
        self.assertIn("reviewed_at IS NOT NULL AND reviewed_by IS NOT NULL", self.sql)
        # At most one pending proposal per (user, mode-or-global).
        self.assertIn(
            "prompt_change_proposals_one_pending_per_user_mode", self.sql
        )
        self.assertIn("WHERE status = 'pending'", self.sql)
        # proposed_instructions immutability trigger.
        self.assertIn("prompt_change_proposals_freeze_proposed", self.sql)
        self.assertIn("BEFORE UPDATE ON prompt_change_proposals", self.sql)
        self.assertIn("RAISE EXCEPTION", self.sql)
        self.assertIn("COMMENT ON COLUMN prompt_change_proposals.final_instructions", self.sql)
        self.assertIn(
            "COMMENT ON COLUMN prompt_change_proposals.proposed_instructions", self.sql
        )

    def test_migration_mode_allowlist_matches_overrides(self):
        overrides_sql = (
            REPO / "migrations" / "014_user_prompt_overrides_admin_contract.sql"
        ).read_text(encoding="utf-8")

        def modes(sql: str, constraint: str) -> set[str]:
            match = re.search(
                rf"CONSTRAINT {constraint} CHECK \(\s*mode IS NULL\s*OR mode IN \((.*?)\)\s*\)",
                sql,
                re.DOTALL,
            )
            assert match, constraint
            return set(re.findall(r"'([^']+)'", match.group(1)))

        self.assertEqual(
            modes(self.sql, "prompt_change_proposals_mode_chk"),
            modes(overrides_sql, "user_prompt_overrides_mode_chk"),
        )


class PersonalizationStoreTestCase(unittest.IsolatedAsyncioTestCase):
    """Memory store + a db.pool guard: any Postgres call here is a bug."""

    def setUp(self):
        store.use_memory_store(True)
        self.addCleanup(lambda: store.use_memory_store(False))
        self.pool = patch("shared.db.pool", MagicMock(side_effect=AssertionError(
            "personalization tests must not touch Postgres"
        )))
        self.pool_mock = self.pool.start()
        self.addCleanup(self.pool.stop)

    def seed_day(self, user_id: str, day: date, *, texts: list[str]) -> str:
        convo_id = store.memory_seed_conversation(user_id, mode="fitness")
        for index, text in enumerate(texts):
            store.memory_seed_message(
                convo_id,
                role="user" if index % 2 == 0 else "assistant",
                text=text,
                created_on=day,
            )
        return convo_id

    async def build_daily(self, user_id: str, day: date, *, marker: str) -> dict:
        with patch.object(summarize, "generate_summary_text", return_value=marker):
            return await summarize.build_summary(
                user_id, scope="daily", period_start=day, period_end=day
            )


class SummaryEvidenceTests(PersonalizationStoreTestCase):
    async def test_daily_summary_records_only_conversations_read(self):
        convo_id = self.seed_day(
            USER_A, DAY_ONE, texts=["Logged a training session.", "Noted."]
        )
        row = await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)

        self.assertIsNotNone(row)
        self.assertEqual(row["scope"], "daily")
        self.assertEqual(row["summary"], DAILY_MARKER)
        self.assertEqual(row["source_conversation_ids"], [convo_id])
        self.assertEqual(row["source_summary_ids"], [])
        self.assertEqual(row["model_identifier"], summarize.summary_model())
        self.pool_mock.assert_not_called()

    async def test_dropped_conversation_is_not_recorded_as_evidence(self):
        small = self.seed_day(USER_A, DAY_ONE, texts=["short note"])
        self.seed_day(USER_A, DAY_ONE, texts=["x" * 5000])

        with patch.dict(os.environ, {"PERSONALIZATION_MAX_CHARS": "200"}):
            inputs = await summarize.collect_inputs(
                USER_A, scope="daily", period_start=DAY_ONE, period_end=DAY_ONE
            )

        self.assertEqual(inputs.source_conversation_ids, [small])
        self.assertTrue(inputs.truncated)

    async def test_no_material_writes_nothing(self):
        row = await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)
        self.assertIsNone(row)
        self.assertEqual(store._memory.summaries, {})

    async def test_rollup_chains_evidence_through_summary_ids(self):
        self.seed_day(USER_A, DAY_ONE, texts=["Day one."])
        self.seed_day(USER_A, DAY_TWO, texts=["Day two."])
        first = await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)
        second = await self.build_daily(USER_A, DAY_TWO, marker=DAILY_MARKER)

        with patch.object(summarize, "generate_summary_text", return_value=ROLLUP_MARKER):
            rollup = await summarize.build_summary(
                USER_A, scope="multi_day", period_start=DAY_ONE, period_end=DAY_TWO
            )

        self.assertEqual(rollup["scope"], "multi_day")
        self.assertEqual(
            sorted(rollup["source_summary_ids"]),
            sorted([str(first["id"]), str(second["id"])]),
        )
        # Rollups did not read raw conversations, so they claim none.
        self.assertEqual(rollup["source_conversation_ids"], [])

    async def test_weekly_falls_back_to_dailies(self):
        self.seed_day(USER_A, DAY_ONE, texts=["Day one."])
        daily = await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)

        with patch.object(summarize, "generate_summary_text", return_value=ROLLUP_MARKER):
            weekly = await summarize.build_summary(
                USER_A, scope="weekly", period_start=DAY_ONE, period_end=DAY_TWO
            )

        self.assertEqual(weekly["scope"], "weekly")
        self.assertEqual(weekly["source_summary_ids"], [str(daily["id"])])

    async def test_rerunning_the_same_period_is_idempotent(self):
        self.seed_day(USER_A, DAY_ONE, texts=["Day one."])
        first = await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)
        second = await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER + " v2")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(store._memory.summaries), 1)
        self.assertEqual(second["summary"], DAILY_MARKER + " v2")

    async def test_cross_user_reads_are_blocked(self):
        self.seed_day(USER_A, DAY_ONE, texts=["Private to A."])
        await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)

        self.assertIsNone(
            await store.get_summary(
                USER_B, scope="daily", period_start=DAY_ONE, period_end=DAY_ONE
            )
        )
        self.assertEqual(
            await store.list_summaries(
                USER_B,
                scopes=("daily",),
                period_start=DAY_ONE,
                period_end=DAY_TWO,
            ),
            [],
        )
        self.assertEqual(
            await store.list_conversations_in_period(USER_B, DAY_ONE, DAY_ONE, limit=10),
            [],
        )
        convo_id = next(iter(store._memory.conversations))
        self.assertEqual(
            await store.list_messages_in_period(convo_id, USER_B, DAY_ONE, DAY_ONE),
            [],
        )


class SummaryIsNotASystemPromptTests(PersonalizationStoreTestCase):
    async def test_summary_text_never_reaches_a_chat_system_prompt(self):
        self.seed_day(USER_A, DAY_ONE, texts=["Prefers short spoken replies."])
        row = await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)
        self.assertEqual(row["summary"], DAILY_MARKER)

        for mode in MODE_REGISTRY:
            prompt = _build_system_prompt(mode)
            self.assertNotIn(DAILY_MARKER, prompt, msg=mode)
            self.assertNotIn(row["summary"], prompt, msg=mode)
            self.assertNotIn("personalization_summaries", prompt, msg=mode)

    def test_prompt_policy_hierarchy_is_unchanged(self):
        composed = compose_system_prompt("Mode instructions sentinel.")
        identity_at = composed.index(IDENTITY)
        epistemic_at = composed.index(EPISTEMIC_GROUNDING)
        feasibility_at = composed.index(FEASIBILITY_AND_NON_SYCOPHANCY)
        mode_at = composed.index("Mode instructions sentinel.")
        self.assertLess(identity_at, epistemic_at)
        self.assertLess(epistemic_at, feasibility_at)
        self.assertLess(feasibility_at, mode_at)


class ProposalTests(PersonalizationStoreTestCase):
    def _payload(self, **overrides) -> str:
        import json

        body = {
            "proposed_instructions": f"Keep replies brief. {PROPOSAL_MARKER}",
            "reasoning": "Weekly summaries repeatedly show a preference for brevity.",
            "risks": "Two weeks of evidence may be too thin to generalize.",
        }
        body.update(overrides)
        return json.dumps(body)

    async def _seed_weekly(self, user_id: str = USER_A) -> dict:
        self.seed_day(user_id, DAY_ONE, texts=["Day one."])
        await self.build_daily(user_id, DAY_ONE, marker=DAILY_MARKER)
        with patch.object(summarize, "generate_summary_text", return_value=ROLLUP_MARKER):
            return await summarize.build_summary(
                user_id, scope="weekly", period_start=DAY_ONE, period_end=DAY_TWO
            )

    async def test_proposal_is_pending_with_real_evidence(self):
        weekly = await self._seed_weekly()
        daily = await store.get_summary(
            USER_A, scope="daily", period_start=DAY_ONE, period_end=DAY_ONE
        )

        with patch.object(
            proposals, "generate_proposal_json", return_value=self._payload()
        ):
            row = await proposals.build_proposal(
                USER_A, mode="fitness", period_start=DAY_ONE, period_end=DAY_TWO
            )

        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["final_instructions"])
        self.assertIsNone(row["reviewed_at"])
        self.assertIsNone(row["reviewed_by"])
        self.assertIsNone(row["applied_override_id"])
        self.assertIn(PROPOSAL_MARKER, row["proposed_instructions"])
        self.assertEqual(row["evidence"]["source_summary_ids"], [str(weekly["id"])])
        # Weekly rows carry no conversation ids of their own.
        self.assertEqual(row["evidence"]["source_conversation_ids"], [])
        self.assertEqual(row["evidence"]["source_scope"], "weekly")
        self.assertIsNotNone(daily)
        self.pool_mock.assert_not_called()

    async def test_proposal_evidence_matches_multi_day_conversation_chain(self):
        convo_id = self.seed_day(USER_A, DAY_ONE, texts=["Day one."])
        await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)
        with patch.object(summarize, "generate_summary_text", return_value=ROLLUP_MARKER):
            await summarize.build_summary(
                USER_A, scope="multi_day", period_start=DAY_ONE, period_end=DAY_TWO
            )
        # Give the multi_day row a conversation chain the proposal can surface.
        multi = await store.get_summary(
            USER_A, scope="multi_day", period_start=DAY_ONE, period_end=DAY_TWO
        )
        await store.upsert_summary(
            USER_A,
            scope="multi_day",
            period_start=DAY_ONE,
            period_end=DAY_TWO,
            summary=multi["summary"],
            source_conversation_ids=[convo_id],
            source_summary_ids=multi["source_summary_ids"],
            model_identifier=multi["model_identifier"],
        )

        with patch.object(
            proposals, "generate_proposal_json", return_value=self._payload()
        ):
            row = await proposals.build_proposal(
                USER_A, period_start=DAY_ONE, period_end=DAY_TWO
            )

        self.assertEqual(row["evidence"]["source_scope"], "multi_day")
        self.assertEqual(row["evidence"]["source_conversation_ids"], [convo_id])

    async def test_no_summaries_means_no_proposal(self):
        with patch.object(
            proposals, "generate_proposal_json", return_value=self._payload()
        ) as call:
            row = await proposals.build_proposal(
                USER_A, period_start=DAY_ONE, period_end=DAY_TWO
            )
        self.assertIsNone(row)
        call.assert_not_called()
        self.assertEqual(store._memory.proposals, {})

    async def test_garbage_model_output_stores_nothing(self):
        await self._seed_weekly()
        for bad in ("not json at all", "[1, 2, 3]", '{"reasoning": "no instructions"}'):
            with self.subTest(bad=bad):
                with patch.object(proposals, "generate_proposal_json", return_value=bad):
                    with self.assertRaises(proposals.ProposalParseError):
                        await proposals.build_proposal(
                            USER_A, period_start=DAY_ONE, period_end=DAY_TWO
                        )
        self.assertEqual(store._memory.proposals, {})

    async def test_second_pending_proposal_for_same_target_is_rejected(self):
        await self._seed_weekly()
        with patch.object(
            proposals, "generate_proposal_json", return_value=self._payload()
        ):
            await proposals.build_proposal(
                USER_A, mode="fitness", period_start=DAY_ONE, period_end=DAY_TWO
            )
            with self.assertRaises(store.PendingProposalExistsError):
                await proposals.build_proposal(
                    USER_A, mode="fitness", period_start=DAY_ONE, period_end=DAY_TWO
                )
            # A different target is unaffected.
            other = await proposals.build_proposal(
                USER_A, mode="diet", period_start=DAY_ONE, period_end=DAY_TWO
            )
        self.assertEqual(other["status"], "pending")

    async def test_proposals_are_not_visible_across_users(self):
        await self._seed_weekly()
        with patch.object(
            proposals, "generate_proposal_json", return_value=self._payload()
        ):
            row = await proposals.build_proposal(
                USER_A, period_start=DAY_ONE, period_end=DAY_TWO
            )
        self.assertIsNone(await store.get_proposal(str(row["id"]), USER_B))
        self.assertEqual(await store.list_pending_proposals(USER_B), [])
        self.assertEqual(len(await store.list_pending_proposals(USER_A)), 1)

    def test_json_parsing_tolerates_code_fences(self):
        fenced = "```json\n" + self._payload() + "\n```"
        parsed = proposals.parse_proposal_json(fenced)
        self.assertIn(PROPOSAL_MARKER, parsed["proposed_instructions"])
        self.assertTrue(parsed["reasoning"])
        self.assertTrue(parsed["risks"])


class NoOverrideWriteGuardTests(unittest.TestCase):
    """Source-level CI guard: the invariant is grep-enforceable, not just tested."""

    MUTATION = re.compile(
        r"\b(insert\s+into|update|delete\s+from)\s+(public\.)?user_prompt_overrides\b",
        re.IGNORECASE,
    )
    KEYWORD = re.compile(r"\b(insert|update|delete)\b", re.IGNORECASE)

    def _sources(self) -> list[Path]:
        return sorted(PACKAGE_DIR.glob("*.py")) + [RUNNER_PATH]

    def test_package_never_mutates_user_prompt_overrides(self):
        for path in self._sources():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertIsNone(
                    self.MUTATION.search(text),
                    msg=f"{path.name} contains a write against user_prompt_overrides",
                )
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if "user_prompt_overrides" in line and self.KEYWORD.search(line):
                        self.fail(
                            f"{path.name}:{lineno} mentions user_prompt_overrides "
                            "alongside a mutation keyword"
                        )

    def test_package_does_not_import_prompt_assembly(self):
        for path in self._sources():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("shared.prompt_overrides", text)
                self.assertNotIn("from main import", text)

    def test_proposals_module_states_the_invariant(self):
        text = (PACKAGE_DIR / "proposals.py").read_text(encoding="utf-8")
        header = " ".join(text[: text.index("from __future__")].split()).lower()
        self.assertIn("never silently modify its own system prompt", header)
        self.assertIn("status='pending'", header)


class ModelConfigurationTests(unittest.TestCase):
    def test_summary_model_defaults_to_a_cheap_model(self):
        import main

        with patch.dict(os.environ, {"PERSONALIZATION_SUMMARY_MODEL": ""}):
            self.assertEqual(summarize.summary_model(), "claude-haiku-4-5")
            self.assertNotEqual(summarize.summary_model(), main.MODEL)

    def test_summary_model_is_configurable(self):
        with patch.dict(os.environ, {"PERSONALIZATION_SUMMARY_MODEL": "claude-test-1"}):
            self.assertEqual(summarize.summary_model(), "claude-test-1")

    def test_proposal_model_falls_back_to_summary_model(self):
        with patch.dict(
            os.environ,
            {
                "PERSONALIZATION_PROPOSAL_MODEL": "",
                "PERSONALIZATION_SUMMARY_MODEL": "claude-test-1",
            },
        ):
            self.assertEqual(proposals.proposal_model(), "claude-test-1")

    def test_context_bounds_are_configurable_with_safe_fallbacks(self):
        with patch.dict(
            os.environ,
            {"PERSONALIZATION_MAX_CONVERSATIONS": "5", "PERSONALIZATION_MAX_CHARS": "999"},
        ):
            self.assertEqual(summarize.max_conversations(), 5)
            self.assertEqual(summarize.max_chars(), 999)
        with patch.dict(
            os.environ,
            {"PERSONALIZATION_MAX_CONVERSATIONS": "0", "PERSONALIZATION_MAX_CHARS": "junk"},
        ):
            self.assertEqual(
                summarize.max_conversations(), summarize.DEFAULT_MAX_CONVERSATIONS
            )
            self.assertEqual(summarize.max_chars(), summarize.DEFAULT_MAX_CHARS)


class RunnerInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = _load_runner()

    def test_parser_exposes_required_flags(self):
        parser = self.runner.build_parser()
        flags = {
            action.option_strings[0]
            for action in parser._actions
            if action.option_strings
        }
        for flag in ("--user-id", "--scope", "--date", "--period-start",
                     "--period-end", "--propose", "--dry-run", "--mode"):
            self.assertIn(flag, flags)

    def test_daily_scope_resolves_a_single_date(self):
        args = argparse.Namespace(
            scope="daily", date="2026-08-10", period_start=None, period_end=None
        )
        self.assertEqual(self.runner.resolve_period(args), (DAY_ONE, DAY_ONE))

    def test_rollup_scope_requires_a_period(self):
        args = argparse.Namespace(
            scope="weekly", date=None, period_start=None, period_end=None
        )
        with self.assertRaises(SystemExit):
            self.runner.resolve_period(args)

    def test_reversed_period_is_rejected(self):
        args = argparse.Namespace(
            scope="weekly",
            date=None,
            period_start="2026-08-11",
            period_end="2026-08-10",
        )
        with self.assertRaises(SystemExit):
            self.runner.resolve_period(args)


if __name__ == "__main__":
    unittest.main()
