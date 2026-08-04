"""Plain-text sanitization for Mail & Calendar public DTOs."""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)
_REMOTE_IMG_RE = re.compile(r"(?i)https?://\S+\.(?:png|jpe?g|gif|webp|svg)(?:\?\S*)?")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def plain_text(value: str | None, *, max_len: int = 20_000) -> str | None:
    """Strip HTML/tags and control chars; do not preserve remote image markup."""
    if value is None:
        return None
    text = html.unescape(str(value))
    text = _TAG_RE.sub(" ", text)
    text = _REMOTE_IMG_RE.sub(" ", text)
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if not text:
        return None
    if len(text) > max_len:
        return text[:max_len].rstrip() + "…"
    return text
