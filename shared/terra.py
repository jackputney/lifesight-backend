"""Terra API client — wearable connect widget + webhook payload helpers.

Default aggregator for v2 (broader device coverage than HealthKit-only).
Requires TERRA_API_KEY, TERRA_DEV_ID, and optionally TERRA_WEBHOOK_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

import httpx
from fastapi import HTTPException


def _api_key() -> str:
    key = os.environ.get("TERRA_API_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="TERRA_API_KEY is not configured")
    return key


def _dev_id() -> str:
    dev_id = os.environ.get("TERRA_DEV_ID", "")
    if not dev_id:
        raise HTTPException(status_code=500, detail="TERRA_DEV_ID is not configured")
    return dev_id


def _headers() -> dict[str, str]:
    return {
        "x-api-key": _api_key(),
        "dev-id": _dev_id(),
        "Content-Type": "application/json",
    }


async def create_widget_session(
    *,
    reference_id: str,
    providers: list[str] | None = None,
    auth_success_redirect_url: str | None = None,
    auth_failure_redirect_url: str | None = None,
) -> dict[str, Any]:
    """Return Terra widget/session URL for the user to connect a wearable."""
    body: dict[str, Any] = {
        "reference_id": reference_id,
        "language": "en",
    }
    if providers:
        body["providers"] = ",".join(providers)
    if auth_success_redirect_url:
        body["auth_success_redirect_url"] = auth_success_redirect_url
    if auth_failure_redirect_url:
        body["auth_failure_redirect_url"] = auth_failure_redirect_url

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.tryterra.co/v2/auth/generateWidgetSession",
            headers=_headers(),
            json=body,
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Terra widget session failed: {resp.text}")
    return resp.json()


def verify_webhook_signature(body: bytes, signature_header: str | None) -> bool:
    """HMAC-SHA256 check when TERRA_WEBHOOK_SECRET is set.

    Unsigned acceptance is allowed only for local AUTH_MODE=dev outside
    staging/production. Staging/production and AUTH_MODE=self reject missing
    secrets (and missing signatures) so traffic cannot silently skip HMAC.
    """
    secret = (os.environ.get("TERRA_WEBHOOK_SECRET") or "").strip()
    if not secret:
        from shared.auth import auth_mode, is_deploy_environment

        return auth_mode() == "dev" and not is_deploy_environment()
    if not signature_header:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature_header)


def extract_metrics(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a Terra webhook body into intermediate metric rows.

    Returns list of dicts: metric_type, value, value_json, source_device, recorded_at (iso str).
    metric_type stays free text here so a new Terra field needs no migration;
    the closed vocabulary is applied downstream by
    shared.health.service.ingest_terra_metrics, which is what writes
    health_samples (provider='terra') and drops anything it cannot map. The
    deprecated health_metrics table is no longer written (migration 016).
    """
    rows: list[dict[str, Any]] = []
    user = payload.get("user") or {}
    provider = user.get("provider") or payload.get("provider") or "terra"
    data = payload.get("data") or []
    if isinstance(data, dict):
        data = [data]

    for item in data:
        if not isinstance(item, dict):
            continue
        recorded_at = (
            item.get("metadata", {}).get("start_time")
            or item.get("metadata", {}).get("timestamp")
            or item.get("start_time")
            or item.get("timestamp")
        )
        # Common Terra shapes: heart_rate_data, distance_data, calories_data, sleep_durations_data…
        for key, val in item.items():
            if key in ("metadata", "device_data", "user"):
                continue
            if isinstance(val, (int, float)):
                rows.append(
                    {
                        "metric_type": key,
                        "value": float(val),
                        "value_json": None,
                        "source_device": provider,
                        "recorded_at": recorded_at,
                    }
                )
            elif isinstance(val, dict):
                summary = val.get("summary") if isinstance(val.get("summary"), dict) else None
                if summary:
                    for sk, sv in summary.items():
                        if isinstance(sv, (int, float)):
                            rows.append(
                                {
                                    "metric_type": f"{key}.{sk}",
                                    "value": float(sv),
                                    "value_json": None,
                                    "source_device": provider,
                                    "recorded_at": recorded_at,
                                }
                            )
                rows.append(
                    {
                        "metric_type": key,
                        "value": None,
                        "value_json": val,
                        "source_device": provider,
                        "recorded_at": recorded_at,
                    }
                )
    return rows
