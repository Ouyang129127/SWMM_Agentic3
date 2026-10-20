from autogen_agentchat.agents import AssistantAgent, UserProxyAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.ui import Console
from workflow_agents.orchestration.prompts import orchestrator_prompt
from workflow_agents.scenario.agent import ScenarioAgent
from workflow_agents.simulation.agent import SimulationAgent
from workflow_agents.evidence.agent import EvidenceBuilderAgent
from workflow_agents.diagnosis.agent import DiagnosisAgent
from workflow_agents.verification.agent import VerificationAgent
from workflow_agents.reporting.agent import ReportAgent

async def main(task_description):
    from workflow_agents.common.llm import deepseek_flash

    user = UserProxyAgent("user", input_func=input)
    orchestrator = AssistantAgent(
        name="StatefulOrchestrator",
        system_message=orchestrator_prompt,
        model_client=deepseek_flash,
        tools=[
            ScenarioAgent,
            SimulationAgent,
            EvidenceBuilderAgent,
            DiagnosisAgent,
            VerificationAgent,
                ReportAgent,
                ],
    )

    termination = MaxMessageTermination(30) | TextMentionTermination("TERMINATE")
    team = RoundRobinGroupChat(
        participants=[orchestrator, user],
        termination_condition=termination,
        max_turns=30,
    )

    stream = team.run_stream(task=task_description)
    await Console(stream)
