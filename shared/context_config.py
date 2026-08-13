"""Env-backed context budget / summary settings (no scattered magic numbers)."""

from __future__ import annotations

import os

# Defaults (overridable via env).
_DEFAULT_INPUT_TOKEN_BUDGET = 24_000
_DEFAULT_RECENT_MESSAGE_CAP = 20
_DEFAULT_SUMMARY_THRESHOLD = 30
_DEFAULT_CHARS_PER_TOKEN = 4.0

CONVERSATION_TITLE_MAX_CHARS = 60


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def context_input_token_budget() -> int:
    return _int_env("CONTEXT_INPUT_TOKEN_BUDGET", _DEFAULT_INPUT_TOKEN_BUDGET)


def context_recent_message_cap() -> int:
    return _int_env("CONTEXT_RECENT_MESSAGE_CAP", _DEFAULT_RECENT_MESSAGE_CAP)


def context_summary_threshold() -> int:
    return _int_env("CONTEXT_SUMMARY_THRESHOLD", _DEFAULT_SUMMARY_THRESHOLD)


def context_chars_per_token() -> float:
    return _float_env("CONTEXT_CHARS_PER_TOKEN", _DEFAULT_CHARS_PER_TOKEN)
