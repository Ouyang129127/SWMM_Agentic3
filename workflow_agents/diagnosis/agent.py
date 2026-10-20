from typing_extensions import Annotated
from workflow_agents.orchestration import agent_actions

async def DiagnosisAgent(
    model_name: Annotated[str, "Model project name under project-root models/."],
    run_id: Annotated[str, "Run ID under models/<model_name>/runs/."],
    rerun: Annotated[bool, "Allow rerunning this stage."] = False,
    message: Annotated[str, "Empty starts the default whole-run preliminary overflow diagnosis. A user follow-up question prepares a scoped task."] = "",
    task_id: Annotated[str, "Existing task runs one diagnosis/revision; without task/question the initial diagnosis, reference check and report are delivered together."] = "",
) -> str:
    """Workflow-stage DiagnosisAgent."""
    action = 'diagnose' if task_id else 'prepare' if message.strip() else 'initial'
    return await agent_actions._investigation_action(model_name, run_id, action, task_id, message)
