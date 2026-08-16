"""Personalization foundation (018) — summaries, pending proposals, invariants.

Fully offline: memory store + patched model functions. The load-bearing case is
that a summary is evidence only and that nothing here can write an active
prompt override. `NoOverrideWriteGuardTests` scans the whole repo, not just this
package, because "the backend never writes user_prompt_overrides" is a
repo-wide claim.

Run:  python -m unittest tests.test_personalization_v1 -v
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import asyncpg

from main import MODE_REGISTRY, _build_system_prompt
from shared.epistemic import (
    EPISTEMIC_GROUNDING,
    FEASIBILITY_AND_NON_SYCOPHANCY,
    compose_system_prompt,
)
from shared.identity import IDENTITY
from shared.personalization import proposals, store, summarize

REPO = Path(__file__).resolve().parents[1]
MIGRATION_014 = REPO / "migrations" / "014_user_prompt_overrides_admin_contract.sql"
MIGRATION_018 = REPO / "migrations" / "018_personalization.sql"
CONTRACT_DOC = REPO / "docs" / "PERSONALIZATION_PROPOSALS_V1_CONTRACT.md"
PACKAGE_DIR = REPO / "shared" / "personalization"
RUNNER_PATH = REPO / "scripts" / "run_personalization_rollup.py"

# Directories the repo-wide source guard never walks into. `tests` is excluded
# because this file deliberately spells out the forbidden SQL patterns in order
# to prove the guard detects them.
GUARD_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "tests",
    }
)

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


def modes_in_nullable_check(sql: str, constraint: str) -> set[str]:
    """Allowlist literals from an inline `mode IS NULL OR mode IN (...)` CHECK.

    Both nullable mode allowlists (`user_prompt_overrides_mode_chk` in 014 and
    `prompt_change_proposals_mode_chk` in 018) use this exact shape, so a single
    parser covers both. Keep the SQL in this form — see the mode-drift note in
    tests/test_migration_repro.py.
    """
    match = re.search(
        rf"CONSTRAINT {re.escape(constraint)} CHECK \(\s*"
        rf"mode IS NULL\s*OR mode IN \((.*?)\)\s*\)",
        sql,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"constraint {constraint} not found in the expected form")
    return set(re.findall(r"'([^']+)'", match.group(1)))


def json_blocks(markdown: str) -> list[dict]:
    """Every ```json fenced object in a doc, parsed."""
    return [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)```", markdown, re.DOTALL)
    ]


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
        overrides_sql = MIGRATION_014.read_text(encoding="utf-8")
        self.assertEqual(
            modes_in_nullable_check(self.sql, "prompt_change_proposals_mode_chk"),
            modes_in_nullable_check(overrides_sql, "user_prompt_overrides_mode_chk"),
        )

    def test_proposal_mode_check_matches_mode_registry_exactly(self):
        """Mode-drift guard for 018's CHECK, which test_migration_repro misses.

        `MODE_CONSTRAINTS` there covers conversations / action_log /
        pending_actions only. Without this, adding a mode to MODE_REGISTRY would
        leave 018's allowlist stale and proposals for the new mode would fail at
        insert with nothing catching it.
        """
        allowed = modes_in_nullable_check(
            self.sql, "prompt_change_proposals_mode_chk"
        )
        registry = set(MODE_REGISTRY)
        self.assertEqual(
            allowed,
            registry,
            msg=(
                "018 prompt_change_proposals_mode_chk does not match MODE_REGISTRY: "
                f"missing_from_db={sorted(registry - allowed)} "
                f"extra_in_db={sorted(allowed - registry)}"
            ),
        )
        # The Python-side allowlist the writer validates against must agree too.
        self.assertEqual(allowed, set(store.PROPOSAL_MODES))
        self.assertNotIn("health", allowed)


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


MUTATION = re.compile(
    r"\b(insert\s+into|update|delete\s+from)\s+(public\.)?user_prompt_overrides\b",
    re.IGNORECASE,
)
MUTATION_KEYWORD = re.compile(r"\b(insert|update|delete)\b", re.IGNORECASE)


def find_override_writes(text: str) -> list[str]:
    """Every place in `text` that looks like a write to user_prompt_overrides."""
    offenses: list[str] = []
    statement = MUTATION.search(text)
    if statement is not None:
        offenses.append(f"SQL mutation {' '.join(statement.group(0).split())!r}")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if "user_prompt_overrides" in line and MUTATION_KEYWORD.search(line):
            offenses.append(f"line {lineno}: {line.strip()!r}")
    return offenses


def repo_python_sources() -> list[Path]:
    """Every .py file in the repo except tests and virtualenvs.

    Walked with pruning rather than rglob so the scan never descends into
    .venv — it stays a few milliseconds even though its scope is the whole repo.
    """
    found: list[Path] = []
    for root, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [name for name in dirnames if name not in GUARD_SKIP_DIRS]
        found.extend(
            Path(root) / name for name in filenames if name.endswith(".py")
        )
    return sorted(found)


def package_python_sources() -> list[Path]:
    return sorted(PACKAGE_DIR.glob("*.py")) + [RUNNER_PATH]


class NoOverrideWriteGuardTests(unittest.TestCase):
    """Source-level CI guard, scoped to match the invariant it is named for.

    "The backend never writes user_prompt_overrides" is a repo-wide claim, so the
    scan is repo-wide. A write added to shared/db.py, a new router, or a new
    script fails here, not just one added to shared/personalization/.
    """

    # Writes that do not exist anywhere in the repo — they are here to prove the
    # guard would actually catch them. This file is outside the scan (see
    # GUARD_SKIP_DIRS) precisely so these samples do not trip it.
    OFFENDING_SAMPLES = (
        'await db.pool().execute("INSERT INTO user_prompt_overrides (user_id) '
        'VALUES ($1)")',
        "UPDATE user_prompt_overrides SET is_active = true WHERE user_id = $1",
        "DELETE FROM public.user_prompt_overrides WHERE id = $1",
        'execute(\n    """\n    INSERT INTO\n        user_prompt_overrides (user_id)\n'
        '    VALUES ($1)\n    """\n)',
        "# admin will UPDATE user_prompt_overrides for us",
    )

    def test_repo_never_mutates_user_prompt_overrides(self):
        for path in repo_python_sources():
            relative = path.relative_to(REPO)
            with self.subTest(path=str(relative)):
                offenses = find_override_writes(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    offenses,
                    [],
                    msg=f"{relative} writes user_prompt_overrides: {offenses}",
                )

    def test_guard_scope_is_the_whole_repo(self):
        scanned = set(repo_python_sources())
        for required in (
            REPO / "main.py",
            REPO / "shared" / "db.py",
            REPO / "shared" / "prompt_overrides.py",
            PACKAGE_DIR / "store.py",
            PACKAGE_DIR / "proposals.py",
            RUNNER_PATH,
        ):
            self.assertIn(required, scanned, msg=f"{required} escaped the guard")
        self.assertGreater(len(scanned), 20, msg="scan collected suspiciously few files")
        self.assertNotIn(Path(__file__).resolve(), scanned)
        for path in scanned:
            self.assertFalse(
                GUARD_SKIP_DIRS & set(path.relative_to(REPO).parts),
                msg=f"{path} should have been pruned",
            )

    def test_guard_detects_writes_that_do_not_exist_today(self):
        for sample in self.OFFENDING_SAMPLES:
            with self.subTest(sample=" ".join(sample.split())[:60]):
                self.assertNotEqual(
                    find_override_writes(sample),
                    [],
                    msg="the guard would not catch this write",
                )
        # Reads and prose that merely name the table stay clean.
        for allowed in (
            "SELECT instructions FROM user_prompt_overrides WHERE user_id = $1",
            "# this package never writes the user_prompt_overrides table",
        ):
            with self.subTest(allowed=allowed[:40]):
                self.assertEqual(find_override_writes(allowed), [])

    def test_package_does_not_import_prompt_assembly(self):
        for path in package_python_sources():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("shared.prompt_overrides", text)
                self.assertNotIn("from main import", text)


class PendingOnlyProposalWriteTests(PersonalizationStoreTestCase):
    """The only proposal-writing entry point cannot emit a non-pending row.

    Replaces an earlier test that asserted on docstring prose. These exercise the
    real write path instead: there is no status argument to abuse, review columns
    are not writable at insert, and nothing smuggled through `evidence` changes
    the stored status.
    """

    def _kwargs(self, **overrides) -> dict:
        kwargs = {
            "mode": "fitness",
            "proposed_instructions": f"Keep replies brief. {PROPOSAL_MARKER}",
            "reasoning": "Weekly summaries repeatedly show a preference for brevity.",
            "evidence": {"source_summary_ids": [], "source_conversation_ids": []},
            "risks": None,
            "model_identifier": "claude-test-1",
        }
        kwargs.update(overrides)
        return kwargs

    async def test_review_columns_are_not_accepted_arguments(self):
        for forbidden in (
            {"status": "approved"},
            {"status": "applied"},
            {"reviewed_by": "attacker"},
            {"reviewed_at": "2026-08-11T09:16:00Z"},
            {"final_instructions": "self-approved"},
            {"applied_override_id": USER_B},
        ):
            with self.subTest(forbidden=sorted(forbidden)):
                with self.assertRaises(TypeError):
                    await store.insert_pending_proposal(
                        USER_A, **self._kwargs(**forbidden)
                    )
        self.assertEqual(store._memory.proposals, {})

    async def test_stored_row_is_pending_with_empty_review_columns(self):
        row = await store.insert_pending_proposal(USER_A, **self._kwargs())
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["final_instructions"])
        self.assertIsNone(row["reviewed_at"])
        self.assertIsNone(row["reviewed_by"])
        self.assertIsNone(row["applied_override_id"])

    async def test_status_smuggled_through_evidence_changes_nothing(self):
        row = await store.insert_pending_proposal(
            USER_A,
            **self._kwargs(
                evidence={
                    "status": "applied",
                    "reviewed_by": "attacker",
                    "final_instructions": "self-approved",
                    "source_summary_ids": [],
                }
            ),
        )
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["reviewed_by"])
        self.assertIsNone(row["final_instructions"])
        # The payload is stored verbatim as opaque evidence, never interpreted.
        self.assertEqual(row["evidence"]["status"], "applied")

    def test_store_exposes_no_proposal_mutation_helper(self):
        proposal_callables = {
            name
            for name in dir(store)
            if "proposal" in name.lower() and callable(getattr(store, name))
        }
        self.assertEqual(
            proposal_callables,
            {
                "PendingProposalExistsError",
                "insert_pending_proposal",
                "get_proposal",
                "list_pending_proposals",
                "serialize_proposal",
            },
            msg="a new proposal helper appeared; confirm it cannot resolve a review",
        )


class ProposalPostgresWritePathTests(unittest.IsolatedAsyncioTestCase):
    """The Postgres branch itself, with a recording stub instead of a database.

    The memory store cannot prove what SQL ships, so this exercises the real
    asyncpg code path: `status` must be a literal in the statement, and the
    review columns must not appear in the INSERT at all.
    """

    def setUp(self):
        store.use_memory_store(False)
        self.addCleanup(lambda: store.use_memory_store(False))
        self.calls: list[tuple[str, tuple]] = []
        self.error: Exception | None = None
        recorder = self

        class _StubConnection:
            async def fetchrow(self, query, *args):
                recorder.calls.append((query, args))
                if recorder.error is not None:
                    raise recorder.error
                return {
                    "id": "c3d4e5f6-4444-4a2b-8c3d-9e0f11223344",
                    "user_id": USER_A,
                    "mode": "fitness",
                    "proposed_instructions": PROPOSAL_MARKER,
                    "final_instructions": None,
                    "reasoning": "because",
                    "evidence": "{}",
                    "risks": None,
                    "status": "pending",
                    "model_identifier": "claude-test-1",
                    "created_at": None,
                    "reviewed_at": None,
                    "reviewed_by": None,
                    "applied_override_id": None,
                }

        patcher = patch("shared.db.pool", return_value=_StubConnection())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _insert(self, **overrides):
        kwargs = {
            "mode": "fitness",
            "proposed_instructions": f"Keep replies brief. {PROPOSAL_MARKER}",
            "reasoning": "Weekly summaries repeatedly show a preference for brevity.",
            "evidence": {"source_summary_ids": []},
            "risks": None,
            "model_identifier": "claude-test-1",
        }
        kwargs.update(overrides)
        return await store.insert_pending_proposal(USER_A, **kwargs)

    def _insert_bindings(self) -> dict[str, str]:
        query, _args = self.calls[0]
        match = re.search(
            r"INSERT INTO prompt_change_proposals \((.*?)\)\s*VALUES \((.*?)\)",
            query,
            re.DOTALL,
        )
        self.assertIsNotNone(match, msg=f"unrecognized INSERT statement: {query}")
        columns = [part.strip() for part in match.group(1).split(",")]
        values = [part.strip() for part in match.group(2).split(",")]
        self.assertEqual(len(columns), len(values), msg=query)
        return dict(zip(columns, values))

    async def test_status_is_a_sql_literal_not_a_bound_parameter(self):
        await self._insert()
        bindings = self._insert_bindings()
        self.assertEqual(bindings["status"], "'pending'")

        _query, args = self.calls[0]
        for value in args:
            self.assertNotIn(
                value,
                store.PROPOSAL_STATUSES,
                msg="a status string reached the INSERT as a bound parameter",
            )

    async def test_review_columns_are_absent_from_the_insert(self):
        await self._insert()
        bindings = self._insert_bindings()
        for column in (
            "final_instructions",
            "reviewed_at",
            "reviewed_by",
            "applied_override_id",
        ):
            self.assertNotIn(column, bindings, msg=f"{column} is written at insert")

    async def test_unique_violation_becomes_pending_proposal_exists_error(self):
        """The 23505 handling section 6 of the contract promises admin."""
        self.error = asyncpg.UniqueViolationError(
            "duplicate key value violates unique constraint "
            '"prompt_change_proposals_one_pending_per_user_mode"'
        )
        with self.assertRaises(store.PendingProposalExistsError) as caught:
            await self._insert()
        self.assertEqual(caught.exception.user_id, USER_A)
        self.assertEqual(caught.exception.mode, "fitness")
        self.assertIn("resolve it before creating another", str(caught.exception))


class ContractDocTests(PersonalizationStoreTestCase):
    """The doc Oliver admin builds from must match what the code actually writes.

    An admin agent copies payload shapes straight out of this file, so a stale
    example is a real integration bug, not a typo.
    """

    def setUp(self):
        super().setUp()
        self.assertTrue(CONTRACT_DOC.is_file(), "contract doc missing")
        self.doc = CONTRACT_DOC.read_text(encoding="utf-8")
        self.sql = MIGRATION_018.read_text(encoding="utf-8")

    async def _real_proposal(self) -> dict:
        self.seed_day(USER_A, DAY_ONE, texts=["Day one."])
        await self.build_daily(USER_A, DAY_ONE, marker=DAILY_MARKER)
        with patch.object(summarize, "generate_summary_text", return_value=ROLLUP_MARKER):
            await summarize.build_summary(
                USER_A, scope="weekly", period_start=DAY_ONE, period_end=DAY_TWO
            )
        payload = json.dumps(
            {
                "proposed_instructions": f"Keep replies brief. {PROPOSAL_MARKER}",
                "reasoning": "Weekly summaries show a preference for brevity.",
                "risks": "Two weeks of evidence may be too thin.",
            }
        )
        with patch.object(proposals, "generate_proposal_json", return_value=payload):
            return await proposals.build_proposal(
                USER_A, mode="fitness", period_start=DAY_ONE, period_end=DAY_TWO
            )

    async def test_every_documented_evidence_shape_matches_the_real_payload(self):
        row = await self._real_proposal()
        real_keys = set(row["evidence"])
        # Guards against the payload silently shrinking as well as the doc drifting.
        self.assertEqual(
            real_keys,
            {
                "source_summary_ids",
                "source_conversation_ids",
                "period_start",
                "period_end",
                "source_scope",
            },
        )

        checked = 0
        for block in json_blocks(self.doc):
            if "evidence" in block:
                self.assertEqual(
                    set(block["evidence"]),
                    real_keys,
                    msg="example proposal row's evidence does not match the writer",
                )
                checked += 1
            elif "source_scope" in block:
                self.assertEqual(
                    set(block),
                    real_keys,
                    msg="the §3 evidence shape block does not match the writer",
                )
                checked += 1
        self.assertEqual(checked, 2, msg="expected both documented evidence shapes")

    async def test_documented_example_rows_match_the_serializers(self):
        row = await self._real_proposal()
        summary = await store.get_summary(
            USER_A, scope="weekly", period_start=DAY_ONE, period_end=DAY_TWO
        )

        proposal_keys = set(store.serialize_proposal(row)) | {"user_id"}
        summary_keys = set(store.serialize_summary(summary)) | {"user_id"}

        checked = set()
        for block in json_blocks(self.doc):
            if "evidence" in block:
                self.assertEqual(set(block), proposal_keys)
                checked.add("proposal")
            elif "summary" in block:
                self.assertEqual(set(block), summary_keys)
                checked.add("summary")
        self.assertEqual(checked, {"proposal", "summary"})

    def test_doc_names_every_database_object_in_018(self):
        named = set(re.findall(r"CONSTRAINT (\w+) CHECK", self.sql))
        named |= set(re.findall(r"CONSTRAINT (\w+) UNIQUE", self.sql))
        named |= set(
            re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", self.sql)
        )
        named |= set(re.findall(r"CREATE TRIGGER (\w+)", self.sql))
        self.assertGreaterEqual(len(named), 13, msg=f"parsed too few objects: {named}")
        for name in sorted(named):
            with self.subTest(object=name):
                self.assertIn(
                    name,
                    self.doc,
                    msg=f"{name} exists in 018 but is undocumented for admin",
                )

    def test_doc_mode_allowlist_matches_the_code(self):
        match = re.search(
            r"Allowed `mode` values[^\n]*\n\n([^\n]+)\n", self.doc
        )
        self.assertIsNotNone(match, msg="mode allowlist line not found in the doc")
        documented = set(re.findall(r"`([a-z_]+)`", match.group(1)))
        self.assertEqual(documented, set(store.PROPOSAL_MODES))
        self.assertEqual(documented, set(MODE_REGISTRY))

    def test_doc_states_the_sqlstates_admin_will_see(self):
        for code, name in (
            ("23505", "unique_violation"),
            ("23514", "check_violation"),
            ("23001", "restrict_violation"),
        ):
            with self.subTest(sqlstate=code):
                self.assertIn(code, self.doc)
                self.assertIn(name, self.doc)

    def test_doc_pending_index_ddl_matches_the_migration(self):
        ddl = re.search(
            r"CREATE UNIQUE INDEX IF NOT EXISTS "
            r"prompt_change_proposals_one_pending_per_user_mode\s*\n"
            r"\s*ON prompt_change_proposals \(user_id, \(COALESCE\(mode, ''\)\)\)\s*\n"
            r"\s*WHERE status = 'pending';",
            self.sql,
        )
        self.assertIsNotNone(ddl, msg="018's pending index DDL changed shape")
        normalized_doc = " ".join(self.doc.split())
        self.assertIn(" ".join(ddl.group(0).split()), normalized_doc)

    def test_doc_reviewer_check_matches_the_migration(self):
        check = re.search(
            r"CONSTRAINT prompt_change_proposals_reviewer_required_chk CHECK \(\s*"
            r"(.*?)\s*\)\n\);",
            self.sql,
            re.DOTALL,
        )
        self.assertIsNotNone(check)
        normalized_doc = " ".join(self.doc.split())
        self.assertIn(" ".join(check.group(1).split()), normalized_doc)


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
