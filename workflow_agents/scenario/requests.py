"""Deterministic ScenarioAgent core."""

from __future__ import annotations

import json
import re
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from workflow_agents.common.schemas import SCENARIO_READY, SCENARIO_REQUEST_SCHEMA_NAME, WORKFLOW_SCHEMA_VERSION, artifacts_for_run
from workflow_agents.common.state import MODELS_DIR, build_state, save_state


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_") or "run"


def _resolve_model_name(model_name: str) -> str:
    if model_name:
        return model_name
    model_names = sorted(path.name for path in MODELS_DIR.iterdir() if path.is_dir()) if MODELS_DIR.exists() else []
    if len(model_names) == 1:
        return model_names[0]
    if not model_names:
        raise FileNotFoundError("No model projects found under models/.")
    raise ValueError(f"Multiple model projects found; specify model_name. Available: {model_names}")


def _resolve_model_file(model_root: Path, value: str) -> Path:
    candidate = Path(value.replace("\\", "/"))
    if candidate.is_absolute():
        return candidate
    model_candidate = model_root / candidate
    if model_candidate.exists():
        return model_candidate
    return model_root.parent.parent / candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_scenario_request(
    model_name: str = "",
    rainfall_file: str = "",
    event_name: str = "",
    scenario_name: str = "baseline",
    run_id: str = "",
    requested_outputs: list[str] | None = None,
) -> dict[str, Any]:
    """Create scenario_request.json and mark the run SCENARIO_READY."""
    model_name = _resolve_model_name(model_name)
    model_root = MODELS_DIR / model_name
    if not model_root.exists():
        raise FileNotFoundError(f"Model project not found: {model_root}")
    if not rainfall_file:
        raise ValueError("rainfall_file is required for ScenarioAgent.")

    scenario_inp = model_root / "swmm" / "scenarios" / scenario_name / "model.inp"
    if not scenario_inp.exists():
        raise FileNotFoundError(f"Scenario input not found: {scenario_inp}")

    rainfall_path = _resolve_model_file(model_root, rainfall_file)
    if not rainfall_path.exists():
        raise FileNotFoundError(f"Rainfall event file not found: {rainfall_file}")
    events_root = (model_root / "events").resolve()
    try:
        event_relative = rainfall_path.resolve().relative_to(events_root)
    except ValueError as exc:
        raise ValueError("rainfall_file must be located under the selected model's events/ directory.") from exc

    event_name = event_name or rainfall_path.stem
    if not run_id:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"{_safe_id(event_name)}__{_safe_id(scenario_name)}__{timestamp}"
    else:
        run_id = _safe_id(run_id)

    run_root = model_root / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    artifacts = artifacts_for_run(run_root)
    payload = {
        "schema_name": SCENARIO_REQUEST_SCHEMA_NAME,
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "model_name": model_name,
        "event_name": event_name,
        "scenario_name": scenario_name,
        "run_id": run_id,
        "rainfall_file": "events/" + str(event_relative).replace("\\", "/"),
        "event_sha256": _sha256(rainfall_path),
        "scenario_inp": str(scenario_inp.relative_to(model_root)).replace("\\", "/"),
        "requested_outputs": requested_outputs or ["summary", "swmm", "ca2d"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stage_agent": "ScenarioAgent",
    }
    artifacts.scenario_request.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    state = build_state(model_name, run_id, run_root, state=SCENARIO_READY)
    state["history"] = [
        {
            "stage": "scenario",
            "target_state": SCENARIO_READY,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "rerun": False,
        }
    ]
    save_state(run_root, state)
    return {
        "ok": True,
        "message": "ScenarioAgent prepared scenario_request.json.",
        "scenario_request": payload,
        "workflow_state": state,
    }


def load_scenario_request(model_name: str, run_id: str) -> dict[str, Any]:
    model_name = _resolve_model_name(model_name)
    path = MODELS_DIR / model_name / "runs" / run_id / "scenario_request.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing scenario_request.json: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
