"""Mode-agnostic agent loop: call model -> run tools -> repeat until final text.

Adapted from the Oliver_Jarvis_V2 reference (app/agent.py), generalized so any
mode can reuse it: the caller supplies the system prompt (modes/<mode>/prompt.py
owns the text), the Anthropic tool schemas, and an async dispatch function —
nothing here knows about a specific mode, tool, or user. To bind per-request
context (like user_id) into dispatch, wrap the mode's dispatcher with
functools.partial before calling run_agent.

The Anthropic SDK call is blocking, so it runs via asyncio.to_thread (the same
pattern main.py uses for its plain chat call) to avoid stalling the event
loop. dispatch is awaited directly because tool implementations hit the async
storage layer (shared/db.py).
"""
import asyncio
from collections.abc import Awaitable, Callable

MAX_ITERATIONS = 10

# dispatch(name, args) -> (result_text_for_model, pending_action_or_None)
Dispatch = Callable[[str, dict], Awaitable[tuple[str, dict | None]]]


def _jsonable_content(content) -> list[dict]:
    """Anthropic SDK content blocks -> plain dicts safe for json.dumps.

    History loaded from shared/db.py is already dicts; fresh API responses are
    SDK objects. Normalizing here means everything appended to `messages` can
    be persisted as-is by db.append_message.
    """
    out: list[dict] = []
    for block in content:
        if isinstance(block, dict):
            out.append(block)
        elif block.type == "text":
            out.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                }
            )
    return out


async def run_agent(
    messages: list[dict],
    *,
    system_prompt: str,
    tool_schemas: list[dict],
    dispatch: Dispatch,
    client,
    model: str,
    max_tokens: int = 1024,
) -> tuple[str, list[dict]]:
    """Run the loop against the full conversation `messages` (Anthropic format).

    The list is mutated in place with JSON-serializable content: every
    assistant turn (including tool_use blocks), every tool_result turn, and
    the final assistant reply are appended, so the caller can persist the
    complete history for the next turn.

    Tool exceptions become is_error tool_results — they never crash the loop.

    Returns (final_text, pending_actions_created_this_turn). Pending actions
    are whatever dicts dispatch returned as its second element (the Confirm
    Gate proposals); none of the currently wired tools create them yet.
    """
    pending: list[dict] = []

    for _ in range(MAX_ITERATIONS):
        resp = await asyncio.to_thread(
            client.messages.create,
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=tool_schemas,
            messages=messages,
        )
        content = _jsonable_content(resp.content)
        messages.append({"role": "assistant", "content": content})

        if resp.stop_reason != "tool_use":
            text = "".join(
                b.get("text") or "" for b in content if b.get("type") == "text"
            )
            return text, pending

        tool_results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            try:
                result_text, pend = await dispatch(block["name"], block["input"])
                if pend:
                    pending.append(pend)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result_text,
                    }
                )
            except Exception as exc:  # tool errors must not crash the loop
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": f"Error running {block['name']}: {exc}",
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": tool_results})

    fallback = "I couldn't finish that within my step limit. Please try rephrasing."
    messages.append({"role": "assistant", "content": fallback})
    return fallback, pending
