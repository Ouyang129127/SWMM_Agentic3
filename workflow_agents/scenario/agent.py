import json
from typing_extensions import Annotated
from workflow_agents.common.messages import _contains_any
from workflow_agents.scenario.validation import list_rainfall_events, list_swmm_2d_models, check_swmm_2d_project
from workflow_agents import prepare_scenario_request

async def ScenarioAgent(
    message: Annotated[
        str,
        "User request about model project, rainfall event, scenario name, or run parameters.",
    ],
    model_name: Annotated[
        str,
        "Optional model project name under project-root models/.",
    ] = "",
    rainfall_file: Annotated[
        str,
        "Optional rainfall event path, usually events/<file>.txt. If provided, ScenarioAgent writes scenario_request.json.",
    ] = "",
    event_name: Annotated[
        str,
        "Optional event name for scenario_request.json.",
    ] = "",
    scenario_name: Annotated[
        str,
        "Scenario name under swmm/scenarios/<scenario_name>/.",
    ] = "baseline",
    run_id: Annotated[
        str,
        "Optional run ID for the prepared scenario request.",
    ] = "",
) -> str:
    """Workflow-stage ScenarioAgent: organize model/event/scenario prerequisites."""
    if rainfall_file:
        result = prepare_scenario_request(
            model_name=model_name,
            rainfall_file=rainfall_file,
            event_name=event_name,
            scenario_name=scenario_name,
            run_id=run_id,
        )
        return "ScenarioAgent completed:\n" + json.dumps(result, ensure_ascii=False, indent=2)

    wants_events = _contains_any(message, ["降雨", "rainfall", "event", "events", "雨型"])
    wants_check = _contains_any(message, ["检查", "验证", "完整", "check", "validate"])
    wants_list = _contains_any(message, ["列出", "list", "available", "有哪些"])

    if wants_events:
        return "ScenarioAgent completed:\n" + list_rainfall_events(model_name)
    if wants_check:
        return "ScenarioAgent completed:\n" + check_swmm_2d_project(model_name)
    if wants_list or not model_name:
        return "ScenarioAgent completed:\n" + list_swmm_2d_models()
    return "ScenarioAgent completed:\n" + check_swmm_2d_project(model_name)
