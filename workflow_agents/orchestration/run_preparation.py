"""Run preparation stops at unified evidence; diagnosis is task-scoped only."""
import json
from datetime import datetime

from workflow_agents.evidence.evidence_builder import build_evidence_for_run
from workflow_agents.common.schemas import EVIDENCE_READY, FAILED, NO_SURFACE_INFLOW, RUN_READY
from workflow_agents.common.state import build_state, load_or_initialize_state, save_state


def run_workflow_stage(model_name, run_id, target_stage='', until_stage='', rerun=False):
    if target_stage and until_stage:
        raise ValueError('Use only one of target_stage or until_stage')
    stage = target_stage or until_stage or 'evidence_building'
    if stage != 'evidence_building':
        raise ValueError('运行级诊断/核查已删除；请使用带task_id的任务级调查。')
    root, state = load_or_initialize_state(model_name, run_id)
    if state['state'] == EVIDENCE_READY and not rerun:
        return {'ok': True, 'state': state, 'next_action': 'initial_diagnosis',
                'message': '统一证据已准备；下一步调用DiagnosisAgent(model_name, run_id)交付默认全场冒溢初步诊断，无需用户原问题。'}
    if state['state'] not in {RUN_READY, NO_SURFACE_INFLOW, EVIDENCE_READY} and not rerun:
        return {'ok': False, 'state': state, 'message': '请先完成正式模拟，再构建证据。'}
    try:
        result = build_evidence_for_run(root.parents[1].name, root.name)
    except Exception as exc:
        failure = build_state(root.parents[1].name, root.name, root, state=FAILED, errors=[str(exc)])
        save_state(root, failure)
        return {'ok': False, 'state': failure, 'message': f'Evidence construction failed: {exc}'}
    new = build_state(root.parents[1].name, root.name, root, state=EVIDENCE_READY)
    new['history'] = [*state.get('history', []), {'stage': stage, 'target_state': EVIDENCE_READY,
                      'completed_at': datetime.now().isoformat(timespec='seconds'), 'rerun': bool(rerun)}]
    save_state(root, new)
    return {'ok': True, 'state': new, 'stage_results': [{'stage': stage, 'result': result}],
            'next_action': 'initial_diagnosis', 'awaiting_user_confirmation': True,
            'message': '统一证据构建完成；下一步调用DiagnosisAgent(model_name, run_id)交付默认全场冒溢初步诊断，无需用户原问题。'}


def workflow_result_to_text(result):
    return json.dumps(result, ensure_ascii=False, indent=2)
