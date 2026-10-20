from typing_extensions import Annotated
from workflow_agents.orchestration import agent_actions
from workflow_agents.orchestration.run_preparation import run_workflow_stage

async def EvidenceBuilderAgent(
    model_name: Annotated[str, "Model project name under project-root models/."],
    run_id: Annotated[str, "Run ID under models/<model_name>/runs/."],
    rerun: Annotated[bool, "Allow rerunning this stage."] = False,
    task_id: Annotated[str, "Existing investigation task whose displayed evidence requests the user approved."] = "",
) -> str:
    """Workflow-stage EvidenceBuilderAgent."""
    if task_id:
        return await agent_actions._investigation_action(model_name, run_id, 'evidence', task_id)
    return run_workflow_stage(
        model_name=model_name,
        run_id=run_id,
        target_stage="evidence_building",
        rerun=rerun,
    )
