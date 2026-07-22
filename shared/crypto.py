"""Fernet token encryption + HMAC-signed OAuth state.

Two separate secrets on purpose:
- GOOGLE_TOKEN_ENCRYPTION_KEY — Fernet key for access/refresh tokens at rest
- OAUTH_STATE_SECRET — HMAC key for the authorize→callback state param

Never reuse one for the other: a leak of the state secret must not decrypt
stored Google tokens, and vice versa.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from cryptography.fernet import Fernet, InvalidToken

# OAuth state is valid for this many seconds (authorize → consent → callback).
_OAUTH_STATE_MAX_AGE_SECONDS = 600


def _fernet() -> Fernet:
    key = os.environ.get("GOOGLE_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "GOOGLE_TOKEN_ENCRYPTION_KEY is not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string; returns a url-safe Fernet ciphertext string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string back to the original token."""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt token — wrong key or corrupt ciphertext") from exc


def _state_secret() -> bytes:
    secret = os.environ.get("OAUTH_STATE_SECRET")
    if not secret:
        raise RuntimeError(
            "OAUTH_STATE_SECRET is not set. Use a long random string, distinct "
            "from GOOGLE_TOKEN_ENCRYPTION_KEY."
        )
    return secret.encode("utf-8")


def sign_oauth_state(user_id: str) -> str:
    """Build a signed, url-safe state embedding user_id + expiry."""
    expiry = int(time.time()) + _OAUTH_STATE_MAX_AGE_SECONDS
    payload = f"{user_id}.{expiry}"
    sig = hmac.new(_state_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}.{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def verify_oauth_state(state: str) -> str:
    """Validate state and return the embedded user_id. Raises ValueError on failure."""
    if not state:
        raise ValueError("Missing OAuth state")
    pad = "=" * (-len(state) % 4)
    try:
        raw = base64.urlsafe_b64decode(state + pad).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Malformed OAuth state") from exc

    parts = raw.rsplit(".", 2)
    if len(parts) != 3:
        raise ValueError("Malformed OAuth state")
    user_id, expiry_str, sig = parts

    payload = f"{user_id}.{expiry_str}"
    expected = hmac.new(
        _state_secret(), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise ValueError("Invalid OAuth state signature")

    try:
        expiry = int(expiry_str)
    except ValueError as exc:
        raise ValueError("Malformed OAuth state expiry") from exc
    if time.time() > expiry:
        raise ValueError("OAuth state expired — start Connect Google again")

    return user_id
