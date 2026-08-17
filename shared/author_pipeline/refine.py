"""Refinement of raw captures into a derivative draft version, plus advisory flags.

The model never edits stored text: it returns a candidate `content` string and a
list of flags, and the caller inserts those as NEW rows. Captures are read-only
input here.

`call_model` is a module-level seam on purpose — tests patch it so the pipeline
runs deterministically and offline.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional

import anthropic
from fastapi import HTTPException

from shared.author_pipeline.store import (
    DEFAULT_REFINEMENT_LEVEL,
    FLAG_CATEGORIES,
    REFINEMENT_LEVELS,
)

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_OUTPUT_TOKENS = 8192

# Cost ceiling for one /refine call. A session accumulates captures forever, so
# without a bound on the ASSEMBLED prompt a single account can drive unbounded
# Anthropic spend by refining a huge range — and the model could not return a
# refinement that long anyway (MAX_OUTPUT_TOKENS above). Exceeding either bound
# is a 413 with the actual numbers, never a silent upstream call.
MAX_REFINE_CAPTURES = 500
MAX_REFINE_PROMPT_CHARS = 120_000
AUTHOR_REFINE_MAX_CHARS = MAX_REFINE_PROMPT_CHARS

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

VOICE_CONTRACT = """You are refining a writer's own dictated words for LifeSight.

The writer is the author. You are not. Absolute rules:
- Preserve the writer's actual meaning. Never add ideas, facts, or details they
  did not say, and never drop something they did say.
- Preserve their vocabulary and phrasing wherever it still reads. Keep their word
  choices, their rhythm, their sentence shapes, their humour, their bluntness.
- Preserve tone and distinctive voice. A reader who knows this writer must still
  hear this writer.
- Do NOT convert the text into generic polished AI prose. Smooth, neutral,
  corporate, or "improved" writing that no longer sounds like the author is a
  failure, even if it reads well.
- When in doubt, change less and raise a flag instead.

You also review the result. A flag EXPLAINS a possible problem so the author can
decide. You never silently fix writing the author owns: anything beyond your
refinement level belongs in a flag, not in the text."""

LEVEL_INSTRUCTIONS = {
    "light_cleanup": (
        "Refinement level: light_cleanup. Fix only dictation artifacts: "
        "speech-to-text mishearings that are obvious from context, stray filler "
        "words (um, uh, you know), false starts, doubled words, and missing "
        "sentence punctuation or capitalisation. Do not rewrite sentences, do not "
        "reorder anything, do not improve word choice."
    ),
    "preserve_voice": (
        "Refinement level: preserve_voice (the default). Do everything "
        "light_cleanup does, plus the lightest possible edit for readability: "
        "obvious grammar slips and sentence boundaries. Keep every distinctive "
        "phrase intact. If a sentence is awkward but unmistakably the author's, "
        "leave it and flag it."
    ),
    "polish": (
        "Refinement level: polish. Tighten the prose — trim redundancy, sharpen "
        "sentence structure, fix grammar — while the result still sounds like "
        "this author speaking. Keep their signature phrases verbatim. Tightening "
        "is allowed; neutralising the voice is not."
    ),
    "structural_rewrite": (
        "Refinement level: structural_rewrite. You may reorder sentences and "
        "paragraphs, merge or split them, and restore a logical flow the "
        "dictation lost. The author's meaning, argument, and voice must survive "
        "unchanged. Reorganise; do not re-author. Anything you had to move or "
        "cut significantly must also be flagged."
    ),
}

RESPONSE_FORMAT = """Reply with a single JSON object and nothing else. No prose,
no markdown fence, no commentary.

{
  "content": "the refined text",
  "flags": [
    {
      "category": "typo",
      "span_start": 12,
      "span_end": 19,
      "explanation": "spoken sentence explaining why this may be a problem",
      "suggested_change": "text that would replace that span"
    }
  ]
}

Rules for the JSON:
- "content" is the full refined text as one string. Never empty.
- "flags" is an array; use [] when nothing needs the author's attention. Never
  invent a flag to look thorough.
- "span_start" and "span_end" are character offsets into "content"
  (start inclusive, end exclusive). Use them whenever the issue sits in a
  specific stretch of text.
- If the issue is not localisable to a span, set both to null and omit
  "suggested_change" — a flag without a span is advisory only.
- Include "suggested_change" only when you also give a span, and only when you
  have a concrete replacement for exactly that span.
- "explanation" is read aloud to a writer who cannot see the screen. Write one
  natural spoken sentence saying what you noticed and why it may matter.
- "category" is one of: typo, grammar, repetition, tangent, unclear,
  contradiction, structure, other."""


def _anthropic() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")
    return anthropic.Anthropic(api_key=key)


def build_system_prompt(refinement_level: str) -> str:
    level = refinement_level if refinement_level in LEVEL_INSTRUCTIONS else DEFAULT_REFINEMENT_LEVEL
    return "\n\n".join([VOICE_CONTRACT, LEVEL_INSTRUCTIONS[level], RESPONSE_FORMAT])


def build_user_prompt(captures: list[dict]) -> str:
    """Numbered transcript so the model sees capture boundaries but returns one text."""
    lines = [
        "Here is the raw dictation, in order. Each line is one capture as it was "
        "recorded. Refine the whole range into a single continuous text.",
        "",
    ]
    for capture in captures:
        lines.append(f"[capture {int(capture['sequence'])} | {capture['source']}]")
        lines.append(str(capture["raw_text"]))
        lines.append("")
    return "\n".join(lines).strip()


def assert_within_prompt_budget(captures: list[dict], user_prompt: str) -> None:
    """Reject an oversized refine range before any of it reaches Anthropic."""
    if len(captures) > MAX_REFINE_CAPTURES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That range covers {len(captures)} captures; one refine call "
                f"takes at most {MAX_REFINE_CAPTURES}. Refine a narrower "
                "capture_from/capture_to range."
            ),
        )
    if len(user_prompt) > MAX_REFINE_PROMPT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That range assembles {len(user_prompt)} characters of "
                f"dictation; one refine call takes at most "
                f"{MAX_REFINE_PROMPT_CHARS}. Refine a narrower "
                "capture_from/capture_to range."
            ),
        )


def call_model(system_prompt: str, user_prompt: str) -> str:
    """Blocking Anthropic call returning the raw response text.

    Module-level seam: tests patch this so refinement is deterministic and offline.
    """
    client = _anthropic()
    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()


def _load_json_object(raw: str) -> dict:
    """Parse the model reply into a JSON object or fail loudly with 502."""
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=502, detail="Refinement model returned an empty response")

    candidates = [_JSON_FENCE.sub("", text).strip()]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start: end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise HTTPException(
        status_code=502,
        detail="Refinement model returned a response that is not valid JSON",
    )


def _normalize_span(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _parse_flags(payload: Any, content: str) -> list[dict]:
    """Validate flags. Structural garbage → 502; recoverable values → degraded."""
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise HTTPException(
            status_code=502,
            detail="Refinement model returned a malformed flag list",
        )

    flags: list[dict] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=502,
                detail="Refinement model returned a malformed flag entry",
            )
        explanation = entry.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise HTTPException(
                status_code=502,
                detail="Refinement model returned a flag with no explanation",
            )

        category = entry.get("category")
        if not isinstance(category, str) or category not in FLAG_CATEGORIES:
            # Unrecognised bucket, real note: keep the note, park the category.
            category = "other"

        span_start = _normalize_span(entry.get("span_start"))
        span_end = _normalize_span(entry.get("span_end"))
        localizable = (
            span_start is not None
            and span_end is not None
            and 0 <= span_start <= span_end <= len(content)
        )
        if not localizable:
            span_start = span_end = None

        suggested = entry.get("suggested_change")
        if not isinstance(suggested, str) or span_start is None:
            # A replacement with nowhere to apply is not actionable.
            suggested = None

        flags.append(
            {
                "category": category,
                "span_start": span_start,
                "span_end": span_end,
                "explanation": explanation.strip(),
                "suggested_change": suggested,
            }
        )
    return flags


def parse_refinement(raw: str) -> dict:
    """Model reply → {"content", "flags"}. Raises HTTPException(502) on garbage."""
    parsed = _load_json_object(raw)
    content = parsed.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(
            status_code=502,
            detail="Refinement model returned no refined content",
        )
    return {"content": content, "flags": _parse_flags(parsed.get("flags"), content)}


async def refine_captures(captures: list[dict], refinement_level: str) -> dict:
    """Refine a capture range into {"content", "flags", "model_identifier"}."""
    if refinement_level not in REFINEMENT_LEVELS:
        raise HTTPException(status_code=400, detail="Unsupported refinement_level")
    if not captures:
        raise HTTPException(status_code=400, detail="No captures to refine")

    system_prompt = build_system_prompt(refinement_level)
    user_prompt = build_user_prompt(captures)
    assert_within_prompt_budget(captures, user_prompt)
    raw = await asyncio.to_thread(call_model, system_prompt, user_prompt)
    result = parse_refinement(raw)
    result["model_identifier"] = MODEL
    return result
