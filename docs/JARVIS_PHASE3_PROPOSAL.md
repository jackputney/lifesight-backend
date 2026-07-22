# Jarvis Phase 3 proposal — wire `/chat` + `/confirm` (Jack's lane)

Oliver → Jack. Prepared after Phase 5 tools landed on `jarvis-oauth`.
**Do not implement from this doc without Jack's explicit go-ahead.** Files
touched are Jack-owned (`main.py`, possibly `shared/db.py`,
`shared/agent_loop.py` only if a glue tweak is needed).

## Goal
Connect the existing agent loop and Jarvis tool set so `/chat` can propose
Confirm Gate actions and `/confirm` can execute them.

## Current state (Oliver side, ready)
- `shared/agent_loop.py` — `run_agent(messages, system_prompt=, tool_schemas=, dispatch=, client=, model=)` → `(reply_text, pending_actions_list)`.
- `modes/jarvis/tools.py` — 14 schemas in `TOOL_SCHEMAS`; `dispatch_tool(user_id, name, args, *, conversation_id=None)` → `(result_text, pending_or_None)`.
- Irreversible tools return pending shaped as:
  `{ "action_id", "description", "action_type", "payload" }`
  (`description` is speakable via `shared/spoken_readback.build_readback`).
- `execute_confirmed_action(user_id, pending, via="click")` → spoken result string.
  Expects `pending["action_type"]` + `pending["payload"]` (dict or JSON string).

## Proposed `/chat` wiring (jarvis mode only at first)
When `mode == "jarvis"`:
1. Load history (already DB-backed).
2. Append user turn.
3. Bind dispatch:
   ```python
   dispatch = functools.partial(
       jarvis_tools.dispatch_tool,
       user_id,
       conversation_id=conversation_id,
   )
   ```
4. `reply, pendings = await run_agent(..., system_prompt=..., tool_schemas=jarvis_tools.TOOL_SCHEMAS, dispatch=dispatch, ...)`.
5. Persist assistant/tool turns from the mutated `messages` list (agent_loop already appends JSON-serializable content — today `append_message` may need to accept structured content; if it only stores plain text, ESCALATE that detail before coding).
6. Response `pending_action`: first item in `pendings` mapped to API shape `{action_id, description}` (or `null`). Multiple pendings in one turn is unlikely; if it happens, return the first and leave the rest pending in DB (same as reference behavior risk — call out if you want stricter).

Author/health stay on the plain text path until their tool sets exist.

## Proposed `/confirm` wiring
After ownership / expiry / status checks (already in `main.py`):
1. If `approved` is false → resolve rejected; return cancel copy (already done). Do **not** call the executor.
2. If approved → load full pending row, call:
   ```python
   result = await jarvis_tools.execute_confirmed_action(user_id, action, via="click")
   ```
   then return `ConfirmResponse(result=result)`.
3. Resolve status **before** execute only if you accept "confirmed but Google failed" as the failure mode; prefer resolve-after-success and leave `pending` on executor failure so the user can retry — pick one and document it. Recommendation: resolve to `confirmed` only after successful execute; on executor error leave `pending` (or set a new status later) and return a clear spoken error that nothing was sent/created.

## Required `shared/db.py` change (Jack)
`get_pending_action` currently returns only `id, user_id, status, expires_at`.
Executor needs **`action_type` and `payload`** (and `description` is useful for logging). Expand the SELECT to those columns. No schema migration — columns already exist in `002_core_schema.sql`.

## Out of scope for this proposal
- Voice endpoints `/spoken-readback`, `/match-confirmation` (Phase 6).
- Reminder scheduler / APNs.
- Deep-link OAuth callback (option B).
- Wiring author/health tools.

## Docs lockstep (same PR as the code)
- No API shape change expected — `pending_action` already on `/chat`.
- Optional: note in `10-api-contract.mdc` that OAuth state carries nonce (already approved; `MOBILE_API_GUIDE.md` updated on the Jarvis branch).
- README "What's NOT here yet" — remove "tool-calling not wired" once done.

## Test plan (after Jack implements)
1. Dev auth, Google connected (TEST account).
2. `/chat` jarvis: "what's on my calendar today" → tool use, no pending.
3. `/chat`: propose a calendar invite → `pending_action` with speakable description; no event in Calendar yet.
4. `/confirm` approved false → cancel copy; still no event.
5. Propose again → `/confirm` approved true → event exists; result says it was created.
6. Same for draft + send_email and reschedule_event.
7. Expired pending → 410; nothing executes.
