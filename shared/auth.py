# shared/auth.py
# Auth injection point. Endpoints depend on get_current_user_id and never
# decode tokens themselves — so the dev/real swap touches ONLY this file.
#
# Decision (frozen): Supabase Auth owns identity, login is Sign in with Apple
# (accessibility — no typed password). Supabase issues an HS256 JWT whose `sub`
# claim IS the auth.users(id) UUID we FK against. There is no password anywhere.
#
#   AUTH_MODE=dev  (default) -> every request resolves to the fixed dev UUID.
#   AUTH_MODE=real           -> verify the Supabase JWT from the Bearer header.

import os

from fastapi import Header, HTTPException

DEV_FAKE_USER_ID = "00000000-0000-4000-8000-000000000001"  # matches the migration dev seed


async def get_current_user_id(authorization: str = Header(None)) -> str:
    if os.getenv("AUTH_MODE", "dev") == "dev":
        # Dev mode: any Bearer token (or none) resolves to the fake user.
        return DEV_FAKE_USER_ID

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()

    # Imported lazily so dev mode never needs PyJWT installed.
    import jwt  # PyJWT

    secret = os.environ.get("SUPABASE_JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="SUPABASE_JWT_SECRET not configured")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="authenticated",           # Supabase sets aud=authenticated
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError:
        # Never leak the reason (expired vs. bad signature) to the client.
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return payload["sub"]
