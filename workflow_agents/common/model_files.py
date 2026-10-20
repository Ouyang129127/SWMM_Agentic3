import re
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from workflow_agents.common.paths import APP_ROOT, MODELS_DIR
WORKSPACE_DIR = APP_ROOT
PROJECT_SCHEMA_NAME = "SWMM-2D project layout"
PROJECT_SCHEMA_VERSION = "models-readme-2026-07-18"


def _resolve_workspace_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    candidate = WORKSPACE_DIR / candidate
    return candidate

def _resolve_model_root(model_name: str) -> Path:
    if not model_name:
        model_names = sorted(path.name for path in MODELS_DIR.iterdir() if path.is_dir()) if MODELS_DIR.exists() else []
        if len(model_names) == 1:
            model_name = model_names[0]
        elif not model_names:
            return MODELS_DIR / "__no_models_found__"
        else:
            return MODELS_DIR / "__multiple_models_found__"
    return MODELS_DIR / model_name

def _model_selection_error(model_root: Path) -> Optional[str]:
    if model_root.name == "__no_models_found__":
        return "No SWMM-2D model projects found under the project-root models/ directory."
    if model_root.name == "__multiple_models_found__":
        model_names = sorted(path.name for path in MODELS_DIR.iterdir() if path.is_dir())
        return f"Multiple model projects found. Please specify model_name. Available models: {model_names}"
    return None

def _resolve_model_input(model_root: Path, path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    project_candidate = model_root / candidate
    if project_candidate.exists():
        return project_candidate
    return WORKSPACE_DIR / candidate

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _validate_model_event(model_root: Path, rainfall_path: Path, entries: List[Tuple[datetime, float]]) -> Dict[str, object]:
    """Validate that an event belongs to its selected model and is safe to run."""
    events_root = (model_root / "events").resolve()
    candidate = rainfall_path.resolve()
    try:
        relative_event = candidate.relative_to(events_root)
    except ValueError:
        return {"ok": False, "code": "EVENT_OUTSIDE_MODEL", "message": "Rainfall event must be located under this model's events/ directory."}

    values = [value for _, value in entries]
    duration_hours = (entries[-1][0] - entries[0][0]).total_seconds() / 3600.0
    max_intensity = max(values)
    max_allowed_intensity = 500.0
    if max_intensity > max_allowed_intensity:
        return {
            "ok": False,
            "code": "RAINFALL_INTENSITY_REJECTED",
            "message": f"Maximum rainfall intensity {max_intensity:g} mm/h exceeds the safety limit {max_allowed_intensity:g} mm/h.",
            "relative_event": str(relative_event).replace("\\", "/"),
            "max_intensity_mm_h": max_intensity,
            "duration_hours": duration_hours,
        }
    return {
        "ok": True,
        "relative_event": str(relative_event).replace("\\", "/"),
        "event_sha256": _sha256(candidate),
        "max_intensity_mm_h": max_intensity,
        "duration_hours": duration_hours,
        "record_count": len(entries),
    }

def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "run"

def _read_lines(inp_path: Path) -> List[str]:
    with open(inp_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()

def _write_lines(path: Path, lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)

def _section_name(line: str):
    match = re.match(r"^\s*\[([A-Za-z0-9_]+)\]\s*$", line)
    return match.group(1).upper() if match else None

def _find_section_bounds(lines: List[str], section: str):
    target = section.upper()
    start = None
    end = None
    for idx, line in enumerate(lines):
        name = _section_name(line)
        if name == target:
            start = idx
            continue
        if start is not None and name is not None:
            end = idx
            break
    if start is None:
        return None
    if end is None:
        end = len(lines)
    return start, end

def _insert_or_replace_section(lines: List[str], section: str, content_lines: List[str]) -> List[str]:
    bounds = _find_section_bounds(lines, section)
    block = [f"\n[{section.upper()}]\n"] + content_lines
    if content_lines and not content_lines[-1].endswith("\n"):
        block[-1] += "\n"
    if bounds is None:
        return lines + block
    start, end = bounds
    return lines[: start + 1] + content_lines + lines[end:]

def _parse_datetime_value_file(path: Path) -> List[Tuple[datetime, float]]:
    entries: List[Tuple[datetime, float]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith(";"):
            continue
        if "," in line:
            left, right = line.rsplit(",", 1)
        else:
            parts = line.split()
            if len(parts) < 3:
                continue
            left, right = " ".join(parts[:2]), parts[2]
        try:
            value = float(right.strip())
        except ValueError:
            continue

        dt_text = left.strip()
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
            try:
                parsed = datetime.strptime(dt_text, fmt)
                break
            except ValueError:
                pass
        if parsed is not None:
            entries.append((parsed, value))

    if len(entries) < 2:
        raise ValueError(f"Rainfall file needs at least two timestamp,value rows: {path}")
    return sorted(entries, key=lambda item: item[0])

def _format_swmm_interval(delta: timedelta) -> str:
    total_seconds = max(60, int(delta.total_seconds()))
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    return f"{hours}:{minutes:02d}"

def _infer_rainfall_interval(entries: List[Tuple[datetime, float]]) -> str:
    deltas = [
        entries[idx + 1][0] - entries[idx][0]
        for idx in range(len(entries) - 1)
        if entries[idx + 1][0] > entries[idx][0]
    ]
    return _format_swmm_interval(min(deltas) if deltas else timedelta(minutes=5))

def _find_raingage_timeseries(lines: List[str], rain_gage_name: str) -> Optional[str]:
    bounds = _find_section_bounds(lines, "RAINGAGES")
    if bounds is None:
        return None
    start, end = bounds
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        parts = stripped.split()
        if parts and parts[0] == rain_gage_name:
            if "TIMESERIES" in [part.upper() for part in parts]:
                for idx, part in enumerate(parts):
                    if part.upper() == "TIMESERIES" and idx + 1 < len(parts):
                        return parts[idx + 1]
            return parts[-1] if len(parts) >= 6 else None
    return None

def _replace_swmm_event_inputs(
    source_inp: Path,
    target_inp: Path,
    rainfall_entries: List[Tuple[datetime, float]],
    rain_gage_name: str = "rain1",
    timeseries_name: Optional[str] = None,
    timing: Optional[dict] = None,
) -> Dict[str, object]:
    lines = _read_lines(source_inp)
    # The generic entry point defaults to rain1, but public single-gage
    # benchmarks can use another name. Reuse that gage so subcatchments
    # receive the new event instead of adding an unused rain1 gage.
    gage_bounds = _find_section_bounds(lines, "RAINGAGES")
    source_gages = []
    if gage_bounds:
        for line in lines[gage_bounds[0] + 1:gage_bounds[1]]:
            parts = line.split(";", 1)[0].split()
            if parts:
                source_gages.append(parts[0])
    if rain_gage_name == "rain1" and "rain1" not in source_gages and len(source_gages) == 1:
        rain_gage_name = source_gages[0]
    start_dt = rainfall_entries[0][0]
    end_dt = rainfall_entries[-1][0]
    interval = _infer_rainfall_interval(rainfall_entries)
    event_entries = list(rainfall_entries)
    rain_end = end_dt
    if timing:
        # INTENSITY records describe an interval beginning at their timestamp.
        hours, minutes = map(int, interval.split(":"))
        rain_delta = timedelta(hours=hours, minutes=minutes)
        wet = [i for i, (_, value) in enumerate(rainfall_entries) if value > 0]
        if wet:
            last_wet = wet[-1]
            rain_end = rainfall_entries[last_wet][0] + rain_delta
            if last_wet + 1 < len(rainfall_entries):
                rain_end = min(rain_end, rainfall_entries[last_wet + 1][0])
        else:
            rain_end = end_dt
        end_dt = rain_end + timedelta(hours=float(timing["post_rainfall_hours"]))
        event_entries = [(t, v) for t, v in event_entries if t < rain_end]
        event_entries.append((rain_end, 0.0))
    timeseries_name = timeseries_name or _find_raingage_timeseries(lines, rain_gage_name) or "rain01"

    option_values = {
        "IGNORE_RAINFALL": "NO",
        "START_DATE": start_dt.strftime("%m/%d/%Y"),
        "START_TIME": start_dt.strftime("%H:%M:%S"),
        "REPORT_START_DATE": start_dt.strftime("%m/%d/%Y"),
        "REPORT_START_TIME": start_dt.strftime("%H:%M:%S"),
        "END_DATE": end_dt.strftime("%m/%d/%Y"),
        "END_TIME": end_dt.strftime("%H:%M:%S"),
    }
    if timing:
        seconds = int(float(timing["swmm_save_interval_minutes"]) * 60)
        option_values["REPORT_STEP"] = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"

    bounds = _find_section_bounds(lines, "OPTIONS")
    if bounds:
        start, end = bounds
        seen = set()
        for idx in range(start + 1, end):
            parts = lines[idx].strip().split()
            if len(parts) >= 2 and parts[0].upper() in option_values:
                key = parts[0].upper()
                seen.add(key)
                lines[idx] = f"{key.ljust(22)}{option_values[key]}\n"
        if timing:
            lines[end:end] = [f"{key.ljust(22)}{value}\n" for key, value in option_values.items() if key not in seen]

    if timing:
        bounds = _find_section_bounds(lines, "REPORT")
        contents = lines[bounds[0] + 1:bounds[1]] if bounds else []
        contents = [line for line in contents if not line.strip().split() or line.strip().split()[0].upper() not in {"NODES", "LINKS"}]
        lines = _insert_or_replace_section(lines, "REPORT", contents + ["NODES ALL\n", "LINKS ALL\n"])

    # Replace only the selected rainfall series. Other series can drive
    # downstream water levels or inflows and must survive event preparation.
    ts_bounds = _find_section_bounds(lines, "TIMESERIES")
    preserved_series = []
    if ts_bounds:
        for line in lines[ts_bounds[0] + 1:ts_bounds[1]]:
            parts = line.split(";", 1)[0].split()
            if parts and parts[0].casefold() != timeseries_name.casefold():
                preserved_series.append(line if line.endswith("\n") else line + "\n")
    ts_lines = [";;Name           Date       Time       Value\n", ";;-------------- ---------- ---------- ----------\n"] + preserved_series
    for dt_value, value in event_entries:
        ts_lines.append(
            f"{timeseries_name.ljust(16)} {dt_value.strftime('%m/%d/%Y')} "
            f"{dt_value.strftime('%H:%M').ljust(8)} {value:.3f}\n"
        )
    lines = _insert_or_replace_section(lines, "TIMESERIES", ts_lines)

    raingage_lines = [";;Name           Format    Interval SCF      Source\n", ";;-------------- --------- ------ ------ ----------\n"]
    replaced = False
    bounds = _find_section_bounds(lines, "RAINGAGES")
    existing = lines[bounds[0] + 1 : bounds[1]] if bounds else []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        parts = stripped.split()
        if parts and parts[0] == rain_gage_name:
            raingage_lines.append(
                f"{rain_gage_name.ljust(16)} INTENSITY {interval.ljust(8)} 1.0      TIMESERIES {timeseries_name}\n"
            )
            replaced = True
        else:
            raingage_lines.append(line if line.endswith("\n") else line + "\n")
    if not replaced:
        raingage_lines.append(
            f"{rain_gage_name.ljust(16)} INTENSITY {interval.ljust(8)} 1.0      TIMESERIES {timeseries_name}\n"
        )
    lines = _insert_or_replace_section(lines, "RAINGAGES", raingage_lines)

    _write_lines(target_inp, lines)
    effective_rainfall = None
    if timing:
        effective_rainfall = target_inp.parent / "rainfall_for_simulation.txt"
        effective_rainfall.write_text(
            "timestamp,value\n" + "".join(f"{t:%Y-%m-%d %H:%M:%S},{v:.3f}\n" for t, v in event_entries),
            encoding="utf-8",
        )
    return {
        "rain_gage_name": rain_gage_name,
        "timeseries_name": timeseries_name,
        "rainfall_points": len(rainfall_entries),
        "rainfall_start": start_dt.isoformat(sep=" "),
        "rainfall_end": rain_end.isoformat(sep=" "),
        "rainfall_interval": interval,
        "simulation_end": end_dt.isoformat(sep=" "),
        "post_rainfall_hours": float(timing["post_rainfall_hours"]) if timing else 0.0,
        "simulation_rainfall_file": str(effective_rainfall) if effective_rainfall else None,
    }

def _load_mapped_node_ids(mapping_path: Path) -> set[str]:
    if not mapping_path.exists():
        return set()
    import csv

    with mapping_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "node_id" not in reader.fieldnames:
            return set()
        return {str(row["node_id"]).strip() for row in reader if row.get("node_id")}
