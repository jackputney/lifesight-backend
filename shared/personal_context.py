"""Personal-context profile enrichment via a restricted Claude tool."""

from __future__ import annotations

from typing import Any

from shared.profile_schema import ProfilePatch
from shared.profile_service import get_profile, patch_profile

PERSONAL_CONTEXT_FIELDS = frozenset(
    {
        "occupation",
        "industry",
        "education_context",
        "interests",
        "typical_schedule",
    }
)

UPDATE_PERSONAL_CONTEXT_TOOL: dict = {
    "name": "update_personal_context",
    "description": (
        "Save explicit user-provided personal-context fields to their durable "
        "LifeSight profile (occupation, industry, education_context, interests, "
        "typical_schedule). Use ONLY when the user explicitly asked to remember/"
        "update this information, OR when you asked a clear profile-enrichment "
        "question and they answered it. Never infer from weak hints. Never "
        "overwrite a non-empty existing value without replace_existing=true after "
        "the user confirmed the change. Not Confirm Gate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "occupation": {"type": ["string", "null"], "maxLength": 120},
            "industry": {"type": ["string", "null"], "maxLength": 120},
            "education_context": {"type": ["string", "null"], "maxLength": 240},
            "interests": {
                "type": ["array", "null"],
                "items": {"type": "string", "maxLength": 120},
                "maxItems": 20,
            },
            "typical_schedule": {"type": ["string", "null"], "maxLength": 500},
            "replace_existing": {
                "type": "boolean",
                "description": (
                    "Must be true to overwrite a non-empty existing field. "
                    "Default false — conflicting values are rejected."
                ),
            },
            "explicit_consent": {
                "type": "boolean",
                "description": (
                    "True only when the user explicitly asked to remember/update "
                    "OR answered your explicit enrichment question."
                ),
            },
        },
        "required": ["explicit_consent"],
    },
}

PERSONAL_CONTEXT_ENRICHMENT_POLICY = """\
Personal-context enrichment (optional):
- You may ask at most ONE unsolicited missing-profile question per conversation \
when it is clearly relevant to the user's current task.
- Never interrupt an urgent or direct task to gather profile information.
- Never silently invent occupation, industry, education, interests, or schedule.
- Never derive sensitive attributes (relationship status, politics, religion, \
finances, health diagnoses) into the profile.
- Casual mentions are not enough to save. Use update_personal_context only when \
the user says to remember/update, or when you asked an explicit enrichment \
question and made clear the answer can be saved.
- If a field already has a value and the user provides a different one, ask \
before replacing and call the tool with replace_existing=true only after they \
confirm.
- Skipped or unknown fields must not be repeatedly re-asked.
"""


def _normalize_incoming(tool_input: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in PERSONAL_CONTEXT_FIELDS:
        if key not in tool_input:
            continue
        value = tool_input.get(key)
        if value is None:
            continue
        if key == "interests":
            if isinstance(value, list):
                out[key] = [str(v).strip() for v in value if str(v).strip()]
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


async def apply_personal_context_update(
    user_id: str,
    tool_input: dict,
) -> tuple[str, bool]:
    """Validate and apply a restricted personal-context patch.

    Returns (tool_result text for Claude, profile_changed).
    When profile_changed is True, the chat layer should emit refresh_profile.
    """
    if not isinstance(tool_input, dict):
        return "Error: invalid tool input.", False
    if tool_input.get("explicit_consent") is not True:
        return (
            "Error: explicit_consent must be true. Do not save personal context "
            "without an explicit remember/update request or an answered enrichment question.",
            False,
        )

    incoming = _normalize_incoming(tool_input)
    if not incoming:
        return "Error: no personal-context fields provided.", False

    replace_existing = tool_input.get("replace_existing") is True
    current = await get_profile(user_id)
    conflicts: list[str] = []
    patch_data: dict[str, Any] = {}

    for key, new_value in incoming.items():
        existing = getattr(current, key)
        if key == "interests":
            existing_empty = not existing
        else:
            existing_empty = existing is None or existing == ""
        if not existing_empty and not replace_existing:
            if existing != new_value:
                conflicts.append(key)
            continue
        patch_data[key] = new_value

    if conflicts:
        return (
            "Error: existing non-empty values conflict for: "
            + ", ".join(conflicts)
            + ". Ask the user to confirm the change, then call again with "
            "replace_existing=true.",
            False,
        )
    if not patch_data:
        return "No profile changes needed (values already match).", False

    try:
        patch = ProfilePatch.model_validate(patch_data)
    except Exception as exc:
        return f"Error: invalid personal-context values ({exc}).", False

    await patch_profile(user_id, patch)
    saved = ", ".join(sorted(patch_data.keys()))
    return (
        f"Personal context saved ({saved}). Acknowledge briefly; do not claim "
        "unrelated profile fields changed.",
        True,
    )
