from workflow_agents.verification.reference_checks import check_references
from workflow_agents.common.hashing import sha256_file
from workflow_agents.diagnosis.contract import initial_reference_claims
from workflow_agents.evidence.saved_result_evidence import evidence_binding
from workflow_agents.common.json_io import _read, _write

def verify(root, directory, state, rows, visible, model_name, run_id, task_id):
    if state['state'] != 'ready_for_verification':
        raise ValueError('Verification is not the next allowed action')
    if sha256_file(directory / 'diagnosis.json') != state['diagnosis_hash']:
        raise ValueError('Diagnosis changed')
    payload = _read(directory / 'diagnosis.json')
    if payload.get('evidence_binding') is not None and payload['evidence_binding'] != evidence_binding(state):
        raise ValueError('Diagnosis evidence binding changed before verification')
    if state.get('task_kind') == 'initial_overflow':
        payload['claims'] = payload['claims'] + initial_reference_claims(payload)
    result = check_references(payload, visible, model_name, run_id)
    result.update(task_id=task_id, revision=state['revision'], diagnosis_hash=state['diagnosis_hash'], snapshot_hash=state['snapshot_hash'])
    result['evidence_binding'] = evidence_binding(state)
    _write(directory / f"verification_r{state['revision']}.json", result)
    _write(directory / 'verification.json', result)
    state['verification_hash'] = sha256_file(directory / 'verification.json')
    state['state'] = 'ready_for_report' if result['claim_checks'] and all(c['verification_status'] == 'references_verified' for c in result['claim_checks']) else 'revision_requested'
