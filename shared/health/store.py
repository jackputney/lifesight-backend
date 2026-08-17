"""health_samples storage — Postgres via shared.db.pool, optional memory for tests.

Ownership: every query scopes by the user_id resolved from the JWT at the
router. Reads return aggregates only; raw samples never leave this module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from shared import db
from shared.health.types import NormalizedSample

PROVIDER_HEALTHKIT = "healthkit"
PROVIDER_TERRA = "terra"

_UPSERT_SQL = """
INSERT INTO health_samples (
    user_id, provider, external_id, sample_type, start_at, end_at,
    value, unit, value_text, source_bundle, source_name, metadata
)
SELECT
    $1::uuid, $2, s.external_id, s.sample_type, s.start_at, s.end_at,
    s.value, s.unit, s.value_text, s.source_bundle, s.source_name, s.metadata::jsonb
FROM unnest(
    $3::text[], $4::text[], $5::timestamptz[], $6::timestamptz[],
    $7::double precision[], $8::text[], $9::text[], $10::text[], $11::text[], $12::text[]
) AS s(
    external_id, sample_type, start_at, end_at,
    value, unit, value_text, source_bundle, source_name, metadata
)
ON CONFLICT (user_id, provider, external_id) DO UPDATE
SET sample_type   = EXCLUDED.sample_type,
    start_at      = EXCLUDED.start_at,
    end_at        = EXCLUDED.end_at,
    value         = EXCLUDED.value,
    unit          = EXCLUDED.unit,
    value_text    = EXCLUDED.value_text,
    source_bundle = EXCLUDED.source_bundle,
    source_name   = EXCLUDED.source_name,
    metadata      = EXCLUDED.metadata,
    updated_at    = now()
WHERE (
    health_samples.sample_type, health_samples.start_at, health_samples.end_at,
    health_samples.value, health_samples.unit, health_samples.value_text,
    health_samples.source_bundle, health_samples.source_name, health_samples.metadata
) IS DISTINCT FROM (
    EXCLUDED.sample_type, EXCLUDED.start_at, EXCLUDED.end_at,
    EXCLUDED.value, EXCLUDED.unit, EXCLUDED.value_text,
    EXCLUDED.source_bundle, EXCLUDED.source_name, EXCLUDED.metadata
)
RETURNING (xmax = 0) AS inserted
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# In-memory store (tests)
# ---------------------------------------------------------------------------

@dataclass
class _MemoryStore:
    # (user_id, provider, external_id) → row
    samples: dict[tuple[str, str, str], dict] = field(default_factory=dict)
    sync_state: dict[str, datetime] = field(default_factory=dict)


_memory: Optional[_MemoryStore] = None


def use_memory_store(enabled: bool = True) -> _MemoryStore:
    global _memory
    if enabled:
        _memory = _MemoryStore()
        return _memory
    _memory = None
    return _MemoryStore()


def _mem() -> Optional[_MemoryStore]:
    return _memory


def _comparable(row: dict) -> tuple:
    return (
        row["sample_type"],
        row["start_at"],
        row["end_at"],
        row["value"],
        row["unit"],
        row["value_text"],
        row["source_bundle"],
        row["source_name"],
        json.dumps(row["metadata"], sort_keys=True),
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

async def upsert_samples(
    user_id: str,
    provider: str,
    samples: list[NormalizedSample],
) -> tuple[int, int, int]:
    """Idempotent bulk upsert. Returns (inserted, updated, unchanged).

    `samples` must already be deduplicated on external_id — Postgres refuses to
    touch the same conflict target twice in one statement.
    """
    if not samples:
        return 0, 0, 0

    if _mem() is not None:
        inserted = updated = unchanged = 0
        for sample in samples:
            key = (user_id, provider, sample.external_id)
            incoming = {
                "user_id": user_id,
                "provider": provider,
                "external_id": sample.external_id,
                "sample_type": sample.sample_type,
                "start_at": sample.start_at,
                "end_at": sample.end_at,
                "value": sample.value,
                "unit": sample.unit,
                "value_text": sample.value_text,
                "source_bundle": sample.source_bundle,
                "source_name": sample.source_name,
                "metadata": dict(sample.metadata),
            }
            existing = _mem().samples.get(key)
            if existing is None:
                incoming["created_at"] = _now()
                incoming["updated_at"] = _now()
                _mem().samples[key] = incoming
                inserted += 1
            elif _comparable(existing) != _comparable(incoming):
                incoming["created_at"] = existing["created_at"]
                incoming["updated_at"] = _now()
                _mem().samples[key] = incoming
                updated += 1
            else:
                unchanged += 1
        return inserted, updated, unchanged

    rows = await db.pool().fetch(
        _UPSERT_SQL,
        user_id,
        provider,
        [s.external_id for s in samples],
        [s.sample_type for s in samples],
        [s.start_at for s in samples],
        [s.end_at for s in samples],
        [s.value for s in samples],
        [s.unit for s in samples],
        [s.value_text for s in samples],
        [s.source_bundle for s in samples],
        [s.source_name for s in samples],
        [json.dumps(s.metadata) for s in samples],
    )
    inserted = sum(1 for r in rows if r["inserted"])
    updated = len(rows) - inserted
    return inserted, updated, len(samples) - len(rows)


async def touch_sync_state(user_id: str) -> datetime:
    """Stamp "a sync completed now" and return that timestamp."""
    if _mem() is not None:
        stamp = _now()
        _mem().sync_state[user_id] = stamp
        return stamp

    row = await db.pool().fetchrow(
        """
        INSERT INTO health_sync_state (user_id, last_synced_at, updated_at)
        VALUES ($1::uuid, now(), now())
        ON CONFLICT (user_id) DO UPDATE
        SET last_synced_at = now(), updated_at = now()
        RETURNING last_synced_at
        """,
        user_id,
    )
    return row["last_synced_at"]


async def get_last_synced_at(user_id: str) -> Optional[datetime]:
    if _mem() is not None:
        return _mem().sync_state.get(user_id)

    return await db.pool().fetchval(
        "SELECT last_synced_at FROM health_sync_state WHERE user_id = $1::uuid",
        user_id,
    )


# ---------------------------------------------------------------------------
# Reads (aggregates only)
# ---------------------------------------------------------------------------

async def category_status(user_id: str, *, since: datetime) -> dict[str, dict[str, Any]]:
    """Per sample_type: latest sample time and count on/after `since`."""
    if _mem() is not None:
        result: dict[str, dict[str, Any]] = {}
        for (owner, _provider, _ext), row in _mem().samples.items():
            if owner != user_id:
                continue
            bucket = result.setdefault(
                row["sample_type"], {"latest_sample_at": None, "count_recent": 0}
            )
            latest = bucket["latest_sample_at"]
            if latest is None or row["start_at"] > latest:
                bucket["latest_sample_at"] = row["start_at"]
            if row["start_at"] >= since:
                bucket["count_recent"] += 1
        return result

    rows = await db.pool().fetch(
        """
        SELECT sample_type,
               MAX(start_at) AS latest_sample_at,
               COUNT(*) FILTER (WHERE start_at >= $2::timestamptz) AS count_recent
        FROM health_samples
        WHERE user_id = $1::uuid
        GROUP BY sample_type
        """,
        user_id,
        since,
    )
    return {
        r["sample_type"]: {
            "latest_sample_at": r["latest_sample_at"],
            "count_recent": int(r["count_recent"]),
        }
        for r in rows
    }


async def daily_rollup(
    user_id: str,
    sample_types: list[str],
    *,
    since: datetime,
) -> list[dict[str, Any]]:
    """One row per (sample_type, UTC day): sum, avg and count of `value`."""
    if not sample_types:
        return []

    if _mem() is not None:
        buckets: dict[tuple[str, date], list[float]] = {}
        counts: dict[tuple[str, date], int] = {}
        for (owner, _provider, _ext), row in _mem().samples.items():
            if owner != user_id or row["sample_type"] not in sample_types:
                continue
            if row["start_at"] < since:
                continue
            key = (row["sample_type"], row["start_at"].astimezone(timezone.utc).date())
            counts[key] = counts.get(key, 0) + 1
            if row["value"] is not None:
                buckets.setdefault(key, []).append(float(row["value"]))
        out: list[dict[str, Any]] = []
        for key in sorted(counts, key=lambda k: (k[0], k[1])):
            values = buckets.get(key, [])
            out.append(
                {
                    "sample_type": key[0],
                    "day": key[1],
                    "sum_value": sum(values) if values else None,
                    "avg_value": (sum(values) / len(values)) if values else None,
                    "n": counts[key],
                }
            )
        return out

    rows = await db.pool().fetch(
        """
        SELECT sample_type,
               (start_at AT TIME ZONE 'UTC')::date AS day,
               SUM(value) AS sum_value,
               AVG(value) AS avg_value,
               COUNT(*) AS n
        FROM health_samples
        WHERE user_id = $1::uuid
          AND sample_type = ANY($2::text[])
          AND start_at >= $3::timestamptz
        GROUP BY sample_type, day
        ORDER BY sample_type, day
        """,
        user_id,
        sample_types,
        since,
    )
    return [
        {
            "sample_type": r["sample_type"],
            "day": r["day"],
            "sum_value": float(r["sum_value"]) if r["sum_value"] is not None else None,
            "avg_value": float(r["avg_value"]) if r["avg_value"] is not None else None,
            "n": int(r["n"]),
        }
        for r in rows
    ]


async def latest_per_type(
    user_id: str,
    sample_types: list[str],
    *,
    since: datetime,
) -> dict[str, dict[str, Any]]:
    """Most recent sample per type inside the window (value/unit only, no ids)."""
    if not sample_types:
        return {}

    if _mem() is not None:
        latest: dict[str, dict[str, Any]] = {}
        for (owner, _provider, _ext), row in _mem().samples.items():
            if owner != user_id or row["sample_type"] not in sample_types:
                continue
            if row["start_at"] < since:
                continue
            current = latest.get(row["sample_type"])
            if current is None or row["start_at"] > current["start_at"]:
                latest[row["sample_type"]] = {
                    "value": row["value"],
                    "unit": row["unit"],
                    "value_text": row["value_text"],
                    "start_at": row["start_at"],
                }
        return latest

    rows = await db.pool().fetch(
        """
        SELECT DISTINCT ON (sample_type)
               sample_type, value, unit, value_text, start_at
        FROM health_samples
        WHERE user_id = $1::uuid
          AND sample_type = ANY($2::text[])
          AND start_at >= $3::timestamptz
        ORDER BY sample_type, start_at DESC
        """,
        user_id,
        sample_types,
        since,
    )
    return {
        r["sample_type"]: {
            "value": float(r["value"]) if r["value"] is not None else None,
            "unit": r["unit"],
            "value_text": r["value_text"],
            "start_at": r["start_at"],
        }
        for r in rows
    }
