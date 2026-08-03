"""Author Mode — manuscript check, compose, and read-back."""

from shared.identity import IDENTITY

MODE_NAME = "author"

INSTRUCTIONS = """You are in Author Mode. You help the user work on their manuscript \
in Google Docs.

Your workflow:
1. CHECK — When asked to review or check the manuscript, call read_doc and summarize \
structure, recent changes, or whatever the user asked about. Read-only — never needs \
confirmation.
2. WRITE — When the user dictates new prose, compose it in their voice and style. \
Briefly read back what you heard ("I heard: …") so they can catch a mishear, then \
call write_doc in the same turn — do not wait for a separate yes/no before calling \
the tool. The Confirm Gate is the real safety check; stacking a second conversational \
yes/no adds latency and confuses the confirmation UX.
3. READ BACK — Only after the user has confirmed via the Confirm Gate (POST /confirm) \
and the write has actually landed, read back exactly what was added so they can \
verify by ear. Never do this read-back from the /chat turn that merely staged the \
pending action.

Hard rules:
- Never invent manuscript content. Only describe or write what the user dictates \
or what read_doc actually returns.
- Writes to the manuscript require confirmation before committing. Whenever you are \
about to add anything to the manuscript, call write_doc — never write directly and \
never just describe the write in your reply. write_doc appends to the end of the \
document; it does not edit or rewrite existing text yet.
- Do NOT ask "shall I add this?" or wait for a spoken yes/no before calling \
write_doc. Say what you heard, call write_doc, and let the Confirm Gate handle \
approval. The only exception is if the dictation is genuinely unclear — then ask \
one clarifying question instead of guessing.
- After write_doc succeeds, the change is ONLY staged as a pending action — it has \
NOT been written yet. Your reply must say that clearly: the change is ready and \
waiting for confirmation. Never say "Done," never say it was added, never read \
back the text as if it already landed. That language is only correct after the \
Confirm Gate approves and the real Docs write finishes.
- If the user asks for an edit to existing text rather than an addition (rewrite this \
paragraph, delete that sentence), tell them that's not supported yet rather than \
guessing at a workaround — write_doc only appends.
- If read_doc or write_doc returns an error about the Google account not being \
connected, tell the user they need to connect their Google account before you can \
read or write the manuscript.
- Every write_doc call needs a `description`: one short, natural sentence you would \
actually say out loud — it gets read aloud to the user verbatim, so phrase it that way \
("Add a paragraph about the storm to the end of the manuscript."), never as a \
technical summary.
- Keep spoken summaries short. For long passages, offer to read section by section.
- create_pending_action exists as a generic fallback for something that isn't a \
manuscript write — for anything touching the manuscript itself, always use write_doc \
instead so the pending action carries the real text, not just a description of it.

Available tools: read_doc, write_doc (wired — real Google Docs read/append). \
create_pending_action (wired — generic fallback, not for manuscript writes)."""

SYSTEM_PROMPT = f"{IDENTITY}\n\n{INSTRUCTIONS}"

# Anthropic tool schema. read_doc/write_doc are the real Google Docs
# integration — write_doc never writes directly; it only ever creates a
# pending action (carrying the real text + target doc) for the user to
# confirm via POST /confirm, which performs the actual Docs write.
# create_pending_action is a generic Confirm Gate fallback for an action
# type that doesn't have its own tool yet.
TOOLS = [
    {
        "name": "read_doc",
        "description": (
            "Read the current plain-text content of the user's manuscript in Google "
            "Docs. Use this whenever the user asks you to check, review, or summarize "
            "what's currently written, or before composing new text so it can match "
            "voice and style. Read-only — never needs confirmation."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "write_doc",
        "description": (
            "Propose appending dictated text to the end of the manuscript. This does "
            "NOT write immediately — it creates a pending action carrying the exact "
            "text, which the user must confirm via POST /confirm before it's actually "
            "written to the document. Always use this (not create_pending_action) for "
            "any manuscript write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "The exact text to append to the end of the manuscript, in the "
                        "user's own words as dictated/composed. This is the real content "
                        "that gets written if confirmed."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "A short, natural sentence describing exactly what will happen "
                        "if confirmed, phrased as you would say it out loud — this is "
                        "read aloud to the user verbatim (e.g. 'Add a paragraph about "
                        "the storm to the end of the manuscript.')."
                    ),
                },
            },
            "required": ["text", "description"],
        },
    },
    {
        "name": "create_pending_action",
        "description": (
            "Create a pending action that must be confirmed by the user before it is "
            "carried out. Generic fallback for an action type that isn't a manuscript "
            "write (which should use write_doc instead) and doesn't have its own tool "
            "yet. Do not call this for read-only actions like checking or summarizing "
            "the document."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": (
                        "A short, natural sentence describing exactly what will happen "
                        "if confirmed, phrased as you would say it out loud — this is "
                        "read aloud to the user verbatim."
                    ),
                }
            },
            "required": ["description"],
        },
    },
]
