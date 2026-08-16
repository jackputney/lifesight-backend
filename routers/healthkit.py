"""HealthKit sync + status. Ingest is idempotent; status exposes counts only.

Ownership is the JWT user via Depends(get_current_user_id) — the body never
carries a user_id, and no endpoint here can return another user's samples.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shared.auth import get_current_user_id
from shared.health.service import (
    MAX_SYNC_BATCH,
    BatchTooLargeError,
    build_status,
    ingest_healthkit_samples,
)

router = APIRouter(prefix="/healthkit", tags=["healthkit"])


class HealthKitSampleIn(BaseModel):
    """One HealthKit sample as the device reports it.

    Timestamps and units stay strings so a single malformed row is ignored and
    counted rather than rejecting the whole device batch with a 422.
    """

    sample_id: str = Field(..., min_length=1, max_length=200)
    type: str = Field(..., min_length=1, max_length=64)
    start_at: str = Field(..., min_length=1, max_length=64)
    end_at: str = Field(..., min_length=1, max_length=64)
    value: Optional[float] = None
    unit: Optional[str] = Field(default=None, max_length=32)
    value_text: Optional[str] = Field(default=None, max_length=120)
    source_bundle: Optional[str] = Field(default=None, max_length=200)
    source_name: Optional[str] = Field(default=None, max_length=200)


class HealthKitSyncRequest(BaseModel):
    samples: list[HealthKitSampleIn]


class HealthKitSyncResponse(BaseModel):
    accepted: int
    updated: int
    ignored: int
    server_time: str


class HealthKitCategoryStatus(BaseModel):
    latest_sample_at: Optional[str]
    count_last_30d: int


class HealthKitStatusResponse(BaseModel):
    last_synced_at: Optional[str]
    categories: dict[str, HealthKitCategoryStatus]


@router.post("/sync", response_model=HealthKitSyncResponse)
async def sync_healthkit_samples(
    body: HealthKitSyncRequest,
    user_id: str = Depends(get_current_user_id),
):
    """Upsert a batch on (user_id, 'healthkit', sample_id).

    accepted = newly inserted, updated = existing row changed, ignored =
    rejected samples plus replays that changed nothing.
    """
    try:
        result = await ingest_healthkit_samples(
            user_id, [sample.model_dump() for sample in body.samples]
        )
    except BatchTooLargeError:
        raise HTTPException(
            status_code=400,
            detail=f"Too many samples in one request (max {MAX_SYNC_BATCH}).",
        )
    return HealthKitSyncResponse(
        accepted=result["accepted"],
        updated=result["updated"],
        ignored=result["ignored"],
        server_time=result["server_time"],
    )


@router.get("/status", response_model=HealthKitStatusResponse)
async def read_healthkit_status(user_id: str = Depends(get_current_user_id)):
    """Per-category freshness and 30-day counts. Never returns raw samples."""
    status = await build_status(user_id)
    return HealthKitStatusResponse(
        last_synced_at=status["last_synced_at"],
        categories={
            name: HealthKitCategoryStatus(**data)
            for name, data in status["categories"].items()
        },
    )
