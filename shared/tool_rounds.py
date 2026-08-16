"""Atomic persistence for one Claude tool round.

A tool round is two rows: the assistant `tool_use` blocks and the matching
`tool_result` blocks. Persisting only the first leaves a dangling `tool_use`,
and Anthropic then rejects every later call for that conversation — a
permanent break for a user who cannot read an error off a screen. Both the
REST turn (`main._run_model_turn`) and the streaming turn use this so the
guarantee holds on either path.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from shared import db

# How long a cancelled turn waits for an in-flight round write to land.
TOOL_PERSIST_GRACE_SECONDS = 2.0


async def persist_tool_round(
    conversation_id: str,
    *,
    assistant_blocks: list[Any],
    tool_results: list[Any],
) -> None:
    """Write one tool round's assistant blocks and results as a unit.

    Shielded because a hard task.cancel() landing between the two appends
    would persist a tool_use with no tool_result. On cancellation the write
    is given a short grace period to land before the turn unwinds.
    """

    async def _write() -> None:
        await db.append_message(conversation_id, "assistant", assistant_blocks)
        await db.append_message(conversation_id, "user", tool_results)

    write = asyncio.ensure_future(_write())
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        with suppress(BaseException):
            await asyncio.wait_for(
                asyncio.shield(write), timeout=TOOL_PERSIST_GRACE_SECONDS
            )
        raise
