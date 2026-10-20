"""Shared simulation execution and run-state persistence for all interfaces."""
import json
from pathlib import Path
from typing import Any
from workflow_agents.common.schemas import NO_SURFACE_INFLOW, RUN_READY
from workflow_agents.common.state import build_state, save_state
from workflow_agents.simulation.runner import run_swmm_2d_project_from_rainfall

def run_simulation_from_scenario_request(scenario_request: dict[str, Any], rerun: bool = False) -> str:
    """Run SimulationAgent from scenario_request and write RUN_READY state."""
    result = run_swmm_2d_project_from_rainfall(
        model_name=scenario_request["model_name"],
        rainfall_file=scenario_request["rainfall_file"],
        event_name=scenario_request["event_name"],
        scenario_name=scenario_request["scenario_name"],
        run_id=scenario_request["run_id"],
        expected_event_sha256=scenario_request.get("event_sha256", ""),
    )
    parsed = None
    try:
        parsed = json.loads(result.split("\n", 1)[1])
    except Exception:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("run_root"):
        run_root = Path(parsed["run_root"])
        ca2d_status = parsed.get("ca2d", {}).get("status")
        target_state = NO_SURFACE_INFLOW if ca2d_status == NO_SURFACE_INFLOW else RUN_READY
        state = build_state(parsed["model_name"], parsed["run_id"], run_root, state=target_state)
        existing_history = []
        state_path = run_root / "workflow_state.json"
        if state_path.exists():
            try:
                existing_history = json.loads(state_path.read_text(encoding="utf-8")).get("history", [])
            except json.JSONDecodeError:
                existing_history = []
        state["history"] = list(existing_history)
        state["history"].append(
            {
                "stage": "simulation",
                "target_state": target_state,
                "completed_at": parsed.get("created_at", ""),
                "rerun": bool(rerun),
            }
        )
        save_state(run_root, state)
        if parsed.get("ca2d", {}).get("surface_inflow_status") == NO_SURFACE_INFLOW:
            return (
                "SWMM 与 CA2D 均已完成。本次未检测到正节点溢流，"
                "因此二维边界入流为零；已生成地表水深为零的 CA2D 结果。\n\n"
                + result
            )
    return result
