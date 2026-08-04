"""App-level encryption for tokens at rest — Google OAuth access/refresh tokens.

Postgres storing a token in plaintext means anyone with read access to the
database (a leaked DATABASE_URL, a misconfigured RLS policy, a support person
poking around) can act as the user against Google. Encrypting at the app
layer means the ciphertext alone is useless without TOKEN_ENCRYPTION_KEY,
which lives only in this process's environment — never in the database.

Fernet (symmetric, from the `cryptography` package) rather than something
hand-rolled: it bundles AES-128-CBC + HMAC and refuses to decrypt anything
tampered with, so a corrupted or edited ciphertext raises loudly instead of
silently returning garbage.
"""
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet() -> Fernet:
    key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with "
            "Fernet.generate_key() and add it to .env. Losing this key makes "
            "every already-stored token unrecoverable — back it up somewhere "
            "safe, separate from the database."
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt a token for storage in an *_enc column. Returns a string safe
    to store directly in a TEXT column."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a token read back from an *_enc column.

    Raises ValueError (not the raw cryptography exception) on a bad/tampered
    ciphertext or a key mismatch, so callers get one exception type to
    handle regardless of the underlying crypto library.
    """
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Could not decrypt token — wrong key or corrupted ciphertext") from exc
