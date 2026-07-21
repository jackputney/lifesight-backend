"""Fixed phrase matching for spoken confirmation — never delegate to the model."""
import re

_READ_FULL = re.compile(
    r"read (the )?(whole|full|entire) (email|thing|message|body)"
    r"|read (it|the email) (all|in full)"
    r"|read the whole (thing|one)",
    re.I,
)

# Trick / override attempts — always cancel (checked before affirmatives).
_TRICK_CANCEL = re.compile(
    r"skip the confirmation"
    r"|auto[- ]?confirm"
    r"|from now on"
    r"|next time"
    r"|don'?t ask"
    r"|without confirm",
    re.I,
)

_NEGATIVE = re.compile(
    r"\b(no|nope|cancel|stop|don'?t|dont|do not|wait|maybe|perhaps|not)\b",
    re.I,
)

# Pure-affirmative phrases; matching removes them longest-first.
_AFFIRM_PHRASES = (
    "go ahead and send it",
    "go ahead and send",
    "please send it",
    "please send",
    "send the email",
    "send the mail",
    "send this email",
    "send this",
    "send that",
    "send it",
    "go ahead",
    "do it",
    "go for it",
    "sounds good to me",
    "sounds good",
    "looks good to me",
    "looks good",
    "works for me",
    "that works",
    "that is fine",
    "thats fine",
    "that's fine",
    "fine with me",
)

_AFFIRM_WORDS = frozenset(
    {"yes", "yeah", "yep", "yup", "confirm", "confirmed", "sure", "ok", "okay", "alright"}
)

_FILLER = frozenset({"um", "uh", "well", "so", "please", "thanks", "now", "right"})

# Glue words allowed between affirmative phrases ("yes, go ahead and send it").
_CONNECTORS = frozenset({"and", "then"})

# Speech-to-text often hears "sent" for "send".
_STT_ALIASES = re.compile(r"\b(sent it|send it|send the email)\b", re.I)


def _normalize(text: str) -> str:
    t = re.sub(r"[^\w\s'.,!?-]", " ", str(text or "").lower())
    t = t.replace("'", "")
    return re.sub(r"\s+", " ", t).strip()


def _strip_leading_fillers(t: str) -> str:
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"^(um|uh|well|so|alright|okay|ok|yeah|yes)[,.\s]+", "", t).strip()
    return t


def _content_words(t: str) -> list[str]:
    return [w for w in re.split(r"[\s,.]+", t) if w and w not in _FILLER]


def match_confirmation(text: str) -> str:
    """Return 'confirm', 'cancel', or 'read_full' (send_email body request).

    Confirms ONLY when the whole utterance is affirmative material. Anything
    left over ("...to john instead", "...but make it 3pm") means the user is
    asking for a change, and executing the unchanged action would be wrong —
    so it cancels.
    """
    t = _normalize(text)
    if not t:
        return "cancel"
    if _READ_FULL.search(t):
        return "read_full"
    if _TRICK_CANCEL.search(t):
        return "cancel"
    if _NEGATIVE.search(t):
        return "cancel"

    # If leading-filler stripping consumed everything (e.g. "yes."), keep t.
    base = _strip_leading_fillers(t) or t

    residue = base
    for phrase in sorted(_AFFIRM_PHRASES, key=len, reverse=True):
        residue = re.sub(rf"\b{re.escape(phrase)}\b", " ", residue)
    residue = _STT_ALIASES.sub(" ", residue)

    base_words = _content_words(base)
    residue_words = _content_words(residue)
    leftover = [
        w for w in residue_words if w not in _AFFIRM_WORDS and w not in _CONNECTORS
    ]
    if leftover:
        return "cancel"

    matched_phrase = len(residue_words) < len(base_words)
    matched_word = any(w in _AFFIRM_WORDS for w in base_words)
    return "confirm" if (matched_phrase or matched_word) else "cancel"
