"""Machine-readable Google integration failure states for chat/tools."""

from __future__ import annotations

from enum import Enum


class GoogleFailureState(str, Enum):
    not_connected = "not_connected"
    insufficient_scope = "insufficient_scope"
    authorization_expired = "authorization_expired"
    authorization_revoked = "authorization_revoked"
    provider_unavailable = "provider_unavailable"


class GoogleIntegrationError(Exception):
    """Non-fatal provider/connection failure — chat must degrade cleanly."""

    def __init__(self, state: GoogleFailureState, detail: str = "") -> None:
        self.state = state
        self.detail = detail or state.value
        super().__init__(self.detail)

    def spoken(self) -> str:
        messages = {
            GoogleFailureState.not_connected: (
                "Google is not connected. Connect your Google account in Settings "
                "to use Mail & Calendar."
            ),
            GoogleFailureState.insufficient_scope: (
                "This Google connection is missing the permission needed for that "
                "action. Reconnect Google and grant the requested access."
            ),
            GoogleFailureState.authorization_expired: (
                "Your Google authorization expired. Reconnect Google to continue."
            ),
            GoogleFailureState.authorization_revoked: (
                "Your Google authorization was revoked. Reconnect Google to continue."
            ),
            GoogleFailureState.provider_unavailable: (
                "Google is temporarily unavailable. Try again in a moment."
            ),
        }
        return messages[self.state]
