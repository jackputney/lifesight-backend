"""Health ingest + read orchestration: validate, dedupe, upsert, summarize.

The model-facing summary is hard-capped in size and derived from aggregates —
raw samples are never serialized into a prompt or an API response.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from shared.health import store
from shared.health.types import (
    DAILY_AGGREGATION,
    IGNORE_DUPLICATE_IN_BATCH,
    SAMPLE_TYPES,
    TERRA_METRIC_MAP,
    NormalizedSample,
    iso,
    normalize_sample,
    parse_timestamp,
)

MAX_SYNC_BATCH = 1000
# MAX_SYNC_BATCH = 1000. A realistic sample JSON object is well under 1 KB
# (id, type, two timestamps, value, unit, source). Worst-case with every
# string at its Field max_length is still well under ~1 MB for 1000 samples:
#   sample_id 200 + type 64 + start/end 64+64 + unit 32 + value_text 120
#   + source_bundle 200 + source_name 200 + JSON keys/punctuation ≈ 1 KB
#   → ~1 MB for the array, plus wrapper. 2 MiB is enough headroom for a
#   max legal batch and far below a 42 MB / 300k-sample attack.
HEALTHKIT_SYNC_MAX_BODY_BYTES = 2 * 1024 * 1024
STATUS_WINDOW_DAYS = 30

MAX_CONTEXT_DAYS = 30
MIN_CONTEXT_DAYS = 1
DEFAULT_CONTEXT_DAYS = 7
# Hard ceiling on the tool result text handed back to Claude.
MAX_HEALTH_CONTEXT_CHARS = 2000

NO_DIAGNOSIS_NOTE = (
    "Trends only — describe patterns, never diagnose a disease or condition; "
    "defer clinical questions to a clinician."
)


class BatchTooLargeError(Exception):
    """More samples in one request than MAX_SYNC_BATCH."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedupe_last_wins(
    samples: list[NormalizedSample],
) -> tuple[list[NormalizedSample], int]:
    """Collapse repeated external_ids inside one batch; last occurrence wins."""
    by_id: dict[str, NormalizedSample] = {}
    dropped = 0
    for sample in samples:
        if sample.external_id in by_id:
            dropped += 1
        by_id[sample.external_id] = sample
    return list(by_id.values()), dropped


# ---------------------------------------------------------------------------
# HealthKit ingest
# ---------------------------------------------------------------------------

async def ingest_healthkit_samples(
    user_id: str,
    raw_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Upsert a device batch. Raises BatchTooLargeError above MAX_SYNC_BATCH.

    accepted = rows newly inserted, updated = existing rows whose stored values
    changed, ignored = rejected samples + in-batch duplicates + replays that
    changed nothing.
    """
    if len(raw_samples) > MAX_SYNC_BATCH:
        raise BatchTooLargeError(
            f"Batch of {len(raw_samples)} samples exceeds the {MAX_SYNC_BATCH} limit"
        )

    normalized: list[NormalizedSample] = []
    ignored = 0
    ignore_reasons: dict[str, int] = {}
    for raw in raw_samples:
        sample, reason = normalize_sample(
            external_id=raw.get("sample_id"),
            sample_type=raw.get("type"),
            start_at=raw.get("start_at"),
            end_at=raw.get("end_at"),
            value=raw.get("value"),
            unit=raw.get("unit"),
            value_text=raw.get("value_text"),
            source_bundle=raw.get("source_bundle"),
            source_name=raw.get("source_name"),
        )
        if sample is None:
            ignored += 1
            ignore_reasons[reason] = ignore_reasons.get(reason, 0) + 1
            continue
        normalized.append(sample)

    deduped, in_batch_dupes = _dedupe_last_wins(normalized)
    if in_batch_dupes:
        ignored += in_batch_dupes
        ignore_reasons[IGNORE_DUPLICATE_IN_BATCH] = in_batch_dupes

    inserted, updated, unchanged = await store.upsert_samples(
        user_id, store.PROVIDER_HEALTHKIT, deduped
    )
    ignored += unchanged
    synced_at = await store.touch_sync_state(user_id)

    return {
        "accepted": inserted,
        "updated": updated,
        "ignored": ignored,
        "server_time": iso(synced_at) or iso(_now()),
        "ignore_reasons": ignore_reasons,
    }


async def build_status(user_id: str) -> dict[str, Any]:
    """Per-category freshness for this user only. Never returns raw samples."""
    since = _now() - timedelta(days=STATUS_WINDOW_DAYS)
    per_type = await store.category_status(user_id, since=since)
    last_synced_at = await store.get_last_synced_at(user_id)
    categories = {
        sample_type: {
            "latest_sample_at": iso(per_type.get(sample_type, {}).get("latest_sample_at")),
            "count_last_30d": int(per_type.get(sample_type, {}).get("count_recent", 0)),
        }
        for sample_type in SAMPLE_TYPES
    }
    return {"last_synced_at": iso(last_synced_at), "categories": categories}


# ---------------------------------------------------------------------------
# Terra ingest (same table, provider='terra')
# ---------------------------------------------------------------------------

def terra_external_id(
    *,
    metric_type: str,
    recorded_at: datetime,
    source_device: Optional[str],
    value: Optional[float],
) -> str:
    """Deterministic id so a replayed webhook upserts instead of duplicating."""
    raw = "|".join(
        [
            metric_type,
            iso(recorded_at) or "",
            source_device or "",
            "" if value is None else repr(float(value)),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def normalize_terra_metrics(
    metrics: list[dict[str, Any]],
) -> tuple[list[NormalizedSample], int]:
    """Map Terra's flattened metrics onto the closed vocabulary.

    Returns (samples, ignored). Dropped and counted, never invented: metrics
    with no mapping, no value, or no parseable recorded_at. The timestamp is
    part of the synthesized external_id, so substituting now() for a missing
    one would give the same metric a new id on every replay — duplicate rows,
    and a fabricated time on a health reading.
    """
    samples: list[NormalizedSample] = []
    ignored = 0
    for metric in metrics:
        metric_type = str(metric.get("metric_type") or "").strip().lower()
        mapped = TERRA_METRIC_MAP.get(metric_type)
        value = metric.get("value")
        if mapped is None or value is None:
            ignored += 1
            continue
        sample_type, source_unit = mapped
        recorded_at = parse_timestamp(metric.get("recorded_at"))
        if recorded_at is None:
            ignored += 1
            continue
        source_device = metric.get("source_device")
        sample, _reason = normalize_sample(
            external_id=terra_external_id(
                metric_type=metric_type,
                recorded_at=recorded_at,
                source_device=source_device,
                value=value,
            ),
            sample_type=sample_type,
            # Terra summary metrics are point-in-time for our purposes.
            start_at=recorded_at,
            end_at=recorded_at,
            value=value,
            unit=source_unit,
            source_name=source_device,
            metadata={"terra_metric_type": metric_type},
        )
        if sample is None:
            ignored += 1
            continue
        samples.append(sample)
    return samples, ignored


async def ingest_terra_metrics(
    user_id: str,
    metrics: list[dict[str, Any]],
) -> tuple[int, int]:
    """Upsert mapped Terra metrics. Returns (written, ignored).

    written = inserted + updated. ignored = unmappable metrics (no mapping, no
    value, no parseable recorded_at) + duplicates inside one payload + replayed
    rows whose stored content did not change, so replaying a webhook returns
    written 0.
    """
    samples, ignored = normalize_terra_metrics(metrics)
    deduped, in_batch_dupes = _dedupe_last_wins(samples)
    ignored += in_batch_dupes
    inserted, updated, unchanged = await store.upsert_samples(
        user_id, store.PROVIDER_TERRA, deduped
    )
    return inserted + updated, ignored + unchanged


# ---------------------------------------------------------------------------
# Bounded model-facing summary
# ---------------------------------------------------------------------------

def _fmt(value: float, digits: int = 1) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _display(sample_type: str, value: float) -> str:
    """Canonical stored value → a short human/spoken-friendly string."""
    if sample_type == "sleep":
        return f"{_fmt(value / 60.0)} h"
    if sample_type == "workout":
        return f"{_fmt(value)} min"
    if sample_type == "distance_walking_running":
        return f"{_fmt(value / 1000.0, 2)} km"
    if sample_type in ("heart_rate", "resting_heart_rate"):
        return f"{_fmt(value, 0)} bpm"
    if sample_type == "body_mass":
        return f"{_fmt(value, 1)} kg"
    if sample_type == "active_energy":
        return f"{_fmt(value, 0)} kcal"
    return _fmt(value, 0)


def clamp_days(raw: Any) -> int:
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_DAYS
    return max(MIN_CONTEXT_DAYS, min(days, MAX_CONTEXT_DAYS))


def clean_types(raw: Any) -> list[str]:
    """Requested types intersected with the allowlist, in allowlist order."""
    if not isinstance(raw, (list, tuple)):
        return []
    wanted = {str(t).strip().lower() for t in raw}
    return [t for t in SAMPLE_TYPES if t in wanted]


async def build_health_context(user_id: str, sample_types: list[str], days: int) -> str:
    """Aggregate summary for the AI tool, capped at MAX_HEALTH_CONTEXT_CHARS."""
    since = _now() - timedelta(days=days)
    rollup = await store.daily_rollup(user_id, sample_types, since=since)
    latest = await store.latest_per_type(user_id, sample_types, since=since)

    per_type: dict[str, list[dict[str, Any]]] = {}
    for row in rollup:
        per_type.setdefault(row["sample_type"], []).append(row)

    lines: list[str] = [f"Health summary, last {days} day(s), aggregates only:"]
    for sample_type in sample_types:
        days_rows = per_type.get(sample_type) or []
        if not days_rows:
            lines.append(f"- {sample_type}: no data in this window.")
            continue

        mode = DAILY_AGGREGATION.get(sample_type, "avg")
        daily_values = [
            row["sum_value"] if mode == "sum" else row["avg_value"]
            for row in days_rows
            if (row["sum_value"] if mode == "sum" else row["avg_value"]) is not None
        ]
        sample_count = sum(row["n"] for row in days_rows)
        parts = [f"{len(days_rows)} day(s), {sample_count} sample(s)"]
        if daily_values:
            mean = sum(daily_values) / len(daily_values)
            label = "avg/day" if mode == "sum" else "avg"
            parts.append(f"{label} {_display(sample_type, mean)}")
            parts.append(f"range {_display(sample_type, min(daily_values))}"
                         f"–{_display(sample_type, max(daily_values))}")
        last = latest.get(sample_type)
        if last is not None:
            if last.get("value") is not None:
                parts.append(
                    f"latest {_display(sample_type, float(last['value']))} "
                    f"at {iso(last['start_at'])}"
                )
            elif last.get("value_text"):
                parts.append(f"latest {last['value_text']} at {iso(last['start_at'])}")
        lines.append(f"- {sample_type}: " + "; ".join(parts) + ".")

    lines.append(NO_DIAGNOSIS_NOTE)
    text = "\n".join(lines)
    if len(text) > MAX_HEALTH_CONTEXT_CHARS:
        keep = MAX_HEALTH_CONTEXT_CHARS - len(NO_DIAGNOSIS_NOTE) - 2
        text = text[:keep].rstrip() + "\n" + NO_DIAGNOSIS_NOTE
    return text


__all__ = [
    "BatchTooLargeError",
    "HEALTHKIT_SYNC_MAX_BODY_BYTES",
    "MAX_HEALTH_CONTEXT_CHARS",
    "MAX_SYNC_BATCH",
    "NO_DIAGNOSIS_NOTE",
    "build_health_context",
    "build_status",
    "clamp_days",
    "clean_types",
    "ingest_healthkit_samples",
    "ingest_terra_metrics",
    "normalize_terra_metrics",
    "terra_external_id",
]
