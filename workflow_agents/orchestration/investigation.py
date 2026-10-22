import copy
import re
from uuid import uuid4
from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.evidence_package import PACKAGE_VERSION
from workflow_agents.common.state import resolve_run_root
from workflow_agents.evidence.evidence_selection import SELECTION_VERSION
from workflow_agents.evidence.saved_result_evidence import load_supplements
from workflow_agents.common.json_io import _read, _write

async def advance_investigation(model_name, run_id, action='prepare', task_id='', question='', complete=None, review_feedback=None, report_complete=None):
    """One explicit action per call. Evidence and verification never call the LLM."""
    if action == 'initial':
        if task_id or question.strip():
            raise ValueError('Initial diagnosis uses a default objective, not a user question/task')
        from workflow_agents.orchestration.investigation_flow import continue_investigation
        return await continue_investigation(model_name, run_id, complete=complete, initial_only=True, report_complete=report_complete)
    if action == 'continue':
        if question.strip():
            raise ValueError('Changed question requires a new task')
        from workflow_agents.orchestration.investigation_flow import continue_investigation
        return await continue_investigation(model_name, run_id, task_id, complete, approve_evidence=True, report_complete=report_complete)
    root = resolve_run_root(model_name, run_id)
    model_name, run_id = root.parents[1].name, root.name
    tasks = root / 'diagnosis_tasks'
    if action == 'prepare':
        from workflow_agents.orchestration.tasks import prepare_task
        directory, state, rows = prepare_task(root, model_name, run_id, question, task_id)
        task_id = state["task_id"]
    else:
        if not re.fullmatch(r'[0-9a-f]{32}', task_id):
            raise ValueError('Invalid task_id')
        directory = tasks / task_id
        state = _read(directory / 'task.json')
        if state['model_name'] != model_name or state['run_id'] != run_id:
            raise ValueError('Task belongs to another run')
        if question and question != state['task_context']['original_question']:
            raise ValueError('Changed question requires a new confirmed task')
        if state.get('evidence_package_version') != PACKAGE_VERSION or state.get('selection_version') != SELECTION_VERSION:
            raise ValueError('旧任务格式或拓扑范围不受支持；请用已有模拟重建统一证据包并创建新诊断任务，无须重新模拟。')
        if state['snapshot_hash'] != sha256_file(directory / 'evidence_snapshot.json'):
            raise ValueError('Evidence snapshot changed; prepare a new task')
        rows = _read(directory / 'evidence_snapshot.json')
        rows = load_supplements(root, directory, state, rows)
        visible_id_set = set(state['visible_ids'])
        visible = [r for r in rows if r['evidence_id'] in visible_id_set]
        if state.get('task_kind') == 'initial_overflow':
            by_id = {r['evidence_id']: r for r in rows}
            manifest_ids = [e['event_id'] for e in state['event_manifest']]
            context_ids = {r['event_id'] for r in rows if r['metric_name'] == 'event_context'}
            if len(set(manifest_ids)) != len(manifest_ids) or set(manifest_ids) != context_ids:
                raise ValueError('Initial event manifest changed')
            for event in state['event_manifest']:
                context = by_id.get(event['fact_evidence_id'], {})
                if (context.get('metric_name') != 'event_context' or context.get('event_id') != event['event_id']
                        or context.get('object_id') != event['node_id']
                        or context.get('value', {}).get('event') != event['facts']):
                    raise ValueError('Initial event facts changed outside the frozen snapshot')
            if by_id.get(state['overflow_overview']['evidence_id']) != state['overflow_overview']:
                raise ValueError('Initial overflow overview changed outside the frozen snapshot')
        if action == 'diagnose':
            from workflow_agents.diagnosis.service import diagnose
            await diagnose(root, directory, state, rows, visible, model_name, run_id, task_id, complete=complete)
        elif action == 'review':
            if state.get('report_revision') != state['revision'] or state['state'] not in {'preliminary_delivered', 'awaiting_evidence_confirmation'}:
                raise ValueError('Review requires a delivered current preliminary report')
            issues = review_feedback.get('issues') if isinstance(review_feedback, dict) else None
            if not isinstance(issues, list) or not issues:
                raise ValueError('Review feedback requires explicit issues')
            for issue in issues:
                if not isinstance(issue, dict) or not isinstance(issue.get('message'), str) or not issue['message'].strip():
                    raise ValueError('Review issue requires a message')
                refs = issue.get('evidence_ids')
                if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in visible_id_set for ref in refs):
                    raise ValueError('Review issues must cite visible evidence')
            filename = f"review_feedback_r{state['revision']}_{uuid4().hex}.json"
            _write(directory / filename, {'revision': state['revision'], 'issues': copy.deepcopy(issues),
                                         'review_scope': 'explicit_review_findings_not_causal_certification'})
            state.update(review_feedback_file=filename, review_feedback_hash=sha256_file(directory / filename),
                         state='revision_requested')
        elif action == 'evidence':
            from workflow_agents.evidence.supplement import supplement
            supplement(root, directory, state, rows, visible, model_name, run_id, task_id)
        elif action == 'verify':
            from workflow_agents.verification.service import verify
            verify(root, directory, state, rows, visible, model_name, run_id, task_id)
        elif action == 'report':
            from workflow_agents.reporting.delivery import deliver
            await deliver(root, directory, state, rows, visible, model_name, run_id, task_id, report_complete=report_complete)
        elif action == 'display_report':
            from workflow_agents.reporting.delivery import deliver_html
            await deliver_html(root, directory, state, rows, visible, model_name, run_id, task_id, report_complete=report_complete)
        elif action == 'status':
            pass
        else:
            raise ValueError('Unknown investigation action')
        if action != 'status':
            state['history'].append({'action': action, 'revision': state['revision'], 'state': state['state']})
    if action != 'status':
        _write(directory / 'task.json', state)
        from workflow_agents.orchestration.investigation_flow import remember_active_task
        remember_active_task(root, state)
    response = dict(state)
    if response.get('report_revision') != response.get('revision') and 'report' in response:
        response['previous_report'] = response.pop('report')
    if response.get('display_report_revision') != response.get('revision'):
        response.pop('display_report_file', None)
        if response.get('display_report', {}).get('status') == 'completed':
            response['previous_display_report'] = response.pop('display_report')
    if (directory / 'diagnosis.json').exists():
        response['diagnosis'] = _read(directory / 'diagnosis.json')
    if (directory / 'verification.json').exists():
        verification = _read(directory / 'verification.json')
        key = 'verification' if verification['revision'] == state['revision'] else 'previous_verification'
        response[key] = verification
    return response
