"""Task-scoped evidence selection, never top-N selection or transport encoding.

All source rows stay local. Global scalar statistics have their own IDs and
reproducible selectors; citing one does not mark its source rows as read.
"""
import hashlib
import json
import math
from collections import defaultdict


SELECTION_VERSION = 'task_evidence_v3'
EVENT_GROUPS = frozenset({
    'event_context', 'local_structure', 'local_process_series',
    'direct_source_composition', 'facilities_and_storage', 'surface_association',
})
SUMMARY_KEYS = ('object_type', 'metric_name', 'unit', 'calculation_method', 'threshold')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _ordered(rows):
    return sorted(rows, key=lambda r: (r.get('object_type', ''), r.get('object_id', ''),
                                      r.get('event_id', ''), r['metric_name'], r['evidence_id']))


def _global_summaries(rows, model_name, run_id):
    groups = defaultdict(list)
    for row in rows:
        # Event facts are delivered directly with their event, not mixed with
        # whole-run indicators. Structured packages are not numerical scalars.
        if not row.get('event_id') and not isinstance(row.get('value'), (dict, list)):
            groups[tuple(row.get(key, '') for key in SUMMARY_KEYS)].append(row)
    summaries = []
    for group, sources in sorted(groups.items()):
        sources = _ordered(sources)
        numbers = [_number(r.get('value')) for r in sources]
        valid = [n for n in numbers if n is not None]
        selector = dict(zip(SUMMARY_KEYS, group))
        selector['event_id'] = ''
        # Exact source rows can be recovered from the frozen local snapshot.
        # The digest binds values as well as IDs; do not ship thousands of IDs.
        lineage = {'source': 'evidence_snapshot.json', 'selector': selector,
                   'source_rows_sha256': _digest(sources)}
        value = {'status': 'complete' if len(valid) == len(sources) else 'partial',
                 'source_metric': selector['metric_name'], 'source_object_type': selector['object_type'],
                 'source_row_count': len(sources),
                 'object_count': len({r['object_id'] for r in sources}),
                 'valid_count': len(valid), 'missing_or_nonfinite_count': len(sources) - len(valid),
                 'minimum': min(valid) if valid else None, 'maximum': max(valid) if valid else None,
                 'positive_value_count': sum(n > 0 for n in valid),
                 'zero_value_count': sum(n == 0 for n in valid),
                 'negative_value_count': sum(n < 0 for n in valid),
                 'interpretation': 'Counts describe this metric, not event counts or mechanism support.'}
        # Existing scalar timestamps sometimes denote an object's peak and
        # sometimes its observation interval. Preserve every distinct window
        # as a separate stratum, in a compact table rather than hundreds of
        # repeated evidence-row envelopes. Never sum or imply simultaneity.
        windows = defaultdict(list)
        for source in sources:
            windows[(source.get('time_start', ''), source.get('time_end', ''))].append(source)
        strata = []
        for (start, end), members in sorted(windows.items()):
            values = [_number(r.get('value')) for r in members]
            finite = [n for n in values if n is not None]
            strata.append([start, end, len(members), len({r['object_id'] for r in members}),
                           len(finite), len(members) - len(finite),
                           min(finite) if finite else None, max(finite) if finite else None,
                           sum(n > 0 for n in finite), sum(n == 0 for n in finite), sum(n < 0 for n in finite)])
        value['by_source_window'] = {
            'columns': ['start', 'end', 'rows', 'objects', 'valid', 'missing', 'minimum', 'maximum',
                        'positive', 'zero', 'negative'], 'records': strata}
        value['time_interpretation'] = (
            'Overall statistics compare saved scalar values across objects and their own source windows; '
            'not a simultaneous state or a common-event integral. Per-window statistics are retained below.')
        row = {'source_model': model_name, 'run_id': run_id, 'object_type': 'run',
               'object_id': run_id, 'event_id': '',
               'metric_name': f"summary:{selector['object_type']}:{selector['metric_name']}",
               'unit': selector['unit'],
               'calculation_method': 'grouped_scalar_min_max_counts_v1',
               'source_calculation_method': selector['calculation_method'],
               'source_threshold': selector['threshold'],
               'value': value, 'derivation': lineage}
        row['evidence_id'] = 'SUM_' + _digest(row)[:24]
        summaries.append(row)
    return summaries


def select_initial_evidence(rows, scope, model_name, run_id):
    """Return new summary rows, selected IDs, and explicit coverage/missingness.

    Selection is by task objects, event identity and hydraulic adjacency, never
    row count/order/rank. All selected records are supplied in full.
    """
    node, event = scope.get('node_id'), scope.get('event_id')
    event_rows = [r for r in rows if r.get('event_id')
                  and (not node or (r['object_type'] == 'node' and r['object_id'] == node))
                  and (not event or r['event_id'] == event)]
    events = sorted({r['event_id'] for r in event_rows})
    related_nodes, related_links = ({node} if node else set()), set()
    for row in event_rows:
        if row['metric_name'] == 'local_structure' and isinstance(row.get('value'), dict):
            related_nodes.update(row['value'].get('nodes', {}))
            related_links.update(row['value'].get('links', {}))

    selected = list(event_rows)
    derived = []
    if node:
        for row in rows:
            if row.get('event_id'):
                continue
            if (row['object_type'] == 'node' and row['object_id'] in related_nodes
                    or row['object_type'] == 'link' and row['object_id'] in related_links):
                selected.append(row)
    else:
        derived = _global_summaries(rows, model_name, run_id)
        selected.extend(derived)
    # Common run context, including rainfall, is supplied for every scope.
    selected.extend(r for r in rows if not r.get('event_id')
                    and r['object_type'] == 'run' and isinstance(r.get('value'), dict))

    coverage = []
    for event_id in events:
        groups = {r['metric_name'] for r in event_rows if r['event_id'] == event_id}
        coverage.append({'event_id': event_id, 'missing_groups': sorted(EVENT_GROUPS - groups)})
    manifest = {
        'policy': SELECTION_VERSION, 'selection_basis': 'task_scope_and_event_hydraulic_context',
        'scope': scope['scope'], 'event_coverage': coverage,
        'related_nodes': sorted(related_nodes), 'related_links': sorted(related_links),
        'background_scope': 'Whole-run statistics remain background; do not substitute them for event values.',
        'limitations': ['Local packages cover only their saved process window; missing values remain unknown.',
                        'Topology corridors stop at recorded boundaries; they do not guarantee all causes are covered.',
                        'Global statistics are not individual-object or causal diagnosis.'],
    }
    if not events:
        manifest['limitations'].append(
            'No event evidence in the selected snapshot scope. Check event-count evidence/overview; '
            'absence of a package alone does not prove absence of flooding.')
    if node and not related_links:
        manifest['limitations'].append('No local connection package available; adjacent processes are not supplied.')
    return derived, sorted({r['evidence_id'] for r in selected}), manifest


def lookup_evidence(rows, request):
    allowed = {'tool', 'object_type', 'object_id', 'metric_name', 'event_id',
               'evidence_id', 'offset', 'reason'}
    if set(request) - allowed:
        raise ValueError('Unsupported evidence lookup parameters')
    offset = request.get('offset', 0)
    if type(offset) is not int or offset != 0:
        raise ValueError('Row offsets are no longer supported; query objects, events or metrics')
    filters = {key: request[key] for key in
               ('object_type', 'object_id', 'metric_name', 'event_id', 'evidence_id') if request.get(key)}
    if any(not isinstance(value, str) for value in filters.values()):
        raise ValueError('Evidence query filters must be strings')
    if request.get('tool') != 'unavailable' and not any(key in filters for key in ('object_id', 'event_id', 'metric_name', 'evidence_id')):
        raise ValueError('Evidence queries require an object, event, metric or evidence ID, not a whole-library scan')
    selected = _ordered([r for r in rows if all(r.get(k) == v for k, v in filters.items())])
    return {'rows': selected, 'total_matches': len(selected), 'selection': filters,
            'complete_match_set': True}
