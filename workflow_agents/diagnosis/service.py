"""Human-stepped LLM investigation over an immutable, run-bound evidence snapshot.

This layer does not simulate, execute generated code, or infer missing measurements.
The unified evidence package is the only preparation input.
"""
import copy
import json
import time
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.evidence_selection import lookup_evidence
from workflow_agents.diagnosis.contract import check_claim_binding, validate_initial
from workflow_agents.evidence.saved_result_evidence import validate_request, evidence_binding

def serialize_prompt(prompt):
    return json.dumps(prompt, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


from workflow_agents.diagnosis.prompts import SYSTEM_PROMPT


def _lookup(rows, request):
    return lookup_evidence(rows, request)


def build_diagnosis_prompt(state, rows, previous=None, feedback=None):
    visible_ids = set(state['visible_ids'])
    visible = [r for r in rows if r['evidence_id'] in visible_ids]
    bindings = {(r['source_model'], r['run_id']) for r in visible}
    if len(bindings) > 1:
        raise ValueError('Visible evidence mixes model/run bindings')
    # Send complete selected records directly. Copy only to isolate the frozen
    # evidence from consumers; do not encode, factor or restore its structure.
    prompt = {'task': state['task_context'], 'visible_evidence': copy.deepcopy(visible),
              'evidence_selection': state.get('evidence_selection', {}),
              'evidence_overview': state['evidence_overview'],
              'inventory': dict(sorted(Counter(r['metric_name'] for r in rows).items())),
              'observations': state['observations'], 'previous_diagnosis': previous,
              'verification_feedback': feedback}
    if state.get('numeric_precision'):
        prompt['numeric_precision'] = copy.deepcopy(state['numeric_precision'])
    prompt['task'] = dict(prompt['task'], **{k: state[k] for k in ('model_name', 'run_id', 'task_id') if k in state})
    if 'snapshot_hash' in state:
        prompt['evidence_binding'] = evidence_binding(state)
    prompt['diagnosis_contract_version'] = 'joint_mechanisms_v1'
    if state.get('task_kind') == 'initial_overflow':
        prompt['event_manifest'] = copy.deepcopy(state['event_manifest'])
    return prompt


def _validate_response(result, rows, state=None):
    if not isinstance(result, dict):
        raise ValueError('Diagnosis response must be an object')
    for field in ('claims', 'evidence_requests', 'question_assessments', 'mechanism_assessments'):
        if not isinstance(result.get(field), list):
            raise ValueError(f'Missing list: {field}')
    for field, key, expected in [('question_assessments', 'question_id', {f'Q{i}' for i in range(1, 5)}),
                                  ('mechanism_assessments', 'mechanism_id', {f'M{i}' for i in range(1, 7)})]:
        if any(not isinstance(x, dict) for x in result[field]) or len(result[field]) != len(expected) or {x.get(key) for x in result[field]} != expected:
            raise ValueError(f'All distinct entries required: {field}')
        for item in result[field]:
            statuses = {'answered', 'needs_evidence', 'not_requested'} if key == 'question_id' else {
                'supported', 'candidate', 'not_applicable', 'not_investigated', 'needs_evidence'}
            if item.get('status') not in statuses or not isinstance(item.get('reason'), str) or not item['reason'].strip():
                raise ValueError(f'Invalid assessment: {field}')
            if key == 'mechanism_id' and item['status'] in {'supported', 'not_applicable'}:
                refs = item.get('evidence_ids')
                visible_ids = {r['evidence_id'] for r in rows}
                if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in visible_ids for ref in refs):
                    missing = [ref for ref in refs if isinstance(ref, str) and ref not in visible_ids] if isinstance(refs, list) else []
                    raise ValueError(f"{field}/{item[key]}: mechanism applicability/support must cite visible evidence; invalid IDs={missing}, references={refs}")
    for index, claim in enumerate(result['claims']):
        if not isinstance(claim, dict):
            raise ValueError('Claim must be an object')
        for key in ('claim_text', 'engineering_reason', 'alternatives', 'scope'):
            if not isinstance(claim.get(key), str) or not claim[key].strip():
                raise ValueError(f'Missing claim field: {key}')
        if claim.get('question_id') not in {f'Q{i}' for i in range(1, 5)} or claim.get('claim_kind') not in {'fact', 'clue', 'process_explanation'}:
            raise ValueError('Invalid claim question/kind')
        refs = claim.get('evidence_ids')
        if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
            raise ValueError('evidence_ids must be strings')
        check_claim_binding(claim, rows, f'claims[{index}]')
    for req in result['evidence_requests']:
        if not isinstance(req, dict):
            raise ValueError('Evidence request must be an object')
        if req.get('tool') not in {'evidence_lookup', 'saved_result_extract', 'unavailable'} or not str(req.get('reason', '')).strip():
            raise ValueError('Invalid evidence request')
        if req.get('tool') != 'evidence_lookup' and (req.get('tool') == 'saved_result_extract' or any(k in req for k in ('object_ids', 'event_ids', 'time_start', 'time_end', 'sample_interval_seconds'))):
            validate_request(req)
        else:
            _lookup([], req)  # Validate parameters; never execute generated code.
    if not isinstance(result.get('stop_reason'), str) or not result['stop_reason'].strip():
        raise ValueError('Missing stop_reason')
    if state and state.get('task_kind') == 'initial_overflow':
        validate_initial(result, state['event_manifest'], rows, require_joint=True)


async def configured_completion(prompt, audit=None):
    # Lazy import: offline tests and preparation do not load keys or call a model.
    from autogen_core.models import SystemMessage, UserMessage
    import workflow_agents.common.llm as llm
    deepseek_flash = llm.deepseek_flash
    if audit:
        audit(model=getattr(llm, 'deepseek_model', None), provider=getattr(llm, 'deepseek_base_url', None))
    response = await deepseek_flash.create(
        [SystemMessage(content=SYSTEM_PROMPT), UserMessage(content=serialize_prompt(prompt), source='user')],
        json_output=True)
    if audit:
        usage = getattr(response, 'usage', None)
        raw = response.content
        if not isinstance(raw, str):
            raw = [item.model_dump(mode='json') if hasattr(item, 'model_dump') else str(item) for item in raw] if isinstance(raw, list) else raw
        audit(phase='parsing', raw_response=raw,
              finish_reason=getattr(response, 'finish_reason', None),
              usage={'prompt_tokens': getattr(usage, 'prompt_tokens', None),
                     'completion_tokens': getattr(usage, 'completion_tokens', None)})
    if not isinstance(response.content, str):
        raise ValueError('Expected JSON text from DiagnosisAgent')
    return json.loads(response.content)


def _dry_result(state):
    ref = state['overflow_overview']['evidence_id']
    return {'question_assessments': [{'question_id': f'Q{i}', 'status': 'answered',
                'reason': '经事件目录与全场节点指标一致性核对，本场保存结果中未识别冒溢。'} for i in range(1, 5)],
            'mechanism_assessments': [{'mechanism_id': f'M{i}', 'status': 'not_investigated',
                'reason': '本轮未识别冒溢事件，不推断其他水力或地表风险。'} for i in range(1, 7)],
            'claims': [], 'event_assessments': [], 'evidence_requests': [],
            'run_overview': {'text': '本场保存结果中未识别冒溢。判断基于事件目录和完整节点统计的一致性；不代表绝对安全或地表无积水。', 'evidence_ids': [ref]},
            'stop_reason': '完成无冒溢保存结果的初步说明；其他风险未在本轮分析。'}


from workflow_agents.common.json_io import _read, _write

async def diagnose(root, directory, state, rows, visible, model_name, run_id, task_id, complete=None):
    if state['state'] not in {'ready_for_diagnosis', 'revision_requested'}:
        raise ValueError('Diagnosis is not the next allowed action')
    previous = _read(directory / 'diagnosis.json') if (directory / 'diagnosis.json').exists() else None
    feedback = _read(directory / 'verification.json') if state['state'] == 'revision_requested' else None
    if state.get('review_feedback_file'):
        review_path = directory / state['review_feedback_file']
        if sha256_file(review_path) != state['review_feedback_hash']:
            raise ValueError('Review feedback changed')
        feedback = {'reference_verification': feedback, 'review_feedback': _read(review_path)}
    prompt = build_diagnosis_prompt(state, rows, previous, feedback)
    failed = state.get('last_attempt', {})
    if failed.get('status') == 'failed' and failed.get('phase') in {'parsing', 'validation'}:
        failed_audit = _read(directory / failed['audit_file'])
        if failed_audit['snapshot_hash'] != state['snapshot_hash']:
            raise ValueError('Failed attempt belongs to another evidence snapshot')
        prompt['validation_feedback'] = {
            'error': failed.get('validation_error'),
            'previous_response': failed_audit.get('parsed_response', failed_audit.get('raw_response')),
            'instruction': '依据本次可见证据修正以下校验错误，重新返回完整JSON；不得放宽校验或编造对象／引用。'}
    attempt_id = uuid4().hex
    attempt_path = directory / f'diagnosis_attempt_{attempt_id}.json'
    audit_data = {'attempt_id': attempt_id, 'task_id': task_id, 'status': 'started',
                  'phase': 'completion', 'started_at': datetime.now(timezone.utc).isoformat(),
                  'request_characters': len(SYSTEM_PROMPT) + len(serialize_prompt(prompt)),
                  'snapshot_hash': state['snapshot_hash'], 'visible_ids': list(state['visible_ids']),
                  'evidence_binding': evidence_binding(state),
                  'response_source': 'injected_completion' if complete else 'configured_model',
                  'network_call_started': False}
    completion_metadata = getattr(complete, 'diagnosis_audit_metadata', None)
    if isinstance(completion_metadata, dict):
        audit_data.update(copy.deepcopy(completion_metadata))
    started = time.monotonic()
    def audit(**values):
        audit_data.update(values)
        _write(attempt_path, audit_data)
    audit()
    try:
        if state.get('task_kind') == 'initial_overflow' and not state['event_manifest']:
            audit(response_source='deterministic_no_overflow')
            result = _dry_result(state)
        elif complete:
            result = await complete(prompt)
        else:
            audit(network_call_started=True)
            result = await configured_completion(prompt, audit=audit)
        audit(phase='validation', parsed_response=result)
        _validate_response(result, visible, state)
    except Exception as exc:
        failure = {'error_type': type(exc).__name__, 'phase': audit_data['phase'],
                   'error_message': str(exc)}
        if audit_data['phase'] in {'validation', 'parsing'}:
            failure['validation_error'] = str(exc)
        status_code = getattr(exc, 'status_code', None)
        if status_code is not None:
            failure['http_status'] = status_code
        audit(status='failed', elapsed_seconds=round(time.monotonic() - started, 3), **failure)
        state['last_attempt'] = {'attempt_id': attempt_id, 'status': 'failed',
                                 'audit_file': attempt_path.name, **failure}
        state['history'].append({'action': 'diagnose_failed', 'revision': state['revision'], **state['last_attempt']})
        _write(directory / 'task.json', state)
        from workflow_agents.orchestration.investigation_flow import remember_active_task
        remember_active_task(root, state)
        raise
    audit(status='completed', phase='validated', elapsed_seconds=round(time.monotonic() - started, 3))
    state['last_attempt'] = {'attempt_id': attempt_id, 'status': 'completed', 'audit_file': attempt_path.name}
    revision = state['revision'] + 1
    for index, claim in enumerate(result['claims'], 1):
        claim['claim_id'] = f'D{revision}:C{index}'
    result.update(model_name=model_name, run_id=run_id, task_id=task_id, revision=revision,
                  diagnosis_mode='llm_investigation', task_context=state['task_context'],
                  diagnosis_contract_version='joint_mechanisms_v1', evidence_binding=evidence_binding(state))
    _write(directory / f'diagnosis_r{revision}.json', result)
    _write(directory / 'diagnosis.json', result)
    state.update(revision=revision, diagnosis_hash=sha256_file(directory / 'diagnosis.json'),
                 pending_evidence_requests=bool(result['evidence_requests']), state='ready_for_verification')
    state.pop('review_feedback_file', None)
    state.pop('review_feedback_hash', None)
