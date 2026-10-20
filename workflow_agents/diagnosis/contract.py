"""Cited object/event bindings and the first, overflow-centred diagnosis contract."""
import copy
import hashlib
import json
import math
from collections import Counter

from workflow_agents.evidence.evidence_selection import EVENT_GROUPS
from workflow_agents.evidence.evidence_precision import round_value, uses_decimal3

INITIAL_OBJECTIVE = '以冒溢为中心，给出全场概览，对每个冒溢节点／事件分析发生过程、可能机制和证据缺口；先交付初步诊断，随后再补证。'
MECHANISMS = {
    'M1': '来水集中／叠加', 'M2': '局部输水约束', 'M3': '下游顶托',
    'M4': '地面／井口与水头决定位置', 'M5': '设施运行', 'M6': '初始蓄水／可用调蓄',
}
STATUSES = {'supported', 'candidate', 'not_applicable', 'not_investigated', 'needs_evidence'}
STATUS_LABELS = {'supported': '有证据支持', 'candidate': '候选机制', 'not_applicable': '不适用',
                 'not_investigated': '尚未调查', 'needs_evidence': '需要补证'}


def row_subjects(row):
    """Only known schema fields declare subjects; arbitrary nested keys do not."""
    subjects = {(row.get('object_type'), row.get('object_id'))}
    # Run-level statements may aggregate cited node/event records from that
    # same bound run; the envelope owner is not the only subject of a summary.
    if row.get('run_id'):
        subjects.add(('run', row['run_id']))
    value = row.get('value')
    if not isinstance(value, dict):
        return subjects
    if row.get('metric_name') == 'local_structure':
        for key, kind in [('nodes', 'node'), ('links', 'link')]:
            objects = value.get(key, {})
            if isinstance(objects, dict):
                subjects.update((kind, obj) for obj in objects)
    elif row.get('metric_name') in {'local_process_series', 'storage_volume_process', 'fullness_time_series',
                                    'max_fullness', 'ponding_depth_time_series'}:
        for sample in value.get('series', []):
            if isinstance(sample, dict):
                for key, kind in [('nodes', 'node'), ('links', 'link'), ('cells', 'cell')]:
                    if isinstance(sample.get(key), dict):
                        subjects.update((kind, obj) for obj in sample[key])
    elif row.get('metric_name') == 'direct_source_composition':
        for source in value.get('sources', []):
            if isinstance(source, dict) and isinstance(source.get('source_id'), str) and source['source_id'] != 'aggregate_lateral':
                subjects.add(('link', source['source_id']))
    return subjects


def check_claim_binding(claim, rows, label='Claim'):
    subject = (claim.get('object_type'), claim.get('object_id'))
    by_id = {r['evidence_id']: r for r in rows}
    known = set().union(*(row_subjects(r) for r in rows)) if rows else set()
    if subject not in known:
        raise ValueError(f'{label}: Claim object is outside the supplied evidence: {subject}')
    event_id = claim.get('event_id')
    eligible = [r for r in rows if not event_id or r.get('event_id') == event_id]
    if event_id and not any(subject in row_subjects(r) for r in eligible):
        raise ValueError(f'{label}: Unknown claim event for object: {event_id}, {subject}')
    # Missing IDs remain a reference-verification failure. A known ID cannot be
    # used to license an unrelated object or an unrelated event.
    refs = claim.get('evidence_ids', [])
    cited = [by_id[ref] for ref in refs if ref in by_id]
    if cited and not any(subject in row_subjects(r) and (not event_id or r.get('event_id') == event_id)
                         for r in cited):
        raise ValueError(f'{label}: cited evidence does not bind object/event: {subject}, {event_id}')


def event_manifest(package):
    """Validate the index, then freeze exact event facts from event_context."""
    rows = package['evidence_rows']
    by_id = {r['evidence_id']: r for r in rows}
    events, seen = [], set()
    for index in package.get('packages', []):
        event_id, node = index.get('event_id'), index.get('node_id')
        if not isinstance(event_id, str) or not isinstance(node, str) or event_id in seen:
            raise ValueError('Invalid or duplicate event index')
        seen.add(event_id)
        refs = index.get('evidence_ids', [])
        if not isinstance(refs, list) or len(refs) != len(EVENT_GROUPS) or len(set(refs)) != len(refs):
            raise ValueError(f'Event index requires six distinct groups: {event_id}')
        bundle = {}
        for ref in refs:
            row = by_id.get(ref)
            if not row or row.get('event_id') != event_id or (row.get('object_type'), row.get('object_id')) != ('node', node):
                raise ValueError(f'Event index binding mismatch: {event_id}, {ref}')
            if row['metric_name'] in bundle:
                raise ValueError(f'Duplicate event group: {event_id}')
            bundle[row['metric_name']] = row
        if set(bundle) != EVENT_GROUPS:
            raise ValueError(f'Event groups mismatch: {event_id}')
        context = bundle['event_context']
        facts = context.get('value', {}).get('event', {})
        if facts.get('event_id') != event_id or facts.get('node_id') != node:
            raise ValueError(f'Event facts binding mismatch: {event_id}')
        if facts.get('model_name') != package['model_name'] or facts.get('run_id') != package['run_id']:
            raise ValueError(f'Event facts belong to another run: {event_id}')
        for field in ('estimated_volume_m3', 'duration_minutes', 'peak_flooding_Ls'):
            number = facts.get(field)
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                raise ValueError(f'Invalid event fact: {event_id}, {field}')
        events.append({'event_id': event_id, 'node_id': node, 'facts': copy.deepcopy(facts),
                       'fact_evidence_id': context['evidence_id'],
                       'evidence_ids': list(refs)})
    if seen != {r['event_id'] for r in rows if r.get('event_id')}:
        raise ValueError('Event index does not cover all event records')
    overview = package['overview']
    if overview.get('event_count') != len(events) or overview.get('overflow_node_count') != len({e['node_id'] for e in events}):
        raise ValueError('Event index and overview counts disagree')
    # Cross-check the saved whole-run event counts and positive volumes. Empty
    # directories alone do not certify a dry run.
    counts = [r for r in rows if r['metric_name'] == 'overflow_event_count' and r['object_type'] == 'node' and not r.get('event_id')]
    volumes = [r for r in rows if r['metric_name'] == 'total_flooding_volume' and r['object_type'] == 'node' and not r.get('event_id')]
    node_count = overview.get('node_count')
    flows = [r for r in rows if r['metric_name'] == 'max_flooding_flow' and r['object_type'] == 'node' and not r.get('event_id')]
    node_sets = [{r['object_id'] for r in group} for group in (counts, volumes, flows)]
    if (any(len(group) != node_count for group in (counts, volumes, flows))
            or any(len(nodes) != node_count or nodes != node_sets[0] for nodes in node_sets)):
        raise ValueError('Whole-run node metrics do not cover the declared nodes')
    expected = Counter(e['node_id'] for e in events)
    for row in counts:
        if row['value'] != expected[row['object_id']]:
            raise ValueError(f"Event count disagrees with node metric: {row['object_id']}")
    for row in volumes:
        if type(row['value']) not in (int, float) or not math.isfinite(row['value']) or row['value'] < 0:
            raise ValueError('Invalid whole-run flooding volume')
        if row['value'] > 0 and row['object_id'] not in expected:
            raise ValueError(f"Event presence disagrees with node flooding volume: {row['object_id']}")
    tolerance = package.get('event_detection', {}).get('positive_tolerance_Ls', 1e-9)
    for row in flows:
        if type(row['value']) not in (int, float) or not math.isfinite(row['value']) or row['value'] < 0:
            raise ValueError('Invalid whole-run flooding peak')
        if (row['value'] > tolerance) != (row['object_id'] in expected):
            raise ValueError(f"Event presence disagrees with node flooding peak: {row['object_id']}")
    return sorted(events, key=lambda e: (e['facts']['start'], e['event_id']))


def overview_row(package, events):
    facts = [e['facts'] for e in events]
    value = {'event_count': len(events), 'overflow_node_count': len({e['node_id'] for e in events}),
             'overflow_nodes': sorted({e['node_id'] for e in events}),
             'identified_event_volume_m3': math.fsum(e['estimated_volume_m3'] for e in facts),
             'first_start': min((e['start'] for e in facts), default=None),
             'last_end': max((e['end'] for e in facts), default=None),
             'left_censored_events': [e['event_id'] for e in facts if e.get('left_censored')],
             'right_censored_events': [e['event_id'] for e in facts if e.get('right_censored')],
             'status': 'events_identified' if events else 'no_overflow_in_saved_results',
             'interpretation': 'Saved-sample event integrals, not a solver water budget or complete causal attribution.'}
    if uses_decimal3(package):
        value = round_value(value)
    derivation = {'source': 'evidence_snapshot.json',
                  'event_evidence_ids': [e['fact_evidence_id'] for e in events],
                  'node_metric_evidence_ids': [r['evidence_id'] for r in package['evidence_rows']
                      if r['metric_name'] in {'overflow_event_count', 'total_flooding_volume'} and not r.get('event_id')]}
    row = {'source_model': package['model_name'], 'run_id': package['run_id'],
           'object_type': 'run', 'object_id': package['run_id'], 'event_id': '',
           'metric_name': 'overflow_event_overview', 'unit': 'structured', 'value': value,
           'calculation_method': 'validated_saved_event_catalogue_v1', 'derivation': derivation}
    row['evidence_id'] = 'SUM_EVT_' + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:24]
    return row


def validate_initial(result, manifest, rows, require_joint=False):
    by_id = {r['evidence_id']: r for r in rows}
    overview = result.get('run_overview')
    if not isinstance(overview, dict) or not isinstance(overview.get('text'), str) or not overview['text'].strip():
        raise ValueError('Initial diagnosis requires run_overview.text')
    refs = overview.get('evidence_ids')
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in by_id for ref in refs):
        raise ValueError('Initial run overview must cite visible evidence')
    assessments = result.get('event_assessments')
    expected = {e['event_id']: e for e in manifest}
    if (not isinstance(assessments, list) or any(not isinstance(e, dict) for e in assessments)
            or len(assessments) != len(expected) or {e.get('event_id') for e in assessments} != set(expected)):
        raise ValueError('Initial diagnosis must assess every event exactly once: ' + ', '.join(expected))
    for item in assessments:
        event_id = item['event_id']
        if item.get('node_id') != expected[event_id]['node_id']:
            raise ValueError(f'Event assessment node mismatch: {event_id}')
        for field in ('process_explanation', 'alternatives'):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f'Event assessment missing {field}: {event_id}')
        gaps = item.get('evidence_gaps')
        if not isinstance(gaps, list) or any(not isinstance(x, str) or not x.strip() for x in gaps):
            raise ValueError(f'Event assessment requires explicit evidence_gaps: {event_id}')
        refs = item.get('evidence_ids')
        if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in by_id for ref in refs)
                or not any(by_id[ref].get('event_id') == event_id for ref in refs)):
            raise ValueError(f'Event assessment must cite its event: {event_id}')
        mechanisms = item.get('mechanism_assessments')
        if (not isinstance(mechanisms, list) or any(not isinstance(m, dict) for m in mechanisms)
                or len(mechanisms) != 6 or {m.get('mechanism_id') for m in mechanisms} != set(MECHANISMS)):
            raise ValueError(f'Event requires all six mechanism statuses: {event_id}')
        for m in mechanisms:
            if m.get('status') not in STATUSES or not isinstance(m.get('reason'), str) or not m['reason'].strip():
                raise ValueError(f'Invalid event mechanism: {event_id}, {m.get("mechanism_id")}')
            mrefs = m.get('evidence_ids', [])
            if not isinstance(mrefs, list) or any(not isinstance(ref, str) or ref not in by_id for ref in mrefs):
                raise ValueError(f'Invalid event mechanism references: {event_id}')
            if m['status'] in {'supported', 'not_applicable'} and (not mrefs or not any(by_id[ref].get('event_id') == event_id for ref in mrefs)):
                raise ValueError(f'Event mechanism support/applicability must cite its event: {event_id}, {m["mechanism_id"]}')
        joint = item.get('joint_assessment')
        # Legacy reports remain readable; every new model response must supply
        # the synthesis explicitly. Never synthesize causation from status flags.
        if joint is None and not require_joint:
            continue
        if not isinstance(joint, dict) or joint.get('status') not in {'supported', 'candidate', 'needs_evidence'}:
            raise ValueError(f'Event requires joint_assessment with an explicit evidence status: {event_id}')
        for field in ('conclusion', 'interaction_explanation', 'limitations'):
            if not isinstance(joint.get(field), str) or not joint[field].strip():
                raise ValueError(f'Joint assessment requires {field}: {event_id}')
        selected = joint.get('mechanism_ids')
        if not isinstance(selected, list) or any(not isinstance(k, str) or k not in MECHANISMS for k in selected) or len(set(selected)) != len(selected):
            raise ValueError(f'Invalid joint mechanism IDs: {event_id}')
        statuses = {m['mechanism_id']: m['status'] for m in mechanisms}
        if joint['status'] == 'supported' and (not selected or any(statuses[k] != 'supported' for k in selected)):
            raise ValueError(f'Supported joint assessment can only include supported event mechanisms: {event_id}')
        if any(statuses[k] in {'not_applicable', 'not_investigated'} for k in selected):
            raise ValueError(f'Joint assessment includes an inapplicable/uninvestigated mechanism: {event_id}')
        jrefs = joint.get('evidence_ids')
        if (not isinstance(jrefs, list) or not jrefs or any(not isinstance(ref, str) or ref not in by_id for ref in jrefs)
                or any(by_id[ref].get('event_id') not in {'', None, event_id} for ref in jrefs)
                or not any(by_id[ref].get('event_id') == event_id for ref in jrefs)):
            raise ValueError(f'Joint assessment must cite its own visible event evidence: {event_id}')


def initial_reference_claims(result):
    """Trace prose sections as well as ordinary claims; no causal certification."""
    items = []
    if 'run_overview' in result:
        items.append({'claim_id': 'OVERVIEW', 'evidence_ids': result['run_overview']['evidence_ids']})
    for item in result.get('event_assessments', []):
        items.append({'claim_id': 'EVENT:' + item['event_id'], 'evidence_ids': item['evidence_ids']})
        if item.get('joint_assessment'):
            items.append({'claim_id': 'EVENT:' + item['event_id'] + ':JOINT',
                          'evidence_ids': item['joint_assessment']['evidence_ids']})
        for m in item['mechanism_assessments']:
            if m.get('evidence_ids'):
                items.append({'claim_id': 'EVENT:' + item['event_id'] + ':' + m['mechanism_id'], 'evidence_ids': m['evidence_ids']})
    return items


def apply_reviewed_reference_corrections(result, corrections, rows, visible_ids):
    """Explicit reviewed typo corrections only; never fuzzy-match or relax guards."""
    by_id = {r['evidence_id']: r for r in rows if r['evidence_id'] in set(visible_ids)}
    mapping = {}
    for correction in corrections:
        old, new = correction.get('invalid_id'), correction.get('evidence_id')
        if not isinstance(old, str) or old in by_id or old in mapping or new not in by_id:
            raise ValueError('Reference correction must map a distinct invalid ID to a visible ID')
        row = by_id[new]
        if any(correction.get(key) != row.get(key) for key in ('object_type', 'object_id', 'event_id', 'metric_name')):
            raise ValueError('Reference correction does not match its declared evidence locator')
        if not isinstance(correction.get('reason'), str) or not correction['reason'].strip():
            raise ValueError('Reference correction requires a review reason')
        mapping[old] = new
    corrected, occurrences = copy.deepcopy(result), Counter()
    def visit(value):
        if isinstance(value, dict):
            for key, content in value.items():
                if key == 'evidence_ids' and isinstance(content, list):
                    for i, ref in enumerate(content):
                        if isinstance(ref, str) and ref in mapping:
                            content[i] = mapping[ref]
                            occurrences[ref] += 1
                else:
                    visit(content)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(corrected)
    if set(occurrences) != set(mapping):
        raise ValueError('A reviewed invalid ID was not present in reference fields')
    return corrected, dict(occurrences)
