import json

async def _investigation_action(model_name, run_id, action, task_id='', question=''):
    from workflow_agents.orchestration.investigation import advance_investigation
    if action == 'diagnose' and not question:
        from workflow_agents.orchestration.investigation_flow import diagnose_with_repair
        result = await diagnose_with_repair(model_name, run_id, task_id)
    else:
        result = await advance_investigation(model_name, run_id, action, task_id, question)
    return json.dumps(result, ensure_ascii=False, indent=2)
