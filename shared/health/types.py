"""Closed health sample vocabulary — types, canonical units, Terra mapping.

The type and unit allowlists are closed on purpose: aggregates are only
comparable if every row of a type shares one unit, so client units are
converted here and anything unrecognized is ignored rather than invented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Mirrors the health_samples_sample_type_chk CHECK in migration 016.
SAMPLE_TYPES: tuple[str, ...] = (
    "steps",
    "heart_rate",
    "resting_heart_rate",
    "sleep",
    "workout",
    "active_energy",
    "distance_walking_running",
    "body_mass",
)

# Types whose payload may be a category label (sleep stage, activity name)
# instead of a number; these are the only types allowed a NULL unit.
CATEGORICAL_SAMPLE_TYPES: frozenset[str] = frozenset({"sleep", "workout"})

CANONICAL_UNITS: dict[str, str] = {
    "steps": "count",
    "heart_rate": "count/min",
    "resting_heart_rate": "count/min",
    "sleep": "min",
    "workout": "min",
    "active_energy": "kcal",
    "distance_walking_running": "m",
    "body_mass": "kg",
}

# Accepted client unit → multiplier into the canonical unit above.
ACCEPTED_UNITS: dict[str, dict[str, float]] = {
    "steps": {"count": 1.0, "steps": 1.0},
    "heart_rate": {"count/min": 1.0, "bpm": 1.0, "count/minute": 1.0},
    "resting_heart_rate": {"count/min": 1.0, "bpm": 1.0, "count/minute": 1.0},
    "sleep": {"min": 1.0, "minute": 1.0, "s": 1.0 / 60.0, "sec": 1.0 / 60.0, "hr": 60.0, "h": 60.0},
    "workout": {"min": 1.0, "minute": 1.0, "s": 1.0 / 60.0, "sec": 1.0 / 60.0, "hr": 60.0, "h": 60.0},
    "active_energy": {"kcal": 1.0, "cal": 1.0, "kj": 1.0 / 4.184},
    "distance_walking_running": {
        "m": 1.0,
        "km": 1000.0,
        "mi": 1609.344,
        "ft": 0.3048,
    },
    "body_mass": {"kg": 1.0, "g": 0.001, "lb": 0.45359237},
}

# How a day's rows collapse into one daily number for the AI summary.
DAILY_AGGREGATION: dict[str, str] = {
    "steps": "sum",
    "active_energy": "sum",
    "distance_walking_running": "sum",
    "sleep": "sum",
    "workout": "sum",
    "heart_rate": "avg",
    "resting_heart_rate": "avg",
    "body_mass": "avg",
}

# Terra's flattened metric keys (see shared/terra.py extract_metrics) → our
# closed vocabulary. Terra keys outside this map are dropped and counted.
TERRA_METRIC_MAP: dict[str, tuple[str, str]] = {
    "steps": ("steps", "count"),
    "distance_data.steps": ("steps", "count"),
    "distance_data.distance_meters": ("distance_walking_running", "m"),
    "heart_rate_data.avg_hr_bpm": ("heart_rate", "bpm"),
    "heart_rate_data.resting_hr_bpm": ("resting_heart_rate", "bpm"),
    "avg_hr_bpm": ("heart_rate", "bpm"),
    "resting_hr_bpm": ("resting_heart_rate", "bpm"),
    "calories_data.net_activity_calories": ("active_energy", "kcal"),
    "active_durations_data.activity_seconds": ("workout", "s"),
    "weight_kg": ("body_mass", "kg"),
}

MAX_EXTERNAL_ID_LENGTH = 200
MAX_VALUE_TEXT_LENGTH = 120
MAX_SOURCE_LENGTH = 200

# Ignore reasons. Returned to the caller for counting and tests; never logged
# alongside a health value.
IGNORE_UNKNOWN_TYPE = "unknown_type"
IGNORE_BAD_UNIT = "bad_unit"
IGNORE_BAD_TIMESTAMP = "bad_timestamp"
IGNORE_BAD_INTERVAL = "bad_interval"
IGNORE_MISSING_VALUE = "missing_value"
IGNORE_MISSING_ID = "missing_sample_id"
IGNORE_DUPLICATE_IN_BATCH = "duplicate_in_batch"


@dataclass(frozen=True)
class NormalizedSample:
    """One row ready for health_samples, in canonical units."""

    external_id: str
    sample_type: str
    start_at: datetime
    end_at: datetime
    value: Optional[float]
    unit: Optional[str]
    value_text: Optional[str]
    source_bundle: Optional[str]
    source_name: Optional[str]
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_timestamp(raw: Any) -> Optional[datetime]:
    """ISO 8601 (with `Z` accepted) → aware UTC datetime, else None."""
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def convert_unit(sample_type: str, value: float, unit: str) -> Optional[float]:
    """Value in the canonical unit for this type, or None if the unit is not accepted."""
    factors = ACCEPTED_UNITS.get(sample_type) or {}
    factor = factors.get(unit.strip().lower())
    if factor is None:
        return None
    return float(value) * factor


def _clip(raw: Any, limit: int) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text[:limit]


def normalize_sample(
    *,
    external_id: Any,
    sample_type: Any,
    start_at: Any,
    end_at: Any,
    value: Any = None,
    unit: Any = None,
    value_text: Any = None,
    source_bundle: Any = None,
    source_name: Any = None,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[Optional[NormalizedSample], Optional[str]]:
    """Validate + canonicalize one incoming sample.

    Returns (sample, None) on success or (None, reason) when the sample must be
    ignored. A bad sample never raises — one malformed row must not fail a
    whole device batch.
    """
    ext = _clip(external_id, MAX_EXTERNAL_ID_LENGTH)
    if not ext:
        return None, IGNORE_MISSING_ID

    stype = str(sample_type or "").strip().lower()
    if stype not in SAMPLE_TYPES:
        return None, IGNORE_UNKNOWN_TYPE

    start = parse_timestamp(start_at)
    end = parse_timestamp(end_at)
    if start is None or end is None:
        return None, IGNORE_BAD_TIMESTAMP
    if end < start:
        return None, IGNORE_BAD_INTERVAL

    text = _clip(value_text, MAX_VALUE_TEXT_LENGTH)

    numeric: Optional[float] = None
    if value is not None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None, IGNORE_MISSING_VALUE
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None, IGNORE_MISSING_VALUE

    canonical_unit: Optional[str] = None
    if numeric is not None:
        raw_unit = str(unit or "").strip()
        if not raw_unit:
            return None, IGNORE_BAD_UNIT
        converted = convert_unit(stype, numeric, raw_unit)
        if converted is None:
            return None, IGNORE_BAD_UNIT
        numeric = converted
        canonical_unit = CANONICAL_UNITS[stype]
    elif text is None:
        return None, IGNORE_MISSING_VALUE
    elif stype not in CATEGORICAL_SAMPLE_TYPES:
        # Numeric-only type sent without a number is unusable.
        return None, IGNORE_MISSING_VALUE

    return (
        NormalizedSample(
            external_id=ext,
            sample_type=stype,
            start_at=start,
            end_at=end,
            value=numeric,
            unit=canonical_unit,
            value_text=text,
            source_bundle=_clip(source_bundle, MAX_SOURCE_LENGTH),
            source_name=_clip(source_name, MAX_SOURCE_LENGTH),
            metadata=dict(metadata or {}),
        ),
        None,
    )
