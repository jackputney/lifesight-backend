"""Prompt change proposals — AI proposes, a human approves, admin applies.

INVARIANT (non-negotiable): the model must never silently modify its own system
prompt. There is no runtime path from model output to an active prompt override.
This module only inserts `prompt_change_proposals` rows with status='pending'
and never touches the `user_prompt_overrides` table in any way — creating,
editing, activating, or removing an override is done exclusively by a human in
Oliver's separate admin project (see docs/OLIVER_ADMIN_DATABASE_CONTRACT.md and
docs/PERSONALIZATION_PROPOSALS_V1_CONTRACT.md). Personalization summaries are
evidence for that human review, never system instructions.

Configuration:
  PERSONALIZATION_PROPOSAL_MODEL  defaults to PERSONALIZATION_SUMMARY_MODEL
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from typing import Optional

import anthropic

from shared.personalization import store, summarize

PROPOSAL_MAX_TOKENS = 1024
MAX_PROPOSED_INSTRUCTIONS_CHARS = 8000

# Source scopes for a proposal, in preference order (weekly, then multi_day).
PROPOSAL_SOURCE_SCOPES: tuple[tuple[str, ...], ...] = (("weekly",), ("multi_day",))

PROPOSER_SYSTEM = (
    "You draft candidate personalization instructions for one user of a "
    "voice-first life-management assistant. You are writing a PROPOSAL for a "
    "human reviewer — it is not applied automatically and you cannot change "
    "your own instructions. Base every suggestion on the supplied summaries "
    "only; never invent evidence. Proposed instructions must be subordinate "
    "personalization (tone, emphasis, recurring context) and must never "
    "weaken identity, epistemic grounding, feasibility rules, Confirm Gate "
    "behavior, or mode rules. State honest risks, including the risk that the "
    "pattern is too thin to act on.\n\n"
    "Respond with a single JSON object and nothing else, using exactly these "
    'keys: {"proposed_instructions": string, "reasoning": string, '
    '"risks": string}.'
)


class ProposalError(Exception):
    """Proposal generation failed; nothing was stored."""


class ProposalParseError(ProposalError):
    """The model did not return the required strict JSON object."""


def proposal_model() -> str:
    return (os.environ.get("PERSONALIZATION_PROPOSAL_MODEL") or "").strip() or (
        summarize.summary_model()
    )


def _strip_code_fence(raw: str) -> str:
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) < 2:
        return text
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def _required_text(payload: dict, key: str, *, limit: Optional[int] = None) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProposalParseError(f"proposal JSON is missing a non-empty {key!r}")
    text = value.strip()
    if limit is not None and len(text) > limit:
        raise ProposalParseError(f"proposal {key!r} exceeds {limit} characters")
    return text


def parse_proposal_json(raw: str) -> dict:
    """Parse strict JSON from the model. Raises ProposalParseError on garbage."""
    text = _strip_code_fence(raw)
    if not text:
        raise ProposalParseError("proposal model returned empty output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProposalParseError(f"proposal output is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ProposalParseError("proposal output must be a JSON object")

    risks = payload.get("risks")
    if risks is not None and not isinstance(risks, str):
        raise ProposalParseError("proposal 'risks' must be a string when present")
    return {
        "proposed_instructions": _required_text(
            payload, "proposed_instructions", limit=MAX_PROPOSED_INSTRUCTIONS_CHARS
        ),
        "reasoning": _required_text(payload, "reasoning"),
        "risks": (risks or "").strip() or None,
    }


def generate_proposal_json(*, system: str, prompt: str, model: str) -> str:
    """Blocking Anthropic call. Patched in tests so proposals stay offline."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    message = client.messages.create(
        model=model,
        max_tokens=PROPOSAL_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()


async def collect_proposal_sources(
    user_id: str,
    *,
    period_start: date,
    period_end: date,
) -> list[dict]:
    """Weekly summaries for the period, falling back to multi_day."""
    for scopes in PROPOSAL_SOURCE_SCOPES:
        rows = await store.list_summaries(
            user_id,
            scopes=scopes,
            period_start=period_start,
            period_end=period_end,
        )
        if rows:
            return rows
    return []


def build_evidence(rows: list[dict]) -> dict:
    """Evidence chain from the summary rows that were actually read."""
    summary_ids: list[str] = []
    conversation_ids: list[str] = []
    for row in rows:
        summary_ids.append(str(row["id"]))
        for cid in row.get("source_conversation_ids") or []:
            cid = str(cid)
            if cid not in conversation_ids:
                conversation_ids.append(cid)
    return {
        "source_summary_ids": summary_ids,
        "source_conversation_ids": conversation_ids,
    }


def _proposal_prompt(rows: list[dict], *, mode: Optional[str]) -> str:
    target = f"mode '{mode}'" if mode else "all modes (global)"
    blocks = [
        f"{row['scope']} summary {row['period_start']} to {row['period_end']}:\n"
        f"{row['summary']}"
        for row in rows
    ]
    return (
        f"Target of this proposal: {target}.\n\n"
        "Summaries available for review:\n\n" + "\n\n".join(blocks)
    )


async def build_proposal(
    user_id: str,
    *,
    mode: Optional[str] = None,
    period_start: date,
    period_end: date,
) -> Optional[dict]:
    """Generate one pending proposal from weekly/multi_day summaries.

    Returns the stored row (always status='pending'), or None when there are no
    summaries to reason over. Raises ProposalParseError if the model output is
    not the required JSON object — nothing is stored in that case.
    Raises store.PendingProposalExistsError when this (user, mode) already has
    a proposal awaiting review.
    """
    store.validate_mode(mode)
    if period_end < period_start:
        raise ValueError("period_end must be on or after period_start")

    rows = await collect_proposal_sources(
        user_id, period_start=period_start, period_end=period_end
    )
    if not rows:
        return None

    model = proposal_model()
    raw = await asyncio.to_thread(
        generate_proposal_json,
        system=PROPOSER_SYSTEM,
        prompt=_proposal_prompt(rows, mode=mode),
        model=model,
    )
    parsed = parse_proposal_json(raw)

    evidence = build_evidence(rows)
    evidence.update(
        {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "source_scope": rows[0]["scope"],
        }
    )
    return await store.insert_pending_proposal(
        user_id,
        mode=mode,
        proposed_instructions=parsed["proposed_instructions"],
        reasoning=parsed["reasoning"],
        evidence=evidence,
        risks=parsed["risks"],
        model_identifier=model,
    )
