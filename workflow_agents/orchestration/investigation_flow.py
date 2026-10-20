import json
import re
from workflow_agents.common.hashing import sha256_file

def remember_active_task(root, state):
    from workflow_agents.common.json_io import _write
    _write(root / 'diagnosis_tasks/active_task.json', {
        'task_id': state['task_id'], 'model_name': state['model_name'],
        'run_id': state['run_id'], 'source_hash': state['source_hash']})

def load_active_task(model_name, run_id, task_id='', kind=None):
    from workflow_agents.orchestration.investigation import resolve_run_root
    root = resolve_run_root(model_name, run_id)
    tasks = root / 'diagnosis_tasks'
    source_hash = sha256_file(root / 'evidence/evidence_package.json')

    def read(identifier, explicit=False):
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise ValueError('Invalid task_id')
        path = tasks / identifier / 'task.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        if (state['task_id'] != identifier or state['model_name'] != root.parents[1].name
                or state['run_id'] != root.name or state['source_hash'] != source_hash):
            if explicit:
                raise ValueError('Task belongs to another run or evidence package')
            return None
        return state if not kind or state.get('task_kind') == kind else None

    if task_id:
        return read(task_id, explicit=True)
    pointer = tasks / 'active_task.json'
    if pointer.exists():
        state = read(json.loads(pointer.read_text(encoding='utf-8'))['task_id'])
        if state:
            return state
    # Compatibility with tasks prepared before the durable pointer existed.
    # Snapshot creation order is stable across retries/report state updates.
    paths = sorted(tasks.glob('*/evidence_snapshot.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in paths:
        state = read(path.parent.name)
        if state:
            return state
    return None

async def diagnose_with_repair(model_name, run_id, task_id, complete=None):
    from workflow_agents.orchestration.investigation import advance_investigation
    # One corrective model response per execution, with explicit validation
    # feedback. This is failure recovery, not a token/context budget.
    for correction in (False, True):
        try:
            return await advance_investigation(model_name, run_id, 'diagnose', task_id, complete=complete)
        except Exception as exc:
            state = load_active_task(model_name, run_id, task_id)
            attempt = state.get('last_attempt', {})
            correctable = attempt.get('status') == 'failed' and attempt.get('phase') in {'parsing', 'validation'}
            if correctable and not correction:
                continue
            return dict(state, execution_error=str(exc),
                        next_action='diagnose', execution_status='failed')

async def continue_investigation(model_name, run_id, task_id='', complete=None,
                                 approve_evidence=False, initial_only=False, report_complete=None):
    from workflow_agents.orchestration.investigation import advance_investigation
    state = load_active_task(model_name, run_id, task_id, 'initial_overflow' if initial_only else None)
    if state is None:
        state = await advance_investigation(model_name, run_id)
    task_id = state['task_id']
    # A delivered legacy task may gain executable saved-result capabilities
    # after an upgrade. Explicit continuation re-plans its original requests;
    # it does not silently discard the report or create a second initial task.
    if state['state'] in {'awaiting_evidence_confirmation', 'preliminary_delivered'} and approve_evidence:
        state = await advance_investigation(model_name, run_id, 'evidence', task_id)
    reference_repair = False
    while True:
        current = state['state']
        if current in {'ready_for_diagnosis', 'revision_requested'}:
            state = await diagnose_with_repair(model_name, run_id, task_id, complete)
            if state.get('execution_error'):
                return state
        elif current == 'ready_for_verification':
            state = await advance_investigation(model_name, run_id, 'verify', task_id)
            if state['state'] == 'revision_requested':
                if reference_repair:
                    return dict(state, execution_status='failed', next_action='diagnose',
                                execution_error='引用核查仍未通过；已保存反馈和原任务，可继续修正。')
                reference_repair = True
        elif current == 'ready_for_report':
            return await advance_investigation(model_name, run_id, 'report', task_id, report_complete=report_complete)
        else:
            # Read through the same binding/hash guards as every mutation.
            return await advance_investigation(model_name, run_id, 'status', task_id)
