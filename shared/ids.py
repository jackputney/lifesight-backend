"""Canonical UUID parsing for path and body identifiers.

Malformed values become None so callers can take the ordinary 404 path
instead of letting asyncpg raise DataError on a `$1::uuid` bind.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional


def normalized_uuid(value: Any) -> Optional[str]:
    """Canonical UUID string, or None when the value is not a UUID at all."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None
