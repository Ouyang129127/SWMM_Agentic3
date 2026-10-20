import json
from pathlib import Path
from typing_extensions import Annotated
from workflow_agents.common.paths import APP_ROOT, MODELS_DIR
WORKSPACE_DIR = APP_ROOT
PROJECT_SCHEMA_NAME = "SWMM-2D project layout"
PROJECT_SCHEMA_VERSION = "models-readme-2026-07-18"
from workflow_agents.common.model_files import _model_selection_error, _resolve_model_root, _resolve_workspace_path
from workflow_agents.simulation.ca2d import check_static_model


def check_ca2d_model(
    model_dir: Annotated[
        str,
        "Path to a CA2D static model directory relative to the project root. If empty, use the selected model project's static/ directory.",
    ] = "",
) -> str:
    if model_dir:
        model_path = _resolve_workspace_path(model_dir)
        display_dir = model_dir
        if (model_path / "static").is_dir():
            model_path = model_path / "static"
            display_dir = str(Path(model_dir) / "static")
    else:
        model_root = _resolve_model_root("")
        selection_error = _model_selection_error(model_root)
        if selection_error:
            return selection_error
        model_path = model_root / "static"
        display_dir = str(model_path)
    status = check_static_model(model_path)
    if not status["ok"]:
        return (
            "CA2D static model is incomplete.\n"
            f"Schema: {PROJECT_SCHEMA_NAME} ({PROJECT_SCHEMA_VERSION})\n"
            "Validator: check_ca2d_model\n"
            f"Model directory: {display_dir}\n"
            f"Missing files: {status['missing']}"
        )
    return (
        "CA2D static model is ready.\n"
        f"Schema: {PROJECT_SCHEMA_NAME} ({PROJECT_SCHEMA_VERSION})\n"
        "Validator: check_ca2d_model\n"
        f"Model directory: {display_dir}\n"
        f"Grid shape: {status['grid_shape']}\n"
        f"Cell size: {status['cell_size']} m\n"
        f"Mapped SWMM nodes: {status['mapped_nodes']}\n"
        f"Flowable cells: {status['flow_cells']}"
    )

def list_swmm_2d_models() -> str:
    if not MODELS_DIR.exists():
        return "No models directory exists yet."

    rows = []
    for model_root in sorted(MODELS_DIR.iterdir()):
        if not model_root.is_dir():
            continue
        marker = model_root / "model.yaml"
        static_status = check_static_model(model_root / "static") if (model_root / "static").exists() else {"ok": False}
        rows.append(
            {
                "schema_name": PROJECT_SCHEMA_NAME,
                "schema_version": PROJECT_SCHEMA_VERSION,
                "validator": "list_swmm_2d_models",
                "model": model_root.name,
                "has_model_yaml": marker.exists(),
                "static_ready": bool(static_status.get("ok")),
                "baseline_inp": (model_root / "swmm" / "scenarios" / "baseline" / "model.inp").exists(),
            }
        )

    if not rows:
        return "No SWMM-2D model projects found under the project-root models/ directory."
    return json.dumps(rows, ensure_ascii=False, indent=2)

def list_rainfall_events(
    model_name: Annotated[
        str,
        "Name of a SWMM-2D model project under the project-root models/ directory.",
    ] = "",
) -> str:
    model_root = _resolve_model_root(model_name)
    selection_error = _model_selection_error(model_root)
    if selection_error:
        return selection_error
    model_name = model_root.name
    events_dir = model_root / "events"
    if not model_root.exists():
        return f"SWMM-2D model project not found: models/{model_name}"
    if not events_dir.exists():
        return f"Rainfall events directory not found: {events_dir}"

    rainfall_extensions = {".txt", ".csv", ".dat", ".rain", ".prcp", ".precip", ".ts"}
    rows = []
    for file_path in sorted(path for path in events_dir.rglob("*") if path.is_file()):
        if file_path.suffix.lower() not in rainfall_extensions:
            continue
        preview = []
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as f:
                for idx, line in enumerate(f):
                    if idx >= 3:
                        break
                    preview.append(line.strip())
        except Exception as exc:
            preview = [f"failed to read preview: {exc}"]
        rows.append(
            {
                "file": str(file_path),
                "relative_to_model": str(file_path.relative_to(model_root)),
                "size_bytes": file_path.stat().st_size,
                "preview": preview,
            }
        )

    if not rows:
        return f"No rainfall event files found under {events_dir}"
    return json.dumps(rows, ensure_ascii=False, indent=2)

def check_swmm_2d_project(
    model_name: Annotated[
        str,
        "Name of a SWMM-2D model project under the project-root models/ directory.",
    ] = "",
) -> str:
    model_root = _resolve_model_root(model_name)
    selection_error = _model_selection_error(model_root)
    if selection_error:
        return selection_error
    model_name = model_root.name
    if not model_root.exists():
        return f"SWMM-2D model project not found: models/{model_name}"

    required_dirs = ["raw", "static", "swmm", "mapping", "events", "runs"]
    missing_dirs = [name for name in required_dirs if not (model_root / name).is_dir()]
    baseline_inp = model_root / "swmm" / "scenarios" / "baseline" / "model.inp"
    static_status = check_static_model(model_root / "static")
    problems = []
    if missing_dirs:
        problems.append(f"missing directories: {missing_dirs}")
    if not (model_root / "model.yaml").exists():
        problems.append("missing model.yaml")
    if not baseline_inp.exists():
        problems.append("missing baseline SWMM input: swmm/scenarios/baseline/model.inp")
    if not static_status["ok"]:
        problems.append(f"incomplete CA2D static model: {static_status['missing']}")

    summary = {
        "schema_name": PROJECT_SCHEMA_NAME,
        "schema_version": PROJECT_SCHEMA_VERSION,
        "validator": "check_swmm_2d_project",
        "model_name": model_name,
        "model_root": str(model_root),
        "ready": not problems,
        "problems": problems,
        "baseline_inp": str(baseline_inp),
        "static_model": static_status,
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)

def check_all_swmm_2d_projects() -> str:
    if not MODELS_DIR.exists():
        return "No models directory exists yet."

    model_names = sorted(path.name for path in MODELS_DIR.iterdir() if path.is_dir())
    if not model_names:
        return "No SWMM-2D model projects found under the project-root models/ directory."

    summaries = []
    for model_name in model_names:
        summaries.append(json.loads(check_swmm_2d_project(model_name)))
    return json.dumps(
        {
            "schema_name": PROJECT_SCHEMA_NAME,
            "schema_version": PROJECT_SCHEMA_VERSION,
            "validator": "check_all_swmm_2d_projects",
            "project_count": len(summaries),
            "projects": summaries,
        },
        ensure_ascii=False,
        indent=2,
    )
