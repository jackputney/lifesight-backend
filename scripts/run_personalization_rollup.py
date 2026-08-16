#!/usr/bin/env python3
"""Build personalization summaries (and optionally a pending proposal) for one user.

Deterministic manual runner — no scheduler in this slice. Re-running the same
period is idempotent: `personalization_summaries` is unique on
(user_id, scope, period_start, period_end) and the runner upserts, so a repeat
run overwrites that one row instead of adding another.

Usage examples:
  python scripts/run_personalization_rollup.py \\
    --user-id 00000000-0000-4000-8000-000000000001 --scope daily --date 2026-08-10

  python scripts/run_personalization_rollup.py \\
    --user-id <uuid> --scope multi_day \\
    --period-start 2026-08-08 --period-end 2026-08-10

  python scripts/run_personalization_rollup.py \\
    --user-id <uuid> --scope weekly \\
    --period-start 2026-08-04 --period-end 2026-08-10 --propose --mode fitness

  python scripts/run_personalization_rollup.py \\
    --user-id <uuid> --scope weekly \\
    --period-start 2026-08-04 --period-end 2026-08-10 --dry-run

--propose is opt-in and only ever creates a status='pending'
prompt_change_proposals row for human review in Oliver admin. This runner never
writes the user_prompt_overrides table.

--dry-run reads the inputs, prints counts and ids, and writes nothing — it also
skips the model calls, so it costs nothing.

Requires DATABASE_URL (and ANTHROPIC_API_KEY unless --dry-run). Output is
limited to ids and counts; summary and proposal text is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from shared import db  # noqa: E402
from shared.personalization import proposals, store, summarize  # noqa: E402


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"invalid date {raw!r} — expected YYYY-MM-DD")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build LifeSight personalization summaries for one user."
    )
    p.add_argument("--user-id", required=True, help="LifeSight users.id UUID")
    p.add_argument(
        "--scope",
        required=True,
        choices=list(store.SUMMARY_SCOPES),
        help="daily reads raw conversations; multi_day/weekly roll up summaries",
    )
    p.add_argument("--date", help="YYYY-MM-DD — the single day for --scope daily")
    p.add_argument("--period-start", help="YYYY-MM-DD inclusive")
    p.add_argument("--period-end", help="YYYY-MM-DD inclusive")
    p.add_argument(
        "--mode",
        choices=list(store.PROPOSAL_MODES),
        help="Target mode for --propose (omit for a global proposal)",
    )
    p.add_argument(
        "--propose",
        action="store_true",
        help="Also generate a pending prompt change proposal for human review",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written (ids and counts only); write nothing",
    )
    return p


def resolve_period(args: argparse.Namespace) -> tuple[date, date]:
    if args.scope == "daily":
        raw = args.date or args.period_start
        if not raw:
            raise SystemExit("--scope daily requires --date (or --period-start)")
        day = _parse_date(raw)
        if args.period_end and _parse_date(args.period_end) != day:
            raise SystemExit("--scope daily covers exactly one date")
        return day, day

    if args.date:
        raise SystemExit("--date is only valid with --scope daily")
    if not args.period_start or not args.period_end:
        raise SystemExit(
            f"--scope {args.scope} requires --period-start and --period-end"
        )
    start = _parse_date(args.period_start)
    end = _parse_date(args.period_end)
    if end < start:
        raise SystemExit("--period-end must be on or after --period-start")
    return start, end


async def _dry_run(args: argparse.Namespace, start: date, end: date) -> int:
    inputs = await summarize.collect_inputs(
        args.user_id, scope=args.scope, period_start=start, period_end=end
    )
    plan = inputs.plan()
    print(
        f"dry-run summary scope={plan['scope']} "
        f"period={plan['period_start']}..{plan['period_end']} "
        f"sources={plan['source_count']} chars={plan['material_chars']} "
        f"truncated={plan['truncated']} model={summarize.summary_model()}"
    )
    print(f"  source_conversation_ids={plan['source_conversation_ids']}")
    print(f"  source_summary_ids={plan['source_summary_ids']}")
    if not inputs.has_material:
        print("  nothing to summarize — no row would be written")

    if args.propose:
        rows = await proposals.collect_proposal_sources(
            args.user_id, period_start=start, period_end=end
        )
        evidence = proposals.build_evidence(rows)
        print(
            f"dry-run proposal mode={args.mode} status=pending "
            f"source_summaries={len(rows)} model={proposals.proposal_model()}"
        )
        print(f"  evidence={evidence}")
        if not rows:
            print("  no weekly/multi_day summaries — no proposal would be written")
    return 0


async def _run(args: argparse.Namespace) -> int:
    start, end = resolve_period(args)

    await db.init_pool()
    try:
        if args.dry_run:
            return await _dry_run(args, start, end)

        row = await summarize.build_summary(
            args.user_id, scope=args.scope, period_start=start, period_end=end
        )
        if row is None:
            print(
                f"no input for scope={args.scope} period={start}..{end} "
                "— no summary written"
            )
        else:
            summary = store.serialize_summary(row)
            print(
                f"summary id={summary['id']} scope={summary['scope']} "
                f"period={summary['period_start']}..{summary['period_end']} "
                f"model={summary['model_identifier']} "
                f"conversations={len(summary['source_conversation_ids'])} "
                f"summaries={len(summary['source_summary_ids'])}"
            )

        if not args.propose:
            return 0

        try:
            proposal_row = await proposals.build_proposal(
                args.user_id, mode=args.mode, period_start=start, period_end=end
            )
        except store.PendingProposalExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except proposals.ProposalError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3

        if proposal_row is None:
            print("no weekly/multi_day summaries in period — no proposal written")
            return 0

        proposal = store.serialize_proposal(proposal_row)
        print(
            f"proposal id={proposal['id']} mode={proposal['mode']} "
            f"status={proposal['status']} model={proposal['model_identifier']} "
            f"evidence_summaries="
            f"{len(proposal['evidence'].get('source_summary_ids', []))}"
        )
        print("  awaiting human review in Oliver admin (nothing was applied)")
        return 0
    finally:
        await db.close_pool()


async def main() -> int:
    return await _run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
