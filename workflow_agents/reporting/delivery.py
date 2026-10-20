from workflow_agents.common.hashing import sha256_file
from workflow_agents.reporting.internal_report import render_report
from workflow_agents.evidence.saved_result_evidence import evidence_binding
from workflow_agents.common.json_io import _read, _write

async def deliver(root, directory, state, rows, visible, model_name, run_id, task_id, report_complete=None):
    report_base_hash = sha256_file(directory / 'task.json')
    if state['state'] != 'ready_for_report':
        raise ValueError('Report requires current reference verification')
    if sha256_file(directory / 'diagnosis.json') != state['diagnosis_hash'] or sha256_file(directory / 'verification.json') != state['verification_hash']:
        raise ValueError('Diagnosis or verification changed')
    diagnosis = _read(directory / 'diagnosis.json')
    binding = evidence_binding(state)
    if (diagnosis.get('evidence_binding') is not None and diagnosis['evidence_binding'] != binding
            or _read(directory / 'verification.json').get('evidence_binding') not in (None, binding)):
        raise ValueError('Verified evidence binding changed before report')
    from workflow_agents.evidence.requests import evidence_plan, apply_evidence_plan
    apply_evidence_plan(state, evidence_plan(rows, state['visible_ids'], diagnosis['evidence_requests'], root, state.get('supplemental_request_outcomes', [])))
    markdown = render_report(state, diagnosis, len(visible), len(rows))
    report = {'task_id': task_id, 'revision': state['revision'], 'report_kind': 'preliminary',
              'diagnosis_hash': state['diagnosis_hash'], 'verification_hash': state['verification_hash'],
              'evidence_binding': evidence_binding(state),
              'markdown': markdown}
    _write(directory / f"report_r{state['revision']}.json", report)
    _write(directory / 'report.json', report)
    path = directory / f"preliminary_report_r{state['revision']}.md"
    path.write_text(markdown, encoding='utf-8')
    state.update(report=markdown, report_revision=state['revision'], report_file=str(path.resolve()),
                 state='awaiting_evidence_confirmation' if state['pending_evidence_requests'] else 'preliminary_delivered')
    from workflow_agents.reporting.pipeline import attach_display_report
    await attach_display_report(root, directory, state, report_complete)
    if sha256_file(directory / 'task.json') != report_base_hash:
        raise ValueError('Task changed during HTML generation; current task state was preserved')

async def deliver_html(root, directory, state, rows, visible, model_name, run_id, task_id, report_complete=None):
    report_base_hash = sha256_file(directory / 'task.json')
    if state['state'] not in {'preliminary_delivered', 'awaiting_evidence_confirmation'} or state.get('report_revision') != state['revision']:
        raise ValueError('HTML report requires a delivered, verified current diagnosis')
    from workflow_agents.reporting.pipeline import attach_display_report
    await attach_display_report(root, directory, state, report_complete)
    if sha256_file(directory / 'task.json') != report_base_hash:
        raise ValueError('Task changed during HTML generation; current task state was preserved')
