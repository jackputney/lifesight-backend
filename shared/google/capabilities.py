"""Capability names → fixed Google OAuth scope allowlist.

Clients pass capability names to POST /integrations/google/start — never
arbitrary scope URLs.
"""

from __future__ import annotations

from typing import Iterable

# Closed set of capability ids. Order is stable for status JSON.
CAPABILITY_IDS: tuple[str, ...] = (
    "google_identity",
    "calendar",
    "gmail_send",
    "gmail_read",
)

# TestFlight initial grant: identity + calendar (read/write events).
DEFAULT_START_CAPABILITIES: tuple[str, ...] = ("google_identity", "calendar")

CAPABILITY_SCOPES: dict[str, tuple[str, ...]] = {
    "google_identity": (
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ),
    # Events scope covers list + create + update + delete for primary calendar.
    "calendar": ("https://www.googleapis.com/auth/calendar.events",),
    "gmail_send": ("https://www.googleapis.com/auth/gmail.send",),
    "gmail_read": ("https://www.googleapis.com/auth/gmail.readonly",),
}

# Scopes that imply a capability is granted (any match → true).
_CAPABILITY_REQUIRED_SCOPES: dict[str, frozenset[str]] = {
    "google_identity": frozenset(
        {
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        }
    ),
    "calendar": frozenset(
        {
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar",
        }
    ),
    "gmail_send": frozenset({"https://www.googleapis.com/auth/gmail.send"}),
    "gmail_read": frozenset(
        {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        }
    ),
}


class UnknownCapabilityError(ValueError):
    pass


def normalize_capabilities(raw: Iterable[str] | None) -> list[str]:
    """Validate + dedupe capability names. Empty → default TestFlight set."""
    if raw is None:
        items = list(DEFAULT_START_CAPABILITIES)
    else:
        items = [str(x).strip() for x in raw if str(x).strip()]
        if not items:
            items = list(DEFAULT_START_CAPABILITIES)
    unknown = [c for c in items if c not in CAPABILITY_SCOPES]
    if unknown:
        raise UnknownCapabilityError(
            "Unknown capabilities: " + ", ".join(sorted(set(unknown)))
        )
    requested = set(items)
    requested.add("google_identity")  # always required for connection identity
    return [cap for cap in CAPABILITY_IDS if cap in requested]


def scopes_for_capabilities(capabilities: Iterable[str] | None) -> list[str]:
    caps = normalize_capabilities(capabilities)
    scopes: list[str] = []
    seen: set[str] = set()
    for cap in caps:
        for scope in CAPABILITY_SCOPES[cap]:
            if scope not in seen:
                seen.add(scope)
                scopes.append(scope)
    return scopes


def capabilities_from_scopes(granted_scopes: Iterable[str] | None) -> dict[str, bool]:
    granted = {s.strip() for s in (granted_scopes or []) if s and str(s).strip()}
    # Google sometimes returns scope without https scheme variants — compare exact.
    out: dict[str, bool] = {}
    for cap in CAPABILITY_IDS:
        required = _CAPABILITY_REQUIRED_SCOPES[cap]
        if cap == "google_identity":
            # Any identity-ish scope or openid is enough to show connected identity.
            out[cap] = bool(granted & required) or "email" in granted or "profile" in granted
        else:
            out[cap] = bool(granted & required)
    return out


def require_capability(granted_scopes: Iterable[str] | None, capability: str) -> None:
    from shared.google.errors import GoogleFailureState, GoogleIntegrationError

    caps = capabilities_from_scopes(granted_scopes)
    if capability not in CAPABILITY_SCOPES:
        raise UnknownCapabilityError(capability)
    if not caps.get(capability):
        raise GoogleIntegrationError(
            GoogleFailureState.insufficient_scope,
            f"Missing capability: {capability}",
        )
