"""`get_recent_health_data` — bounded health aggregates for fitness/diet chat.

Pull-only: nothing here is injected into a system prompt. It runs solely when
Claude calls the tool, and the result is capped at MAX_HEALTH_CONTEXT_CHARS.
"""
from __future__ import annotations

from typing import Any

from shared.health.service import (
    DEFAULT_CONTEXT_DAYS,
    MAX_CONTEXT_DAYS,
    MIN_CONTEXT_DAYS,
    build_health_context,
    clamp_days,
    clean_types,
)
from shared.health.types import SAMPLE_TYPES

GET_RECENT_HEALTH_DATA_TOOL: dict = {
    "name": "get_recent_health_data",
    "description": (
        "Read a bounded summary of the user's recent health samples (from "
        "Apple Health or a connected wearable). Returns aggregates only — "
        "daily averages, ranges, counts and the latest reading — never raw "
        "samples. Call it when recent activity, sleep, heart rate or body "
        "weight actually changes your answer. Describe trends only; never "
        "diagnose a disease or medical condition."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "types": {
                "type": "array",
                "description": "Which health categories to summarize.",
                "items": {"type": "string", "enum": list(SAMPLE_TYPES)},
                "minItems": 1,
            },
            "days": {
                "type": "integer",
                "description": (
                    f"Lookback window in days "
                    f"({MIN_CONTEXT_DAYS}-{MAX_CONTEXT_DAYS}; "
                    f"default {DEFAULT_CONTEXT_DAYS})."
                ),
                "minimum": MIN_CONTEXT_DAYS,
                "maximum": MAX_CONTEXT_DAYS,
            },
        },
        "required": ["types", "days"],
    },
}

TOOLS: list[dict] = [GET_RECENT_HEALTH_DATA_TOOL]


async def run_get_recent_health_data(user_id: str, tool_input: dict[str, Any]) -> str:
    """Execute the tool for one user. Returns tool_result text, never raises."""
    sample_types = clean_types((tool_input or {}).get("types"))
    if not sample_types:
        return (
            "Error: types must include at least one of: "
            + ", ".join(SAMPLE_TYPES)
            + "."
        )
    days = clamp_days((tool_input or {}).get("days", DEFAULT_CONTEXT_DAYS))
    try:
        return await build_health_context(user_id, sample_types, days)
    except Exception:
        return (
            "Error [health_data_unavailable]: recent health data could not be "
            "read right now. Answer without it and say so plainly."
        )
