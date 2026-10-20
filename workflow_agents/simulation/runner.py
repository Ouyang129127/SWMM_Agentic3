import json
import shutil
from datetime import datetime
from pathlib import Path
from typing_extensions import Annotated
from workflow_agents.common.paths import APP_ROOT
WORKSPACE_DIR = APP_ROOT
PROJECT_SCHEMA_NAME = "SWMM-2D project layout"
PROJECT_SCHEMA_VERSION = "models-readme-2026-07-18"
from workflow_agents.common.model_files import _load_mapped_node_ids, _model_selection_error, _parse_datetime_value_file, _replace_swmm_event_inputs, _resolve_model_input, _resolve_model_root, _safe_id, _validate_model_event
from workflow_agents.simulation.ca2d import check_static_model, run_ca2d_simulation
from workflow_agents.simulation.timing import load_timing, export_native_reports


def run_swmm_2d_project_from_rainfall(
    model_name: Annotated[
        str,
        "Name of a SWMM-2D model project under the project-root models/ directory.",
    ] = "",
    rainfall_file: Annotated[
        str,
        "Rainfall event file path. Relative paths are resolved first under the model project, then under the project root. Expected rows: timestamp,value.",
    ] = "",
    event_name: Annotated[
        str,
        "Rainfall event ID used for run metadata and comparison grouping.",
    ] = "manual_event",
    scenario_name: Annotated[
        str,
        "SWMM scenario ID under swmm/scenarios/<scenario_name>/.",
    ] = "baseline",
    run_id: Annotated[
        str,
        "Optional run ID. If empty, one is generated from event, scenario, and timestamp.",
    ] = "",
    rain_gage_name: Annotated[
        str,
        "SWMM rain gage name to receive the event rainfall.",
    ] = "rain1",
    timeseries_name: Annotated[
        str,
        "Optional SWMM time series name. If empty, the existing rain gage source is reused.",
    ] = "",
    swmm_save_interval_minutes: Annotated[
        float,
        "Interval in minutes for exporting SWMM node/link/flooding time series.",
    ] = 30.0,
    dt_seconds: Annotated[float, "CA2D internal time step in seconds."] = 10.0,
    boundary_interval_minutes: Annotated[
        float,
        "Boundary interpolation interval in minutes for SWMM node overflow injection.",
    ] = 5.0,
    ca2d_save_interval_minutes: Annotated[
        float,
        "Interval in minutes for writing CA2D tabular surface-depth records.",
    ] = 30.0,
    expected_event_sha256: Annotated[
        str,
        "Optional hash from scenario_request.json. A mismatch rejects a modified event file.",
    ] = "",
) -> str:
    model_root = _resolve_model_root(model_name)
    selection_error = _model_selection_error(model_root)
    if selection_error:
        return selection_error
    if not model_root.exists():
        return f"SWMM-2D model project not found: models/{model_name}"
    model_name = model_root.name
    if not rainfall_file:
        return "Rainfall event file is required. Use list_rainfall_events first if you need to discover available event files."

    # Current rainfall runs require an explicit policy and always use native
    # report timestamps. Reject missing policies before creating run artifacts.
    timing = load_timing(model_root, required=True)
    swmm_save_interval_minutes = float(timing["swmm_save_interval_minutes"])
    boundary_interval_minutes = float(timing["boundary_interval_minutes"])
    ca2d_save_interval_minutes = float(timing["ca2d_save_interval_minutes"])

    static_model = model_root / "static"
    static_status = check_static_model(static_model)
    if not static_status["ok"]:
        return f"CA2D static model is incomplete for {model_name}: {static_status['missing']}"

    scenario_inp = model_root / "swmm" / "scenarios" / scenario_name / "model.inp"
    if not scenario_inp.exists():
        return f"SWMM scenario input not found: {scenario_inp}"

    rainfall_path = _resolve_model_input(model_root, rainfall_file)
    if not rainfall_path.exists():
        return f"Rainfall event file not found: {rainfall_file}"

    rainfall_entries = _parse_datetime_value_file(rainfall_path)
    input_validation = _validate_model_event(model_root, rainfall_path, rainfall_entries)
    if input_validation.get("ok") and expected_event_sha256 and input_validation.get("event_sha256") != expected_event_sha256:
        input_validation = {
            "ok": False,
            "code": "EVENT_HASH_MISMATCH",
            "message": "Rainfall event changed after scenario confirmation; prepare a new scenario request before running.",
            "expected_event_sha256": expected_event_sha256,
            "actual_event_sha256": input_validation.get("event_sha256"),
        }
    if not input_validation["ok"]:
        return "SWMM-2D project run rejected:\n" + json.dumps(
            {"status": "INPUT_REJECTED", "model_name": model_name, "rainfall_file": str(rainfall_path), "input_validation": input_validation},
            ensure_ascii=False,
            indent=2,
        )

    if not run_id:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = f"{_safe_id(event_name)}__{_safe_id(scenario_name)}__{timestamp}"
    else:
        run_id = _safe_id(run_id)

    run_root = model_root / "runs" / run_id
    swmm_output_dir = run_root / "swmm"
    ca2d_output_dir = run_root / "ca2d"
    swmm_output_dir.mkdir(parents=True, exist_ok=True)
    ca2d_output_dir.mkdir(parents=True, exist_ok=True)

    event_copy = run_root / "rainfall_event.txt"
    shutil.copy2(rainfall_path, event_copy)
    modified_inp = swmm_output_dir / "model_with_event.inp"
    rainfall_summary = _replace_swmm_event_inputs(
        source_inp=scenario_inp,
        target_inp=modified_inp,
        rainfall_entries=rainfall_entries,
        rain_gage_name=rain_gage_name,
        timeseries_name=timeseries_name or None,
        timing=timing,
    )

    node_mapping = static_model / "node_to_cell_mapping.csv"
    swmm_result = export_native_reports(
        inp_path=modified_inp,
        output_dir=swmm_output_dir,
        mapped_ids=_load_mapped_node_ids(node_mapping),
        interval_minutes=swmm_save_interval_minutes,
    )

    ca2d_result = run_ca2d_simulation(
        model_dir=static_model,
        flooding_file=Path(swmm_result["flooding_file"]),
        output_dir=ca2d_output_dir,
        dt_seconds=dt_seconds,
        boundary_interval_minutes=boundary_interval_minutes,
        save_interval_minutes=ca2d_save_interval_minutes,
        output_txt_path=ca2d_output_dir / "surface_depth.tsv",
    )

    metadata = {
        "execution_policy": "tool-first",
        "pipeline_tool": "run_swmm_2d_project_from_rainfall",
        "schema_name": PROJECT_SCHEMA_NAME,
        "schema_version": PROJECT_SCHEMA_VERSION,
        "run_id": run_id,
        "model_name": model_name,
        "event_name": event_name,
        "scenario_name": scenario_name,
        "run_root": str(run_root),
        "source_scenario_inp": str(scenario_inp),
        "rainfall_file": str(rainfall_path),
        "rainfall_event_copy": str(event_copy),
        "static_model": str(static_model),
        "swmm_output_dir": str(swmm_output_dir),
        "ca2d_output_dir": str(ca2d_output_dir),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "rainfall": rainfall_summary,
        "simulation_timing": timing,
        "simulation_rainfall_file": rainfall_summary.get("simulation_rainfall_file"),
        "input_validation": input_validation,
        "swmm": swmm_result,
        "ca2d": ca2d_result,
    }
    # Persist model-local coupling semantics with the run. Inlet capture-node
    # overflow can be an internal street return, rather than added CA2D water.
    context_path = model_root / "mapping" / "coupling_policy.json"
    if context_path.is_file():
        metadata["model_context"] = json.loads(context_path.read_text(encoding="utf-8"))
        (run_root / "model_context.json").write_text(
            json.dumps(metadata["model_context"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (run_root / "summary.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_root / "run.yaml").write_text(
        "\n".join(
            [
                f"run_id: {run_id}",
                f"model_name: {model_name}",
                f"event_name: {event_name}",
                f"scenario_name: {scenario_name}",
                f"source_scenario_inp: {scenario_inp}",
                f"rainfall_file: {rainfall_path}",
                f"simulation_end: {rainfall_summary['simulation_end']}",
                f"rainfall_end: {rainfall_summary['rainfall_end']}",
                f"swmm_save_interval_minutes: {swmm_save_interval_minutes}",
                f"boundary_interval_minutes: {boundary_interval_minutes}",
                f"ca2d_save_interval_minutes: {ca2d_save_interval_minutes}",
                f"static_model: {static_model}",
                f"swmm_output_dir: {swmm_output_dir}",
                f"ca2d_output_dir: {ca2d_output_dir}",
                f"created_at: {metadata['created_at']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return "SWMM-2D project rainfall run completed:\n" + json.dumps(metadata, ensure_ascii=False, indent=2)
