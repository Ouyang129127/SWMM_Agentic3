from typing_extensions import Annotated
from workflow_agents.orchestration import agent_actions

async def VerificationAgent(
    model_name: Annotated[str, "Model project name under project-root models/."],
    run_id: Annotated[str, "Run ID under models/<model_name>/runs/."],
    rerun: Annotated[bool, "Allow rerunning this stage."] = False,
    task_id: Annotated[str, "Required task to reference-check; no run-level fallback."] = "",
) -> str:
    """Workflow-stage VerificationAgent."""
    if task_id:
        return await agent_actions._investigation_action(model_name, run_id, 'verify', task_id)
    raise ValueError("VerificationAgent必须指定task_id；运行级核查已删除。")
