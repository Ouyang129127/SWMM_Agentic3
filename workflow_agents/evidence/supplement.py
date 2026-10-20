import copy
from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.saved_result_evidence import digest, extract, SavedRequestUnavailable
from workflow_agents.common.json_io import _read, _write
from workflow_agents.diagnosis.service import build_diagnosis_prompt

def supplement(root, directory, state, rows, visible, model_name, run_id, task_id):
    if state['state'] not in {'awaiting_evidence_confirmation', 'preliminary_delivered'}:
        raise ValueError('No pending evidence requests')
    diagnosis = _read(directory / 'diagnosis.json')
    if sha256_file(directory / 'diagnosis.json') != state['diagnosis_hash']:
        raise ValueError('Diagnosis changed')
    from workflow_agents.evidence.requests import evidence_plan, apply_evidence_plan
    plan = evidence_plan(rows, state['visible_ids'], diagnosis['evidence_requests'], root, state.get('supplemental_request_outcomes', []))
    observations, added = [], set()
    for item in plan:
        if item.get('requires_extraction'):
            # Earlier requests in the same approved batch may already
            # have produced one or all of these object/event scopes.
            item = evidence_plan(rows, state['visible_ids'], [item['request']], root,
                                 state.get('supplemental_request_outcomes', []))[0]
        if item.get('requires_extraction'):
            try:
                supplement = extract(root, item)
            except SavedRequestUnavailable as exc:
                outcome = {'request_signature': item['request_signature'], 'status': exc.status,
                           'reason': str(exc), 'source_hashes': exc.source_hashes}
                state.setdefault('supplemental_request_outcomes', []).append(outcome)
                item.update(outcome, requires_extraction=False)
                observations.append(copy.deepcopy(item))
                continue
            filename = 'supplement_' + digest(supplement) + '.json'
            path = directory / filename
            if path.exists():
                if _read(path) != supplement:
                    raise ValueError('Supplement collision')
            else:
                _write(path, supplement)
            state.setdefault('supplements', []).append({'file': filename, 'sha256': sha256_file(path)})
            rows.extend(supplement['evidence_rows'])
            item['matching_evidence_ids'] = sorted(set(item['matching_evidence_ids']) | {r['evidence_id'] for r in supplement['evidence_rows']})
            item['new_evidence_ids'] = sorted(set(item['matching_evidence_ids']) - set(state['visible_ids']))
            item['extraction_status'] = {r['evidence_id']: r['value']['status'] for r in supplement['evidence_rows']}
            item['requires_extraction'] = False
        added.update(item['matching_evidence_ids'])
        observations.append(copy.deepcopy(item))
    new_ids = added - set(state['visible_ids'])
    state['visible_ids'] = sorted(set(state['visible_ids']) | added)
    state['observations'].extend(observations)
    build_diagnosis_prompt(state, rows, diagnosis)
    apply_evidence_plan(state, evidence_plan(rows, state['visible_ids'], diagnosis['evidence_requests'], root, state.get('supplemental_request_outcomes', [])))
    state.update(no_progress=0 if new_ids else state.get('no_progress', 0) + 1,
                 last_evidence_added_ids=sorted(new_ids),
                 state='ready_for_diagnosis' if new_ids else 'preliminary_delivered')
