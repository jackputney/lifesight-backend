# v2 Brainstorm + Mail & Calendar — approved contract

**Status:** contract approved; **runtime not yet updated** (still three-mode
`PUBLIC_MODE_IDS` at `d5d150e`). Do not implement Anthropic web search or
Mail & Calendar OAuth until this document’s review commit is accepted.

**Related:** `.cursor/rules/10-api-contract.mdc`, `AGENTS.md`,
`docs/V2_IOS_CONTRACT_NOTE.md`.

---

## 1. Visible modes (authoritative order)

`GET /modes` must return this **exact order** (no alphabetical sorting):

```json
{"modes":["fitness","diet","author","brainstorm","mail_calendar"]}
```

| # | Key | Display (iOS) |
|---|-----|-----------------|
| 1 | `fitness` | Fitness |
| 2 | `diet` | Diet |
| 3 | `author` | Author |
| 4 | `brainstorm` | Brainstorm |
| 5 | `mail_calendar` | Mail & Calendar |

- `health` — retired (not in registry or `/modes`).
- `jarvis` — **legacy isolation**: keep `modes/jarvis/` and optional
  `MODE_REGISTRY["jarvis"]` for smoke/debug; **never** list in `/modes`;
  **never** silently route `mail_calendar` through Jarvis modules.
- A plain `string[]` is the v1 `/modes` shape. A structured mode-config
  response may be proposed later; do not block on it.

iOS: use `/modes` for **enabled modes and ordered availability**. Native
icons, empty-state copy, accessibility labels, and voice aliases stay on
device.

---

## 2. Additive `/chat` field: `research`

Parallel to `visual_panel` — optional / nullable. Older clients ignore unknown
fields. **Keep `research` separate from `visual_panel`.**

### Wire shape

```json
{
  "reply": "…",
  "mode": "brainstorm",
  "conversation_id": "…",
  "pending_action": null,
  "visual_panel": null,
  "research": {
    "status": "completed",
    "query": "when was the FDA founded?",
    "summary": "…",
    "uncertainty": "…",
    "sources": [
      {
        "title": "…",
        "url": "https://…",
        "publisher": "FDA",
        "retrieved_at": "2026-08-04T20:00:00Z"
      }
    ],
    "fact_check": {
      "claim": "The FDA was founded in 1906.",
      "verdict": "supported",
      "confidence": 0.72
    }
  }
}
```

### `research.status`

| Value | When |
|-------|------|
| `not_requested` | Brainstorm discussion / hypothesis; **no** web-search op ran |
| `completed` | Real web-search operation finished successfully |
| `failed` | Search was attempted and failed |
| `unavailable` | Provider missing/misconfigured (no attempt or hard block) |

Do **not** add `running` until streaming research progress is implemented.

Field absent or `null` on non-Brainstorm modes (and on Brainstorm turns with
nothing to attach) is valid. Prefer `null` outside Brainstorm.

### Sources (public iOS contract)

Each source object:

| Field | Type | Notes |
|-------|------|--------|
| `title` | string | Required |
| `url` | string | Required; **do not speak raw URLs** via VoiceOver |
| `publisher` | string \| null | Preferred display label |
| `retrieved_at` | string (ISO-8601) | When the backend retrieved the result |

**Omit `snippet`** from the initial public iOS contract (may exist server-side
later; do not document as client-required).

### `fact_check`

May be non-null **only** when:

1. `status == "completed"`, **and**
2. a real web-search operation occurred on that turn.

| Field | Type | Values / notes |
|-------|------|----------------|
| `claim` | string | Claim under verification |
| `verdict` | string | `supported` \| `partially_supported` \| `not_supported` \| `inconclusive` |
| `confidence` | number | 0.0–1.0 |

iOS: never show a “Fact-checked” treatment unless `status == "completed"`
**and** `sources` is non-empty. Discussion turns (`not_requested`) must not
claim verification.

### Research provider abstraction

```text
ResearchProvider  (interface)
  └─ AnthropicWebSearchProvider   ← first implementation (Claude native web search)
  └─ (later) TavilyProvider / BraveProvider
```

Brainstorm mode uses `ResearchProvider`; the concrete provider is selected by
config/env. First ship: Anthropic native web search only.

### Confirm Gate

Brainstorm research is read-only — **no** `pending_action` for search.

---

## 3. Author endpoint rename (before global Brainstorm ships)

| Current (runtime today) | Target |
|-------------------------|--------|
| `POST /author/brainstorm` | `POST /author/brainstorm-session` |

Meaning unchanged: manuscript-linked plot/character pairing session
(`brainstorm_sessions` row). Distinct from chat mode `brainstorm`.

During rename: keep a temporary `410` or redirect note on the old path if any
client still calls it (iOS currently should not depend on it for Home modes).
Update `modes/author/prompt.py` references in the same slice as the rename.

---

## 4. Mail & Calendar (`mail_calendar`)

### Product scope (eventual)

Read / summarize email and calendar; draft messages and events; schedule;
act on mail/events. Google Gmail + Google Calendar **first**.

### Provider interfaces (new code only)

```text
modes/mail_calendar/          ← prompts + mode wiring
shared/mail_calendar/         ← provider interfaces + Google impl
  MailProvider
  CalendarProvider
    └─ GoogleMailProvider / GoogleCalendarProvider
    └─ (later) Outlook…
```

**Do not** import or route through `modes/jarvis/` or any legacy Jarvis tool
modules. Jarvis stays isolated until an explicit migration plan.

### Progressive OAuth

1. **Read scopes first** — enable list/read/search/summarize/free-busy.
2. **Write scopes later** — request only when send/mutate features are enabled
   in product.

Connection status should surface via a small domain endpoint (proposed in
implementation slices), not only via chat prose.

### Tools — Confirm Gate scope

| Action | Confirm Gate? |
|--------|---------------|
| Read / search mail | No |
| Summarize | No |
| List / read events, free/busy | No |
| Draft email / draft event | No |
| Send email | **Yes** |
| Delete / archive message | **Yes** |
| Create / update / delete event | **Yes** |
| Invite attendees | **Yes** |
| Respond to invitation | **Yes** |

`pending_action.description` remains a speakable sentence.

---

## 5. iOS contract notes (no `Mode.swift` change until slice approval)

### Voice aliases (native)

`mail_calendar` must match at least:

- mail  
- calendar  
- email  
- schedule  
- mail and calendar  
- mail & calendar (normalization)

Do **not** rely on first-word-of-display-name matching alone (“Mail” ≠ first
word of “Mail & Calendar” under naive splits).

Other modes: keep sensible aliases (`fitness`/`workout`, `diet`/`food`/`nutrition`,
`author`/`writing`, `brainstorm`/`research` as product decides) on device.

### Brainstorm citations

- Decode optional `research`.
- Render citations from typed `research.sources` (title + publisher;
  URL available to accessibility as a link trait / rotor target, **not**
  spoken as a raw string).
- “Fact-checked” UI only when `status == completed` and sources present.

### Mail & Calendar UI states (shell)

Permission needed → Disconnected → Connected/read-only → Draft → Pending
action (shared Confirm Gate pin). Full inbox UI is out of scope for early slices.

### Shared chat shell

Reuse `ModeChatView` for all five modes; mode-specific empty states only.

---

## 6. Implementation slices (ordered commits)

Do not merge later slices until earlier ones are reviewed.

| Slice | Repo focus | Deliverable | Runtime? |
|------:|------------|-------------|----------|
| **0** | Backend docs | This contract + AGENTS / api-contract / iOS note sync | Docs only |
| **1** | Backend | Ordered `PUBLIC_MODE_IDS` (5 keys); empty `modes/brainstorm` + `modes/mail_calendar` prompts; `/modes` order; `/chat` accepts new modes (no tools); rename `POST /author/brainstorm` → `/author/brainstorm-session`; `research: null` on `ChatResponse` model | Yes — registration only |
| **2** | iOS | `Mode.swift` + Home/Sidebar for five modes; voice aliases; `/modes`-driven order; empty states; decode `research` (ignore until present) | Yes — UI modes only |
| **3** | Backend | `ResearchProvider` + Anthropic web search; Brainstorm tools; populate `research` per schema; focused tests (no false fact_check) | Yes — research |
| **4** | iOS | Citation rendering + fact-check rules; VoiceOver (no raw URLs) | Yes — Brainstorm UI |
| **5** | Backend | `mail_calendar` Google OAuth (read scopes) + read/search/draft tools; status endpoint | Yes — OAuth/read |
| **6** | Backend | Write scopes + Confirm Gate for send/delete/archive/event mutate/invite/RSVP | Yes — gated writes |
| **7** | iOS | Mail & Calendar connect / disconnected / draft / pending-action shell states | Yes — MC UI |

**Slice 0 = this change.** Slices 3 and 5+ must not start until slice 0 is
reviewed. Slice 1 may proceed after slice 0 approval (empty registration only —
still no search/OAuth).

---

## 7. Out of scope for early slices

- Streaming `research.status = running`
- Source `snippet` on the public wire
- Outlook / non-Google mail providers
- Structured `/modes` config objects
- Reusing or deleting Jarvis source
- Full Fitness/Diet feature UI or `visual_panel` work unrelated to this track
