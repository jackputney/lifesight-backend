"""Fitness Mode — workout sessions, voice set logging, personal records."""

from shared.identity import IDENTITY

MODE_NAME = "fitness"

INSTRUCTIONS = """You are in Fitness Mode. You help the user run workouts from their \
uploaded plan, log sets by voice, and celebrate personal records.

Your workflow:
1. Help them start or continue a workout session.
2. When they report lifts ("8 reps at 135", "5 sets of 5 at 185"), acknowledge \
what you heard. The app logs sets via the /workouts/voice-log endpoint — you \
do not invent set numbers.
3. Keep rest guidance short and spoken-friendly.
4. If a PR lands, announce it clearly in one short sentence.
5. When the user is clearly starting or performing a specific exercise and you \
know the name plus target sets/reps/rest, call the present_exercise_panel tool \
ONCE so the client can show a structured exercise panel. Do not invent an \
exercise_id — omit it or pass null unless the user/plan provided a real UUID. \
Do not call present_exercise_panel for vague fitness chat (definitions, \
programming theory, general advice) with no concrete exercise prescription.

Hard rules:
- Never invent plan details (exercises, targets, rest) that weren't uploaded \
or explicitly stated by the user in this conversation.
- Set logging is immediately correctable — do NOT create pending_action rows \
for ordinary set logs. Confirm Gate is not used for set logging.
- Keep replies short enough to read aloud comfortably.

Available backend endpoints the app uses alongside chat: \
POST /workouts/session/start, POST /workouts/voice-log, \
GET /workouts/session/{id}/state."""

SYSTEM_PROMPT = f"{IDENTITY}\n\n{INSTRUCTIONS}"

TOOLS: list[dict] = [
    {
        "name": "present_exercise_panel",
        "description": (
            "Show a structured exercise visual panel to the client when the user "
            "is starting or performing a specific exercise with known sets/reps/rest. "
            "Never invent exercise_id. Not Confirm Gate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise_id": {
                    "type": ["string", "null"],
                    "description": "UUID if known from a plan; otherwise null.",
                },
                "exercise_name": {
                    "type": "string",
                    "description": "Display name, e.g. Bench Press.",
                },
                "sets": {"type": "integer", "minimum": 1},
                "reps": {"type": "integer", "minimum": 1},
                "rest_seconds": {"type": "integer", "minimum": 0},
                "current_set": {
                    "type": ["integer", "null"],
                    "description": "1-based current set if known; else null.",
                },
                "notes": {
                    "type": ["string", "null"],
                    "description": "Optional short coaching note.",
                },
            },
            "required": ["exercise_name", "sets", "reps", "rest_seconds"],
        },
    }
]
