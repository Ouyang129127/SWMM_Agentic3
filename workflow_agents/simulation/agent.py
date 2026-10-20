from workflow_agents.simulation.service import run_simulation_from_scenario_request
from typing_extensions import Annotated
from workflow_agents import load_scenario_request, prepare_scenario_request

async def SimulationAgent(
    model_name: Annotated[
        str,
        "Name of a SWMM-2D model project under project-root models/.",
    ],
    rainfall_file: Annotated[
        str,
        "Rainfall event file path, usually events/<file>.txt relative to the model project.",
    ],
    event_name: Annotated[
        str,
        "Rainfall event ID for run metadata.",
    ] = "manual_event",
    scenario_name: Annotated[
        str,
        "Scenario name under swmm/scenarios/<scenario_name>/.",
    ] = "baseline",
    run_id: Annotated[
        str,
        "Run ID. If scenario_request.json exists for this run, SimulationAgent reads it. Empty means generate from event, scenario, and timestamp.",
    ] = "",
) -> str:
    """Workflow-stage SimulationAgent: run the official fixed SWMM->CA2D pipeline."""
    scenario_request = None
    if run_id:
        try:
            scenario_request = load_scenario_request(model_name, run_id)
        except FileNotFoundError:
            scenario_request = None

    if scenario_request is None:
        if not rainfall_file:
            return "SimulationAgent failed: rainfall_file is required unless scenario_request.json already exists for run_id."
        prepared = prepare_scenario_request(
            model_name=model_name,
            rainfall_file=rainfall_file,
            event_name=event_name,
            scenario_name=scenario_name,
            run_id=run_id,
        )
        scenario_request = prepared["scenario_request"]

    result_text = run_simulation_from_scenario_request(scenario_request, rerun=bool(run_id))
    return "SimulationAgent completed:\n" + result_text
