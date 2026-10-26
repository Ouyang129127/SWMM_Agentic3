"""Event-local evidence extraction from saved results only.

No simulation, hydraulic cause classification, or LLM access occurs here.
"""
import csv
import hashlib
import json
import math
from datetime import datetime

from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.topology_scope import SCOPE_POLICY, build_topology_scope


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def parse_run_input(path):
    if not path.exists():
        return {'available': False, 'nodes': {}, 'links': {}, 'options': {}, 'sections': {}}
    sections, section = {}, ''
    for raw in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        line = raw.split(';', 1)[0].strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].upper()
            sections.setdefault(section, [])
        elif line and section:
            sections[section].append(line.split())
    options = {p[0].upper(): ' '.join(p[1:]) for p in sections.get('OPTIONS', []) if len(p) >= 2}
    nodes, links = {}, {}
    for kind in ('JUNCTIONS', 'OUTFALLS', 'STORAGE', 'DIVIDERS'):
        for p in sections.get(kind, []):
            if len(p) < 2:
                raise ValueError(f'Malformed {kind} record')
            if p[0] in nodes:
                raise ValueError('Duplicate node ID')
            nodes[p[0]] = {'node_type': kind, 'invert_elevation': _number(p[1]), 'input_fields': p[1:]}
            if kind == 'OUTFALLS':
                nodes[p[0]]['boundary_type'] = p[2] if len(p) > 2 else None
            if kind == 'JUNCTIONS':
                nodes[p[0]].update(max_depth=_number(p[2]) if len(p) > 2 else None,
                                  initial_depth=_number(p[3]) if len(p) > 3 else None,
                                  surcharge_depth=_number(p[4]) if len(p) > 4 else None,
                                  ponded_area=_number(p[5]) if len(p) > 5 else None)
                # Preserve raw settings; do not infer ground/spill elevation for every node type.
    cross_sections = {p[0]: p[1:] for p in sections.get('XSECTIONS', []) if p}
    for kind in ('CONDUITS', 'PUMPS', 'ORIFICES', 'WEIRS', 'OUTLETS'):
        for p in sections.get(kind, []):
            if len(p) < 3 or p[0] in links or p[1] == p[2]:
                raise ValueError(f'Invalid/duplicate link in {kind}')
            links[p[0]] = {'link_type': kind, 'from_node': p[1], 'to_node': p[2],
                           'input_fields': p[3:], 'cross_section_fields': cross_sections.get(p[0]),
                           'offset_convention': options.get('LINK_OFFSETS', 'DEPTH')}
            if kind == 'CONDUITS':
                links[p[0]].update({name: _number(p[i]) if len(p) > i else None for i, name in
                                   [(3, 'length'), (4, 'roughness'), (5, 'inlet_offset'), (6, 'outlet_offset')]})
            cross = cross_sections.get(p[0])
            if cross:
                links[p[0]]['cross_section'] = {'shape': cross[0], 'geometry_fields': cross[1:]}
    return {'available': True, 'nodes': nodes, 'links': links, 'options': options, 'sections': sections}


def read_saved_binary(path, node_ids):
    """Read native output; never instantiate Simulation. Native SI/LPS only initially."""
    if not path.exists():
        return {}, {'status': 'missing', 'reason': 'model.out not found'}
    from pyswmm import Output
    from swmm.toolkit.shared_enum import NodeAttribute
    attrs = {'head_m': NodeAttribute.HYDRAULIC_HEAD, 'total_inflow_Ls': NodeAttribute.TOTAL_INFLOW,
             'lateral_inflow_Ls': NodeAttribute.LATERAL_INFLOW}
    result = {}
    with Output(str(path)) as output:
        if output.units.get('system') != 'SI' or output.units.get('flow') != 'LPS':
            return {}, {'status': 'unsupported_units', 'units': output.units}
        for node_id in sorted(node_ids):
            if node_id not in output.nodes:
                continue
            data = {}
            for name, attr in attrs.items():
                for time, value in output.node_series(node_id, attr).items():
                    data.setdefault(time.isoformat(sep=' '), {})[name] = _number(value)
            result[node_id] = data
    return result, {'status': 'available', 'units': {'head': 'm', 'flow': 'L/s'}}


def _table(path, id_column, fields):
    result = {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            time = datetime.fromisoformat(f"{row['date']} {row['time']}").isoformat(sep=' ')
            values = result.setdefault(row[id_column], {})
            if time in values:
                raise ValueError(f'Duplicate saved time: {row[id_column]} {time}')
            values[time] = {field: _number(row.get(field)) for field in fields}
    return result


def _composition(series, start, end, paths, peak_time):
    window = [r for r in series if start <= r['time'] <= end]
    volumes = {key: 0.0 for key in paths}
    outgoing = dict(volumes)
    covered_seconds = dict(volumes)
    for a, b in zip(window, window[1:]):
        seconds = (datetime.fromisoformat(b['time']) - datetime.fromisoformat(a['time'])).total_seconds()
        for key in paths:
            value = a['signed_towards_node_Ls'].get(key)
            if value is not None:
                covered_seconds[key] += seconds
                volumes[key] += max(value, 0) * seconds / 1000
                outgoing[key] += max(-value, 0) * seconds / 1000
    duration = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    complete = duration > 0 and all(abs(value - duration) < 1e-6 for value in covered_seconds.values())
    peak = next((r for r in window if r['time'] == peak_time), None)
    peak_complete = peak is not None and all(peak['signed_towards_node_Ls'].get(k) is not None for k in paths)
    denominator = sum(volumes.values()) if complete else None
    peak_denominator = sum(max(0, peak['signed_towards_node_Ls'][k]) for k in paths) if peak_complete else None
    return {'status': 'complete' if complete else 'partial', 'method': 'left_sample_rectangle',
            'event_window': {'start': start, 'end': end}, 'volume_denominator_m3': denominator,
            'peak_time': peak_time, 'peak_denominator_Ls': peak_denominator,
            'share_meaning': 'direct entering composition, not flood contribution',
            'total_inflow_role': 'comparison only; never added as a source',
            'sources': [{'source_id': k, 'entering_volume_over_covered_intervals_m3': volumes[k],
                         'outgoing_volume_over_covered_intervals_m3': outgoing[k],
                         'covered_seconds': covered_seconds[k],
                         'event_volume_share': volumes[k] / denominator if denominator else None,
                         'peak_entering_Ls': max(0, peak['signed_towards_node_Ls'][k]) if peak_complete else None,
                         'peak_flow_share': max(0, peak['signed_towards_node_Ls'][k]) / peak_denominator if peak_denominator else None}
                        for k in paths]}


def build_event_evidence(root, event_catalog, binary_reader=read_saved_binary):
    """Build all recorded overflow-node episodes, not a top-N case selection."""
    paths = {name: root / value for name, value in {
        'input': 'swmm/model_with_event.inp', 'binary': 'swmm/model.out',
        'nodes': 'swmm/nodes.tsv', 'links': 'swmm/links.tsv'}.items()}
    before = {name: sha256_file(path) for name, path in paths.items() if path.exists()}
    model = parse_run_input(paths['input'])
    nodes = _table(paths['nodes'], 'node_id', ['depth_m', 'flooding_Ls'])
    links = _table(paths['links'], 'link_id', ['flow_Ls', 'depth_m'])
    targets = {e['node_id'] for e in event_catalog['events']}
    scopes = {target: build_topology_scope(model, target) for target in sorted(targets)}
    required = {node for scope in scopes.values() for node in scope['node_roles']}
    native, native_status = binary_reader(paths['binary'], required)
    # Exported columns are labelled L/s and m; refuse to use them when the native
    # run declares a different unit system rather than silently mislabelling them.
    compatible = model['options'].get('FLOW_UNITS', '').upper() == 'LPS'
    rows, packages = [], []
    def add(node_id, event_id, metric, value):
        key = f'{event_id}|{metric}'
        eid = 'FP_' + hashlib.sha256(key.encode()).hexdigest()[:20]
        rows.append({'evidence_id': eid, 'run_id': root.name, 'source_model': root.parents[1].name,
                     'object_type': 'node', 'object_id': node_id, 'event_id': event_id,
                     'metric_name': metric, 'value': value, 'unit': 'structured',
                     'source_file': 'evidence/evidence_package.json'})
        return eid
    for event in event_catalog['events']:
        target, event_id = event['node_id'], event['event_id']
        scope = scopes[target]
        connected = {k: model['links'][k] for k in scope['direct_links']}
        scoped_links = {k: model['links'][k] for k in scope['link_roles']}
        local_nodes = set(scope['node_roles'])
        times = sorted(nodes.get(target, {}))
        if event['start'] not in times or event['end'] not in times:
            raise ValueError('Event boundaries are absent from saved node records')
        first, last = times.index(event['start']), times.index(event['end'])
        context_times = times[max(0, first - 1):min(len(times), last + 2)]
        series = []
        for time in context_times:
            node_values, signed, link_values = {}, {}, {}
            for node in sorted(local_nodes):
                saved = nodes.get(node, {}).get(time, {})
                values = dict(native.get(node, {}).get(time, {}))
                for field in ('head_m', 'total_inflow_Ls', 'lateral_inflow_Ls'):
                    values.setdefault(field, None)
                values['depth_m'] = saved.get('depth_m') if compatible else None
                values['flooding_Ls'] = saved.get('flooding_Ls') if compatible else None
                if values.get('head_m') is None and compatible:
                    invert = model['nodes'].get(node, {}).get('invert_elevation')
                    if invert is not None and values['depth_m'] is not None:
                        values['head_m'] = invert + values['depth_m']
                        values['head_method'] = 'invert_plus_saved_depth'
                elif values.get('head_m') is not None:
                    values['head_method'] = 'native_output'
                node_values[node] = values
            for key, link in scoped_links.items():
                saved = links.get(key, {}).get(time, {})
                flow = saved.get('flow_Ls') if compatible else None
                if key in connected:
                    signed[key] = None if flow is None else flow * (1 if link['to_node'] == target else -1)
                link_values[key] = {'signed_model_flow_Ls': flow,
                                    'depth_m': saved.get('depth_m') if compatible else None}
                upstream_head = node_values[link['from_node']].get('head_m')
                downstream_head = node_values[link['to_node']].get('head_m')
                link_values[key]['from_minus_to_head_m'] = upstream_head - downstream_head if upstream_head is not None and downstream_head is not None else None
            signed['aggregate_lateral'] = node_values[target].get('lateral_inflow_Ls')
            all_present = model['available'] and compatible and all(v is not None for v in signed.values())
            positive_sum = sum(max(v, 0) for v in signed.values()) if all_present else None
            total = node_values[target].get('total_inflow_Ls')
            series.append({'time': time, 'nodes': node_values, 'links': link_values,
                           'signed_towards_node_Ls': signed, 'positive_source_sum_Ls': positive_sum,
                           'source_sum_minus_total_Ls': positive_sum - total if positive_sum is not None and total is not None else None})
        composition = _composition(series, event['start'], event['end'], list(connected) + ['aggregate_lateral'], event['peak_time'])
        if not model['available'] or not compatible:
            composition = {'status': 'unavailable', 'reason': 'Missing run input or unsupported units'}
        local_facilities = {k: v for k, v in scoped_links.items() if v['link_type'] != 'CONDUITS'}
        # Only direct outlets to this event's target node; no catchment properties
        # or upstream subcatchment routing are added to the initial evidence.
        direct_subcatchments = [
            {'subcatchment_id': fields[0], 'outlet_node_id': target}
            for fields in model['sections'].get('SUBCATCHMENTS', [])
            if len(fields) >= 3 and fields[2] == target
        ]
        evidence_ids = []
        values = {
            'event_context': {'event': event, 'process_window': {'start': context_times[0], 'end': context_times[-1]},
                              'window_policy': 'one saved neighbor before/after; may require longer investigation'},
            'local_structure': {'available': model['available'], 'links': scoped_links, 'topology_scope': scope,
                                'nodes': {n: model['nodes'].get(n) for n in sorted(local_nodes)},
                                'direct_subcatchments': direct_subcatchments,
                                'options': model['options'], 'units_verified_LPS': compatible},
            'local_process_series': {'series': series, 'native_status': native_status, 'missing_values': 'null, not zero',
                                     'scope_policy': SCOPE_POLICY,
                                     'direct_source_scope': 'Only target incident links and target aggregate lateral; never sum corridor flows as new sources'},
            'direct_source_composition': composition,
            'facilities_and_storage': {'local_facilities': local_facilities,
                'facility_presence_status': 'checked_from_run_input' if model['available'] else 'unknown',
                'controls_present': bool(model['sections'].get('CONTROLS')) if model['available'] else None,
                'facility_actions': 'not_extracted', 'storage_volume_process': 'not_extracted',
                'initial_state_source': 'input_fields in local_structure, not inferred from first report sample'},
            'surface_association': {'status': 'not_built', 'reason': 'No source attribution inferred from nearest cell'},
        }
        for metric, value in values.items():
            evidence_ids.append(add(target, event_id, metric, value))
        packages.append({'node_id': target, 'event_id': event_id, 'evidence_ids': evidence_ids})
    after = {name: sha256_file(path) for name, path in paths.items() if path.exists()}
    if before != after:
        raise ValueError('Run artifacts changed during evidence extraction')
    return {'schema_name': 'event_local_evidence', 'schema_version': '0.1',
            'model_name': root.parents[1].name, 'run_id': root.name,
            'source_hashes': before, 'event_catalog_hash': hashlib.sha256(json.dumps(event_catalog, sort_keys=True).encode()).hexdigest(),
            'overview': {'event_count': len(packages), 'overflow_node_count': len(targets),
                         'event_scope_policy': SCOPE_POLICY,
                         'node_count': len(nodes), 'link_count': len(links),
                         'storage_node_count': sum(v['node_type'] == 'STORAGE' for v in model['nodes'].values()) if model['available'] else None,
                         'facility_counts': {kind: sum(v['link_type'] == kind for v in model['links'].values())
                                             for kind in ('PUMPS', 'ORIFICES', 'WEIRS', 'OUTLETS')},
                         'facility_inventory_status': 'available' if model['available'] else 'unknown',
                         'native_status': native_status},
            'packages': packages, 'evidence_rows': rows}
