"""Rainfall event context extraction for diagnostic evidence packages."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from workflow_agents.common.schemas import RAINFALL_CONTEXT_SCHEMA_NAME, WORKFLOW_SCHEMA_VERSION


LIGHT_RAIN_TOTAL_MM = 10.0
MODERATE_RAIN_TOTAL_MM = 25.0
HEAVY_RAIN_TOTAL_MM = 50.0
RAINSTORM_TOTAL_MM = 100.0
SHORT_DURATION_HEAVY_INTENSITY_MM_H = 20.0
SHORT_DURATION_MAX_MINUTES = 180.0


def _empty_context(event_name: str, rainfall_file: str = "", reason: str = "missing_rainfall_file") -> dict[str, Any]:
    return {
        "schema_name": RAINFALL_CONTEXT_SCHEMA_NAME,
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "event_name": event_name,
        "rainfall_file": rainfall_file,
        "unit": "mm/h",
        "total_rainfall_mm": None,
        "rainfall_duration_min": None,
        "effective_rainfall_duration_min": None,
        "max_intensity_mm_per_h": None,
        "mean_intensity_mm_per_h": None,
        "rainfall_peak_time": None,
        "intensity_class": "unknown",
        "short_duration_heavy_rainfall": False,
        "classification_basis": "internal_thresholds_pending_local_standard_calibration",
        "classification_note": reason,
        "rainfall_points": 0,
    }


def _read_rainfall_points(path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    unit = "mm/h"
    for raw_line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("单位") or line.lower().startswith("unit"):
            if "mm/h" in line:
                unit = "mm/h"
            continue
        if "," not in line:
            continue
        time_text, value_text = line.rsplit(",", 1)
        timestamp = pd.to_datetime(time_text.strip(), errors="coerce")
        value = pd.to_numeric(value_text.strip(), errors="coerce")
        if pd.isna(timestamp) or pd.isna(value):
            continue
        rows.append({"timestamp": timestamp, "intensity_mm_per_h": float(value)})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "intensity_mm_per_h", "unit"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    df["unit"] = unit
    return df.reset_index(drop=True)


def _stepwise_total_rainfall_mm(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.0
    total = 0.0
    timestamps = df["timestamp"].tolist()
    intensities = df["intensity_mm_per_h"].tolist()
    for idx in range(len(df) - 1):
        delta_hours = (timestamps[idx + 1] - timestamps[idx]).total_seconds() / 3600.0
        if delta_hours > 0:
            total += float(intensities[idx]) * delta_hours
    return float(total)


def _stepwise_positive_duration_min(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.0
    duration = 0.0
    timestamps = df["timestamp"].tolist()
    intensities = df["intensity_mm_per_h"].tolist()
    for idx in range(len(df) - 1):
        delta_minutes = (timestamps[idx + 1] - timestamps[idx]).total_seconds() / 60.0
        if delta_minutes > 0 and float(intensities[idx]) > 0.0:
            duration += delta_minutes
    return float(duration)


def _classify_rainfall(total_mm: float, duration_min: float, max_intensity: float) -> tuple[str, bool, str]:
    short_heavy = max_intensity >= SHORT_DURATION_HEAVY_INTENSITY_MM_H and duration_min <= SHORT_DURATION_MAX_MINUTES
    if short_heavy:
        return (
            "short_duration_heavy_rainfall",
            True,
            f"max_intensity >= {SHORT_DURATION_HEAVY_INTENSITY_MM_H} mm/h and duration <= {SHORT_DURATION_MAX_MINUTES} min",
        )
    if total_mm >= RAINSTORM_TOTAL_MM:
        return "heavy_rainstorm", False, f"total_rainfall >= {RAINSTORM_TOTAL_MM} mm"
    if total_mm >= HEAVY_RAIN_TOTAL_MM:
        return "rainstorm", False, f"total_rainfall >= {HEAVY_RAIN_TOTAL_MM} mm"
    if total_mm >= MODERATE_RAIN_TOTAL_MM:
        return "heavy_rain", False, f"total_rainfall >= {MODERATE_RAIN_TOTAL_MM} mm"
    if total_mm >= LIGHT_RAIN_TOTAL_MM:
        return "moderate_rain", False, f"total_rainfall >= {LIGHT_RAIN_TOTAL_MM} mm"
    return "light_rain", False, f"total_rainfall < {LIGHT_RAIN_TOTAL_MM} mm"


def build_rainfall_context(
    rainfall_file: Path,
    event_name: str = "",
    run_id: str = "",
    model_name: str = "",
    scenario_name: str = "",
) -> dict[str, Any]:
    """Parse a rainfall event file and produce diagnostic rainfall context."""
    if not rainfall_file.exists():
        return _empty_context(event_name, str(rainfall_file))

    df = _read_rainfall_points(rainfall_file)
    if df.empty:
        return _empty_context(event_name, str(rainfall_file), reason="no_parseable_rainfall_points")

    first_time = df["timestamp"].min()
    last_time = df["timestamp"].max()
    duration_min = float((last_time - first_time).total_seconds() / 60.0) if len(df) > 1 else 0.0
    effective_duration_min = _stepwise_positive_duration_min(df)

    max_idx = df["intensity_mm_per_h"].idxmax()
    max_intensity = float(df.loc[max_idx, "intensity_mm_per_h"])
    total_mm = _stepwise_total_rainfall_mm(df)
    mean_intensity = float(total_mm / (duration_min / 60.0)) if duration_min > 0 else 0.0
    intensity_class, short_heavy, classification_note = _classify_rainfall(total_mm, duration_min, max_intensity)

    return {
        "schema_name": RAINFALL_CONTEXT_SCHEMA_NAME,
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "run_id": run_id,
        "model_name": model_name,
        "event_name": event_name or rainfall_file.stem,
        "scenario_name": scenario_name,
        "rainfall_file": str(rainfall_file),
        "unit": str(df["unit"].iloc[0]) if "unit" in df else "mm/h",
        "total_rainfall_mm": round(total_mm, 6),
        "rainfall_duration_min": round(duration_min, 6),
        "effective_rainfall_duration_min": round(effective_duration_min, 6),
        "max_intensity_mm_per_h": round(max_intensity, 6),
        "mean_intensity_mm_per_h": round(mean_intensity, 6),
        "rainfall_start": pd.Timestamp(first_time).isoformat(sep=" "),
        "rainfall_end": pd.Timestamp(last_time).isoformat(sep=" "),
        "rainfall_peak_time": pd.Timestamp(df.loc[max_idx, "timestamp"]).isoformat(sep=" "),
        "intensity_class": intensity_class,
        "short_duration_heavy_rainfall": short_heavy,
        "classification_basis": "internal_thresholds_pending_local_standard_calibration",
        "classification_note": classification_note,
        "rainfall_points": int(len(df)),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
