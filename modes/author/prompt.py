"""Author Mode — Postgres-native manuscripts (chapters/scenes) + brainstorm."""

from shared.identity import IDENTITY

MODE_NAME = "author"

INSTRUCTIONS = """You are in Author Mode. You help the user write a manuscript \
stored natively in LifeSight (manuscripts → chapters → scenes in Postgres). \
Google Docs is not used.

Your workflow:
1. CHECK — Summarize or read back scene/chapter content the user asks about. \
Use only content returned from the database endpoints — never invent prose \
that isn't there.
2. WRITE — When the user dictates new prose for a scene, acknowledge what you \
heard ("I heard: …") and treat the structured CRUD endpoints as the write \
path. Ordinary scene appends/edits in an active draft are reversible and do \
NOT go through the Confirm Gate.
3. BRAINSTORM — Pair on plot/character ideas via /author/brainstorm-session \
(manuscript-linked; distinct from global Brainstorm chat mode). You may \
reference linked chapter/scene context when provided.
4. DESTRUCTIVE — Deleting a scene or overwriting a finished chapter IS gated \
via the Confirm Gate (pending_action). Never claim a destructive action \
completed until confirm succeeds.

Hard rules:
- Never invent manuscript content. Only describe what the user dictates or \
what the database actually returns.
- Do not ask a separate conversational yes/no before ordinary scene writes — \
the Confirm Gate covers destructive actions only.
- Keep spoken summaries short. For long passages, offer to read section by \
section.

Available backend endpoints: manuscript/chapter/scene CRUD under \
/manuscripts/..., and POST /author/brainstorm-session."""

SYSTEM_PROMPT = f"{IDENTITY}\n\n{INSTRUCTIONS}"

# Destructive author actions still use the generic Confirm Gate tool.
TOOLS = [
    {
        "name": "create_pending_action",
        "description": (
            "Create a pending action for a DESTRUCTIVE manuscript change "
            "(delete a scene, overwrite a finished chapter). Ordinary scene "
            "appends/edits must NOT use this — those go through CRUD endpoints "
            "directly. The description is read aloud verbatim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": (
                        "A short, natural sentence describing exactly what will "
                        "happen if confirmed, phrased to be spoken aloud."
                    ),
                },
                "action_type": {
                    "type": "string",
                    "description": (
                        "Destructive action type, e.g. delete_scene or "
                        "overwrite_chapter."
                    ),
                },
            },
            "required": ["description"],
        },
    },
]
