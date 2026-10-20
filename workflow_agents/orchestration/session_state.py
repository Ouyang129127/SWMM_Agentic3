"""Serializable approval state for the web UI, separate from run/task progress."""
from dataclasses import asdict, dataclass, fields


ACTION_LABELS = {
    'scenario': 'ScenarioAgent：准备情景请求',
    'simulation': 'SimulationAgent：执行 SWMM-2D 模拟',
    'evidence_building': 'EvidenceBuilderAgent：构建统一证据',
    'prepare_diagnosis': 'DiagnosisAgent：准备诊断任务与可见证据',
    'diagnose': 'DiagnosisAgent：执行一次诊断／修正',
    'verify': 'VerificationAgent：核查证据引用',
    'report': 'ReportAgent：生成并交付报告',
    'display_report': 'ReportAgent：重试 HTML 报告生成',
    'evidence': 'EvidenceBuilderAgent：执行已展示的补证请求',
}

TASK_ACTIONS = {
    'ready_for_diagnosis': 'diagnose',
    'revision_requested': 'diagnose',
    'ready_for_verification': 'verify',
    'ready_for_report': 'report',
    'awaiting_evidence_confirmation': 'evidence',
}


@dataclass
class WebWorkflow:
    version: int = 0
    status: str = 'idle'
    model_name: str = ''
    rainfall_file: str = ''
    scenario_name: str = 'baseline'
    run_id: str = ''
    task_id: str = ''
    question: str = ''
    next_action: str = ''
    state_token: str = ''
    last_error: str = ''

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        names = {f.name for f in fields(cls)}
        flow = cls(**{k: v for k, v in value.items() if k in names})
        # A server restart never replays an in-flight approval.
        if flow.status == 'running':
            flow.status = 'interrupted'
        return flow

    def wait_for(self, action):
        if action and action not in ACTION_LABELS:
            raise ValueError('Unknown web workflow action')
        self.next_action = action
        if action:
            self.status = 'awaiting_confirmation'
        elif self.status != 'context_blocked':
            self.status = 'completed'
        self.version += 1

    def reset(self):
        version = self.version + 1
        self.__dict__.update(WebWorkflow(version=version).__dict__)
