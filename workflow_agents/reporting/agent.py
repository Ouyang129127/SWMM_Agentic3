from typing_extensions import Annotated
from workflow_agents.orchestration import agent_actions

async def ReportAgent(
    model_name: Annotated[str, "Model project name under project-root models/."],
    run_id: Annotated[str, "Run ID containing saved simulation and verified diagnosis files."],
    message: Annotated[str, "User-facing report or explanation request."] = "",
    task_id: Annotated[str, "Verified task; omit to resolve this run's active task."] = "",
) -> str:
    """Generate a self-contained HTML report from this task's saved results."""
    from workflow_agents.orchestration.investigation_flow import load_active_task
    state = load_active_task(model_name, run_id, task_id)
    if state is None:
        raise ValueError('ReportAgent需要已完成诊断与引用核查的任务。')
    action = 'report' if state['state'] == 'ready_for_report' else 'display_report'
    return await agent_actions._investigation_action(model_name, run_id, action, state['task_id'])
