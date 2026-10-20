import copy
from uuid import uuid4
from workflow_agents.verification.reference_checks import check_references
from workflow_agents.common.hashing import sha256_file
from workflow_agents.diagnosis.task_scope import select_scope
from workflow_agents.evidence.evidence_package import PACKAGE_VERSION, load_package
from workflow_agents.evidence.evidence_selection import SELECTION_VERSION, select_initial_evidence
from workflow_agents.diagnosis.contract import INITIAL_OBJECTIVE, event_manifest, overview_row
from workflow_agents.common.json_io import _write

def prepare_task(root, model_name, run_id, question, task_id=''):
    tasks = root / "diagnosis_tasks"
    if task_id:
        raise ValueError('Preparation requires no existing task_id')
    package = load_package(root, verify_sources=True)
    rows = list(package['evidence_rows'])
    initial = not question.strip()
    scope = ({'scope': 'global', 'node_id': None, 'event_id': None,
              'task_kind': 'initial_overflow', 'original_question': None, 'objective': INITIAL_OBJECTIVE}
             if initial else select_scope(question, {r['object_id'] for r in rows if r['object_type'] == 'node'}))
    manifest = event_manifest(package) if initial else None
    if scope['event_id'] and not any(r.get('event_id') == scope['event_id'] for r in rows):
        raise ValueError('Event not present in current evidence')
    task_id = uuid4().hex
    directory = tasks / task_id
    directory.mkdir(parents=True, exist_ok=False)
    derived, visible_ids, selection = select_initial_evidence(rows, scope, model_name, run_id)
    if initial:
        overview = overview_row(package, manifest)
        derived.append(overview)
        visible_ids.append(overview['evidence_id'])
    rows.extend(derived)
    check_references({'model_name': model_name, 'run_id': run_id, 'claims': []}, rows, model_name, run_id)
    _write(directory / 'evidence_snapshot.json', rows)
    state = {'task_id': task_id, 'model_name': model_name, 'run_id': run_id,
             'task_kind': 'initial_overflow' if initial else 'followup',
             'task_context': scope, 'state': 'ready_for_diagnosis', 'revision': 0,
             'snapshot_hash': sha256_file(directory / 'evidence_snapshot.json'),
             'source_hash': sha256_file(root / 'evidence/evidence_package.json'),
             'evidence_package_version': PACKAGE_VERSION,
             'selection_version': SELECTION_VERSION, 'evidence_selection': selection,
             'visible_ids': visible_ids,
             'observations': [{'request': 'task_scope_selection', 'selected_count': len(visible_ids),
                               'snapshot_count': len(rows), 'selection_policy': SELECTION_VERSION}],
             'evidence_overview': package['overview'],
             'history': [{'action': 'prepare'}]}
    if package.get('numeric_precision'):
        state['numeric_precision'] = copy.deepcopy(package['numeric_precision'])
    if initial:
        state.update(event_manifest=manifest, overflow_overview=overview)
    return directory, state, rows
