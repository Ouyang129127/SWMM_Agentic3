from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from workflow_agents.common.messages import _final_message_content, _is_unrelated_reply, _normalize_evidence_block, _fallback_validation_explanation, _fallback_simulation_explanation, _extract_agent_reply
from workflow_agents.interfaces.presentation import details
from workflow_agents.orchestration.prompts import orchestrator_prompt, simulation_evidence_explainer_prompt, validation_evidence_explainer_prompt, web_interactive_prompt

async def explain_validation_evidence(user_message: str, evidence: str) -> str:
    """Explain fixed-validator evidence without deciding project status in the web layer."""

    evidence_block, fence_type = _normalize_evidence_block(evidence)
    task = (
        "请把下面的 SWMM-2D 固定验证器结果解释给用户。\n"
        "严格规则：\n"
        "1. 只能依据 Evidence 中已有字段解释，不得新增缺失项、路径、文件名或检查结论。\n"
        "2. 如果 Evidence 中 problems 为空或 ready=true，只能说明未发现缺失项。\n"
        "3. 如果 Evidence 中列出 problems 或 missing，只能逐条解释这些已有问题。\n"
        "4. 必须说明本结果来自固定验证器，并提到 schema_name/schema_version/validator（如果 Evidence 中存在）。\n"
        "5. 用中文，只给两句结果概括；不逐字段复述JSON，不重复路径与结构规范。\n"
        "6. 不要输出代码，不要输出与 SWMM-2D 无关的内容。\n\n"
        f"User request:\n{user_message}\n\n"
        f"Evidence:\n```{fence_type}\n{evidence_block}\n```"
    )
    try:
        from workflow_agents.common.llm import deepseek_flash
        explainer = AssistantAgent(
            name="EvidenceExplainer",
            system_message=validation_evidence_explainer_prompt,
            model_client=deepseek_flash,
        )
        team = RoundRobinGroupChat(participants=[explainer], max_turns=1)
        result = await team.run(task=task)
        explanation = _final_message_content(result.messages)
        if not explanation or _is_unrelated_reply(explanation):
            explanation = _fallback_validation_explanation(evidence)
    except Exception:
        explanation = _fallback_validation_explanation(evidence)

    return (
        explanation.strip()
        + '\n\n' + details('完整检查证据', f"```{fence_type}\n{evidence_block}\n```")
    )

async def explain_simulation_evidence(user_message: str, evidence: str) -> str:
    """Explain fixed simulation-pipeline evidence without rerunning or altering facts."""

    evidence_block, fence_type = _normalize_evidence_block(evidence)
    task = (
        "请把下面的 SWMM-2D 固定模拟管线结果解释给用户。\n"
        "严格规则：\n"
        "1. 只能依据 Evidence 中已有字段解释，不得新增文件、指标、成功状态或失败原因。\n"
        "2. 必须说明本次执行走的是 tool-first 固定管线：降雨事件写入 SWMM -> PySWMM 运行 -> 导出节点溢流 -> CA2D 计算 -> 保存 runs 结果。\n"
        "3. 如果 Evidence 表明运行完成，概括 run_id、输出目录、SWMM 步数/保存步数、CA2D 最大水深等已有指标。\n"
        "4. 如果 Evidence 是错误文本，只解释该错误文本，并提示下一步应检查已有字段指向的路径或参数。\n"
        "5. 用中文，两句概括是否成功和主要指标；不逐字段复述JSON，不重复管线说明。\n"
        "6. 不要输出代码，不要输出与 SWMM-2D 无关的内容。\n\n"
        f"User request:\n{user_message}\n\n"
        f"Evidence:\n```{fence_type}\n{evidence_block}\n```"
    )
    try:
        from workflow_agents.common.llm import deepseek_flash
        explainer = AssistantAgent(
            name="SimulationEvidenceExplainer",
            system_message=simulation_evidence_explainer_prompt,
            model_client=deepseek_flash,
        )
        team = RoundRobinGroupChat(participants=[explainer], max_turns=1)
        result = await team.run(task=task)
        explanation = _final_message_content(result.messages)
        if not explanation or _is_unrelated_reply(explanation):
            explanation = _fallback_simulation_explanation(evidence)
    except Exception:
        explanation = _fallback_simulation_explanation(evidence)

    return (
        explanation.strip()
        + '\n\n' + details('完整模拟证据', f"```{fence_type}\n{evidence_block}\n```")
    )

async def run_web_orchestrator_agent_turn(task_prompt: str, planning_only: bool = False) -> str:
    """One text response; web workflow mutations belong to the approval controller."""
    from workflow_agents.common.llm import deepseek_flash

    mode_prompt = (
        "\nThis is a planning-only turn. Do not call any tools. Give a short step-by-step plan, "
        "state that no execution has been performed yet, and ask the user whether to continue.\n"
        if planning_only
        else ""
    )
    orchestrator = AssistantAgent(
        name="StatefulOrchestrator",
        system_message=orchestrator_prompt + web_interactive_prompt + mode_prompt + (
            '\nWeb execution is controlled by the application state machine. You have no tools. '
            'Return one explanation or plan, never invent execution, approvals, run_id or progress. '
            'Answer in concise Chinese: result, key point and next action. Do not repeat raw JSON, full reports or long scope lists. '
            'A user confirmation is handled by the application, not by your own subsequent messages.\n'
        ),
        model_client=deepseek_flash,
    )
    team = RoundRobinGroupChat(
        participants=[orchestrator],
        max_turns=1,
    )
    result = await team.run(task=task_prompt)
    return _extract_agent_reply(result.messages)
