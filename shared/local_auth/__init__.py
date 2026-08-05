"""Self-hosted username/password auth (AUTH_MODE=self)."""

from shared.local_auth.service import AuthError, AuthService

__all__ = ["AuthError", "AuthService"]
