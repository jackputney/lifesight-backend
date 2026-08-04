"""Supabase Auth HTTP helpers — email/password, magic link, and Apple id_token.

iOS talks only to this backend; these functions call Supabase Auth so the
client never needs a Supabase anon key on-device. Identity for every other
route still comes from the JWT via shared/auth.get_current_user_id.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import HTTPException


def _base_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise HTTPException(status_code=500, detail="SUPABASE_URL is not configured")
    return url


def _anon_key() -> str:
    key = os.environ.get("SUPABASE_ANON_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="SUPABASE_ANON_KEY is not configured")
    return key


def _headers() -> dict[str, str]:
    key = _anon_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


async def sign_up(email: str, password: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_base_url()}/auth/v1/signup",
            headers=_headers(),
            json={"email": email, "password": password},
        )
    return _parse_auth_response(resp)


async def sign_in_password(email: str, password: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_base_url()}/auth/v1/token?grant_type=password",
            headers=_headers(),
            json={"email": email, "password": password},
        )
    return _parse_auth_response(resp)


async def request_magic_link(email: str) -> dict[str, Any]:
    """Send a magic-link email. Returns Supabase's ack payload (no session yet)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_base_url()}/auth/v1/otp",
            headers=_headers(),
            json={"email": email, "create_user": True},
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail=_error_detail(resp))
    return resp.json() if resp.content else {"ok": True}


async def sign_in_apple(id_token: str, nonce: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": "apple",
        "id_token": id_token,
    }
    if nonce:
        body["nonce"] = nonce
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_base_url()}/auth/v1/token?grant_type=id_token",
            headers=_headers(),
            json=body,
        )
    return _parse_auth_response(resp)


def _parse_auth_response(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise HTTPException(status_code=401, detail=_error_detail(resp))
    data = resp.json()
    user = data.get("user") or {}
    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "expires_in": data.get("expires_in"),
        "token_type": data.get("token_type", "bearer"),
        "user_id": user.get("id"),
        "email": user.get("email"),
    }


def _error_detail(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
        return str(payload.get("error_description") or payload.get("msg") or payload.get("error") or resp.text)
    except Exception:
        return resp.text or "Supabase auth request failed"
