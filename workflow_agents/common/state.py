"""Workflow state loading, initialization, validation, and persistence."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from workflow_agents.common.schemas import COMPLETED_STAGE_BY_STATE, EVIDENCE_READY, FAILED, NO_SURFACE_INFLOW, NEXT_STAGE_BY_STATE, RUN_READY, SCENARIO_READY, WORKFLOW_SCHEMA_NAME, WORKFLOW_SCHEMA_VERSION, artifacts_for_run


from workflow_agents.common.paths import MODELS_DIR


def resolve_run_root(model_name: str, run_id: str) -> Path:
    if not model_name:
        model_names = sorted(path.name for path in MODELS_DIR.iterdir() if path.is_dir()) if MODELS_DIR.exists() else []
        if len(model_names) == 1:
            model_name = model_names[0]
        elif not model_names:
            raise FileNotFoundError("No model projects found under models/.")
        else:
            raise ValueError(f"Multiple model projects found; specify model_name. Available: {model_names}")
    if not run_id:
        raise ValueError("run_id is required.")
    run_root = MODELS_DIR / model_name / "runs" / run_id
    if run_root.is_dir():
        return run_root

    # A run ID is the durable hand-off between workflow stages.  The web chat
    # can retain an older model mention in its transcript, so do not turn that
    # stale mention into a different, non-existent run path.  Recover only
    # when the ID identifies exactly one run across the project; duplicated
    # IDs remain an explicit user choice instead of silently selecting one.
    candidates = [
        path
        for path in MODELS_DIR.glob(f"*/runs/{run_id}")
        if path.is_dir()
    ] if MODELS_DIR.exists() else []
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        locations = [str(path) for path in candidates]
        raise ValueError(
            f"Run ID is ambiguous across model projects; specify model_name. "
            f"Requested model: {model_name}. Matches: {locations}"
        )
    raise FileNotFoundError(f"Run directory not found: {run_root}")


def _rel(run_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(run_root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _has_current_evidence(run_root: Path) -> bool:
    from workflow_agents.evidence.evidence_package import load_package
    try:
        load_package(run_root)
    except (FileNotFoundError, ValueError):
        return False
    return True


def infer_state_from_artifacts(run_root: Path) -> str:
    artifacts = artifacts_for_run(run_root)
    if _has_current_evidence(run_root):
        return EVIDENCE_READY
    if artifacts.run_summary.exists() and artifacts.swmm_nodes.exists() and artifacts.swmm_links.exists() and artifacts.ca2d_surface_depth.exists():
        return RUN_READY
    if artifacts.run_summary.exists() and artifacts.swmm_nodes.exists() and artifacts.swmm_links.exists():
        try:
            summary = json.loads(artifacts.run_summary.read_text(encoding="utf-8"))
            if summary.get("ca2d", {}).get("status") == NO_SURFACE_INFLOW:
                return NO_SURFACE_INFLOW
        except (OSError, json.JSONDecodeError):
            pass
    if artifacts.scenario_request.exists():
        return SCENARIO_READY
    return FAILED


def build_state(model_name: str, run_id: str, run_root: Path, state: str | None = None, errors: list[str] | None = None) -> dict[str, Any]:
    artifacts = artifacts_for_run(run_root)
    current_state = state or infer_state_from_artifacts(run_root)
    completed = []
    for candidate in [SCENARIO_READY, RUN_READY, EVIDENCE_READY]:
        stage = COMPLETED_STAGE_BY_STATE[candidate]
        if candidate == SCENARIO_READY:
            ok = artifacts.scenario_request.exists()
        elif candidate == RUN_READY:
            ok = artifacts.run_summary.exists()
        else:
            ok = _has_current_evidence(run_root)
        if ok:
            completed.append(stage)

    return {
        "schema_name": WORKFLOW_SCHEMA_NAME,
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "model_name": model_name,
        "run_id": run_id,
        "state": current_state,
        "completed_stages": completed,
        "next_allowed_stage": NEXT_STAGE_BY_STATE.get(current_state),
        "artifacts": {
            "scenario_request": _rel(run_root, artifacts.scenario_request),
            "run_summary": _rel(run_root, artifacts.run_summary),
            "run_metadata": _rel(run_root, artifacts.run_metadata),
            "swmm_node_flooding": _rel(run_root, artifacts.swmm_node_flooding),
            "swmm_nodes": _rel(run_root, artifacts.swmm_nodes),
            "swmm_links": _rel(run_root, artifacts.swmm_links),
            "ca2d_surface_depth": _rel(run_root, artifacts.ca2d_surface_depth),
            "evidence_package": _rel(run_root, artifacts.evidence_package),
        },
        "errors": errors or [],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_or_initialize_state(model_name: str, run_id: str) -> tuple[Path, dict[str, Any]]:
    run_root = resolve_run_root(model_name, run_id)
    model_name = run_root.parents[1].name
    state_path = run_root / "workflow_state.json"
    if state_path.exists():
        existing = json.loads(state_path.read_text(encoding="utf-8"))
        if existing.get('schema_version') == WORKFLOW_SCHEMA_VERSION and existing.get('state') in NEXT_STAGE_BY_STATE:
            if existing.get('model_name') != model_name or existing.get('run_id') != run_id:
                raise ValueError('Workflow state belongs to another model/run')
            if existing['state'] == EVIDENCE_READY:
                if not _has_current_evidence(run_root):
                    return run_root, build_state(model_name, run_id, run_root)
            existing['next_allowed_stage'] = NEXT_STAGE_BY_STATE[existing['state']]
            return run_root, existing
        # Obsolete run-level diagnosis artifacts never count as new completion.
        return run_root, build_state(model_name, run_id, run_root)
    state = build_state(model_name, run_id, run_root)
    save_state(run_root, state)
    return run_root, state


def save_state(run_root: Path, state: dict[str, Any]) -> None:
    (run_root / "workflow_state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
