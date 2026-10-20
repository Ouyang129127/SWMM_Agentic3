"""Typed, reproducible supplementation from a completed run. Never simulates.

Planning checks capabilities; extraction happens only after the evidence action.
Original packages/snapshots remain immutable. Each supplement has content IDs,
source hashes, an explicit time/object scope and method/interpretation limits.
"""
import csv
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from workflow_agents.evidence.event_packages import _number, _table, parse_run_input
from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.evidence_precision import PRECISION_POLICY, round_evidence_row, uses_decimal3

VERSION = 'saved_result_extract_v1'
METRICS = {'fullness_time_series', 'max_fullness', 'storage_volume_process',
           'local_process_series', 'ponding_depth_time_series'}
FIELDS = {'tool', 'object_type', 'object_id', 'object_ids', 'event_id', 'event_ids',
          'metric_name', 'time_start', 'time_end', 'sample_interval_seconds', 'reason'}


class SavedRequestUnavailable(ValueError):
    """A legitimate request the saved dataset cannot satisfy, not corruption."""
    def __init__(self, reason, status, source_hashes):
        super().__init__(reason)
        self.status, self.source_hashes = status, source_hashes


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def validate_request(request):
    if set(request) - FIELDS:
        raise ValueError('Unsupported saved-result request parameters: ' + ', '.join(sorted(set(request) - FIELDS)))
    for key in ('object_ids', 'event_ids'):
        if key in request and (not isinstance(request[key], list) or not request[key]
                               or any(not isinstance(x, str) or not x.strip() for x in request[key])):
            raise ValueError(f'{key} must be a nonempty array of identifiers')
    for key in ('object_type', 'object_id', 'event_id', 'metric_name', 'time_start', 'time_end', 'reason'):
        if key in request and not isinstance(request[key], str):
            raise ValueError(f'{key} must be a string')
    if bool(request.get('time_start')) != bool(request.get('time_end')):
        raise ValueError('Both time_start and time_end are required for an explicit window')
    if request.get('time_start'):
        a, b = (datetime.fromisoformat(request[k]) for k in ('time_start', 'time_end'))
        if a.tzinfo or b.tzinfo or a > b:
            raise ValueError('Saved-result windows require ordered local simulation timestamps')
    if 'sample_interval_seconds' in request:
        value = request['sample_interval_seconds']
        if isinstance(value, bool) or not isinstance(value, (int, float)) or _number(value) is None or value <= 0:
            raise ValueError('sample_interval_seconds must be finite and positive')


def _ids(request, singular, plural):
    # Older diagnoses used semicolon-separated events. Normalize identifiers,
    # never interpret the prose reason as executable code or filtering rules.
    values = list(request.get(plural, []))
    if request.get(singular):
        values.extend(re.split(r'[;,；，]', request[singular]))
    return sorted({x.strip() for x in values if x.strip()})


def scope_signature(resolved, scope):
    return digest({'method': resolved['method'], 'metric_name': resolved['metric_name'],
                   'source_files': resolved['source_files'],
                   'scope': {k: v for k, v in scope.items() if k != 'window_policy'}})


def _row_scope_signature(row):
    derivation = row.get('derivation', {})
    if derivation.get('scope_signature'):
        return derivation['scope_signature']
    # Compatible with the first supplement batch: its records bound the whole
    # request. Do not rewrite those immutable files merely to change indexing.
    resolved = derivation.get('resolved_request')
    scope = row.get('value', {}).get('resolved_scope') if isinstance(row.get('value'), dict) else None
    if row.get('calculation_method') == VERSION and resolved and scope:
        return scope_signature(resolved, scope)
    return None


def _mapping_path(root):
    summary_path = root / 'summary.json'
    if not summary_path.exists():
        return None
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    if summary.get('model_name') != root.parents[1].name or summary.get('run_id') != root.name:
        raise ValueError('Saved summary belongs to another model/run')
    # A current mapping is a geometric auxiliary input, not a frozen simulation
    # state. Constrain it to this model and hash the exact file actually used.
    static = Path(summary.get('static_model', str(root.parents[1] / 'static'))).resolve()
    if not static.is_relative_to(root.parents[1].resolve()):
        raise ValueError('Surface mapping is outside the bound model directory')
    return static / 'node_to_cell_mapping.csv'


def plan_request(root, rows, request):
    """No time-series computation or artifact writes in capability planning."""
    metric = request.get('metric_name')
    if metric not in METRICS:
        return {'status': 'unavailable', 'reason': '尚无该变量的保存结果提取方法；需明确调查方法，不能直接等同于必须重跑。'}
    validate_request(request)
    model = parse_run_input(root / 'swmm/model_with_event.inp')
    required = ['swmm/model_with_event.inp']
    if metric == 'ponding_depth_time_series':
        required += ['summary.json', 'ca2d/surface_depth.tsv']
        mapping = _mapping_path(root)
        if mapping is None or not mapping.is_file():
            return {'status': 'unavailable', 'reason': '缺少本模型节点—地表网格映射，不能生成空间关联。'}
        required.append(str(mapping))
    elif metric in {'storage_volume_process', 'local_process_series'}:
        required += ['swmm/model.out']
    else:
        required += ['swmm/links.tsv']
    if any(not (Path(p) if Path(p).is_absolute() else root / p).is_file() for p in required):
        return {'status': 'unavailable', 'reason': '缺少该指标必需的已保存结果文件。'}
    if model['options'].get('FLOW_UNITS', '').upper() != 'LPS':
        return {'status': 'unavailable', 'reason': '本提取器目前只支持 SI/LPS，不能把其他单位标为 m、L/s。'}
    if request.get('sample_interval_seconds'):
        if metric == 'ponding_depth_time_series':
            summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
            minutes = summary.get('simulation_timing', {}).get('ca2d_save_interval_minutes')
            step = _number(minutes)
            step = step * 60 if step is not None else None
        else:
            parts = model['options'].get('REPORT_STEP', '').split(':')
            step = sum(float(p) * factor for p, factor in zip(parts, (3600, 60, 1))) if len(parts) == 3 else None
        if step is not None and request['sample_interval_seconds'] < step:
            return {'status': 'unavailable', 'reason': f'要求 {request["sample_interval_seconds"]:g} s 分辨率，但保存步长为 {step:g} s；不能通过插值补成观测，需重新导出或模拟。',
                    'next_investigation': 'finer_saved_output_required', 'saved_step_seconds': step}
    events = {r['event_id']: r for r in rows if r['metric_name'] == 'event_context'}
    event_ids = _ids(request, 'event_id', 'event_ids')
    if not event_ids and not _ids(request, 'object_id', 'object_ids'):
        return {'status': 'unavailable', 'reason': '需要指定对象或事件，不能猜测补证范围。'}
    if set(event_ids) - set(events):
        return {'status': 'no_match', 'reason': '请求事件不在本次冻结事件目录中。'}
    targets = _ids(request, 'object_id', 'object_ids')
    kind = request.get('object_type') or ('link' if metric in {'fullness_time_series', 'max_fullness'} else 'node')
    scopes = []
    for event_id in event_ids or ['']:
        event = events[event_id]['value']['event'] if event_id else None
        objects = targets or [events[event_id]['object_id']]
        actual_kind = kind
        if not targets:
            actual_kind = 'node'
        if metric in {'fullness_time_series', 'max_fullness'}:
            if actual_kind == 'node':
                unknown = set(objects) - set(model['nodes'])
                links = [k for k, v in model['links'].items() if v['from_node'] in objects or v['to_node'] in objects]
            elif actual_kind == 'link':
                unknown, links = set(objects) - set(model['links']), objects
            else:
                return {'status': 'unavailable', 'reason': '满度请求只接受管段，或节点的直接连接管段。'}
            selected = {'nodes': [], 'links': sorted(links), 'cells': []}
        elif metric == 'ponding_depth_time_series':
            if actual_kind not in {'node', 'cell'}:
                return {'status': 'unavailable', 'reason': '地表水深请求只接受节点映射网格或明确网格编号。'}
            unknown = set(objects) - set(model['nodes']) if actual_kind == 'node' else set()
            selected = {'nodes': sorted(objects) if actual_kind == 'node' else [], 'links': [],
                        'cells': sorted(objects) if actual_kind == 'cell' else []}
        else:
            if actual_kind == 'node':
                unknown, nodes = set(objects) - set(model['nodes']), objects
                links = [k for k, v in model['links'].items() if v['from_node'] in nodes or v['to_node'] in nodes]
                nodes = sorted(set(nodes) | {n for k in links for n in
                               (model['links'][k]['from_node'], model['links'][k]['to_node'])})
            elif actual_kind == 'link':
                unknown, links = set(objects) - set(model['links']), objects
                nodes = sorted({n for k in links if k in model['links'] for n in
                                (model['links'][k]['from_node'], model['links'][k]['to_node'])})
            else:
                return {'status': 'unavailable', 'reason': '管网过程请求只接受节点或管段。'}
            selected = {'nodes': sorted(nodes), 'links': sorted(links), 'cells': []}
        if unknown or not any(selected.values()):
            return {'status': 'no_match', 'reason': '请求对象不存在或没有直接连接管段。'}
        start, end = request.get('time_start'), request.get('time_end')
        if start:
            start = datetime.fromisoformat(start).isoformat(sep=' ')
            end = datetime.fromisoformat(end).isoformat(sep=' ')
        if not start and event:
            # A declared engineering context, NOT a token/row budget. Explicit
            # windows override it and return every saved sample without a cap.
            step = event.get('saved_step_seconds') or 0
            if metric == 'ponding_depth_time_series':
                summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
                minutes = _number(summary.get('simulation_timing', {}).get('ca2d_save_interval_minutes'))
                if minutes is not None:
                    step = minutes * 60
            start = (datetime.fromisoformat(event['start']) - timedelta(seconds=step)).isoformat(sep=' ')
            end = (datetime.fromisoformat(event['end']) + timedelta(seconds=step)).isoformat(sep=' ')
            policy = 'event_plus_one_saved_step_each_side'
        else:
            policy = 'explicit_window' if start else 'entire_saved_run'
        scopes.append(dict(selected, event_id=event_id, time_start=start, time_end=end,
                           window_policy=policy, anchor_node=event['node_id'] if event else '',
                           sample_interval_seconds=request.get('sample_interval_seconds')))
    resolved = {'method': VERSION, 'metric_name': metric, 'scopes': scopes,
                'source_files': sorted(required),
                'scope_note': '只按结构化参数执行；reason为调查目的。缺省事件时窗前后各扩展一个相应结果保存步长（地表采用地表保存步长）；未指定管段时取事件节点直接连接管段，过程取其两端节点。'}
    signature = digest(resolved)
    wanted = {scope_signature(resolved, scope) for scope in scopes}
    existing = [r['evidence_id'] for r in rows if _row_scope_signature(r) in wanted]
    missing = sorted(wanted - {_row_scope_signature(r) for r in rows})
    return {'status': 'available', 'execution': 'saved_result_extract', 'resolved_request': resolved,
            'request_signature': signature, 'matching_evidence_ids': existing,
            'new_evidence_ids': [], 'requires_extraction': bool(missing),
            'missing_scope_signatures': missing}


def _hash_sources(root, names):
    return {name: sha256_file(Path(name) if Path(name).is_absolute() else root / name) for name in names}


def _native(path, nodes, links):
    from pyswmm import Output
    from swmm.toolkit.shared_enum import NodeAttribute, LinkAttribute
    node_attrs = {'head_m': NodeAttribute.HYDRAULIC_HEAD, 'total_inflow_Ls': NodeAttribute.TOTAL_INFLOW,
                  'lateral_inflow_Ls': NodeAttribute.LATERAL_INFLOW, 'depth_m': NodeAttribute.INVERT_DEPTH,
                  'flooding_Ls': NodeAttribute.FLOODING_LOSSES, 'node_volume_m3': NodeAttribute.PONDED_VOLUME}
    link_attrs = {'signed_model_flow_Ls': LinkAttribute.FLOW_RATE, 'depth_m': LinkAttribute.FLOW_DEPTH,
                  'link_volume_m3': LinkAttribute.FLOW_VOLUME}
    result = {'nodes': {}, 'links': {}}
    with Output(str(path)) as output:
        if output.units.get('system') != 'SI' or output.units.get('flow') != 'LPS':
            raise ValueError('Native output is not SI/LPS; extraction refused')
        for group, ids, inventory, attrs, reader in [('nodes', nodes, output.nodes, node_attrs, output.node_series),
                                                   ('links', links, output.links, link_attrs, output.link_series)]:
            for obj in ids:
                if obj not in inventory:
                    raise ValueError(f'Requested object missing in native output: {obj}')
                samples = result[group].setdefault(obj, {})
                for name, attr in attrs.items():
                    for time, value in reader(obj, attr).items():
                        samples.setdefault(time.isoformat(sep=' '), {})[name] = _number(value)
        result['metadata'] = {'units': dict(output.units), 'solver_version': output.version,
                              'volume_semantics': 'Native NODE_VOLUME and LINK_VOLUME in m3, not free storage capacity. Toolkit PONDED_VOLUME is the node output slot name; it is not CA2D surface ponding.',
                              'definition_source': 'https://github.com/USEPA/Stormwater-Management-Model/blob/develop/src/solver/node.c'}
    return result


def _surface(root, mapping_path, scopes):
    mapped = {}
    with Path(mapping_path).open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            if row['node_id'] in mapped:
                raise ValueError('Ambiguous node-to-cell mapping')
            mapped[row['node_id']] = row
    wanted = {cell for scope in scopes for cell in scope['cells']}
    for scope in scopes:
        for node in scope['nodes']:
            if node not in mapped:
                raise ValueError(f'Missing surface mapping for node: {node}')
            wanted.add(str(mapped[node]['smid']))
    data = {key: {} for key in wanted}
    with (root / 'ca2d/surface_depth.tsv').open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f, delimiter='\t'):
            cell = str(row['Smid'])
            if cell in data:
                time = datetime.fromisoformat(f"{row['Date']} {row['Time']}").isoformat(sep=' ')
                if time in data[cell]:
                    raise ValueError('Duplicate saved surface timestamp')
                data[cell][time] = {'depth_m': _number(row['Depth'])}
    return mapped, data


def extract(root, plan):
    """Compute all requested saved samples; invalid/incomplete data stay explicit."""
    from workflow_agents.evidence.evidence_package import load_package
    package = load_package(root, verify_sources=True)
    resolved, signature = plan['resolved_request'], plan['request_signature']
    before = _hash_sources(root, resolved['source_files'])
    model = parse_run_input(root / 'swmm/model_with_event.inp')
    metric, scopes = resolved['metric_name'], resolved['scopes']
    nodes = sorted({n for scope in scopes for n in scope['nodes']})
    links = sorted({n for scope in scopes for n in scope['links']})
    native, mapped = None, {}
    if metric in {'storage_volume_process', 'local_process_series'}:
        native = _native(root / 'swmm/model.out', nodes, links)
        node_data, link_data = native['nodes'], native['links']
    elif metric == 'ponding_depth_time_series':
        mapped, cell_data = _surface(root, _mapping_path(root), scopes)
    else:
        link_data = _table(root / 'swmm/links.tsv', 'link_id', ['depth_m', 'flow_Ls'])
    rows = []
    for scope in scopes:
        if scope_signature(resolved, scope) not in plan['missing_scope_signatures']:
            continue
        cells = sorted(set(scope['cells']) | {str(mapped[n]['smid']) for n in scope['nodes']} ) if metric == 'ponding_depth_time_series' else []
        data = cell_data if cells else node_data if scope['nodes'] and native else link_data
        requested_objects = cells or scope['nodes'] or scope['links']
        times = sorted({t for obj in requested_objects for t in data.get(obj, {})})
        intervals = sorted({(datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
                            for a, b in zip(times, times[1:])})
        requested_interval = scope.get('sample_interval_seconds')
        if requested_interval and (not intervals or requested_interval < max(intervals)):
            # Finer reports cannot be recovered by interpolation. Keep the
            # original request unresolved rather than falsely fulfilling it.
            raise SavedRequestUnavailable('Requested time resolution is finer than saved samples; rerun/export required', 'unavailable', before)
        selected_times = [t for t in times if (not scope['time_start'] or t >= scope['time_start'])
                          and (not scope['time_end'] or t <= scope['time_end'])]
        if not selected_times:
            raise SavedRequestUnavailable('请求对象／时窗内没有保存样本；请调整请求，不能据此判断水深或流量为零。', 'no_match', before)
        series, missing = [], []
        for t in selected_times:
            sample = {'time': t, 'nodes': {}, 'links': {}, 'cells': {}}
            if cells:
                sample['cells'] = {c: dict(cell_data[c].get(t, {'depth_m': None})) for c in cells}
            else:
                for node in scope['nodes']:
                    sample['nodes'][node] = dict(node_data.get(node, {}).get(t, {}))
                for link in scope['links']:
                    values = dict(link_data.get(link, {}).get(t, {}))
                    cross = model['links'][link].get('cross_section_fields') or []
                    # Circular Geom1 is the full depth. No guessed height for
                    # custom/irregular cross sections or special facilities.
                    height = _number(cross[1]) if len(cross) > 1 and cross[0].upper() == 'CIRCULAR' else None
                    depth = values.get('depth_m')
                    values['depth_reference_m'] = height
                    values['depth_reference_ratio'] = max(0, depth) / height if height and height > 0 and depth is not None else None
                    if native:
                        ends = model['links'][link]
                        heads = [node_data.get(ends[k], {}).get(t, {}).get('head_m') for k in ('from_node', 'to_node')]
                        values['from_minus_to_head_m'] = heads[0] - heads[1] if all(x is not None for x in heads) else None
                    sample['links'][link] = values
            for group, objects in sample.items():
                if isinstance(objects, dict):
                    for obj, values in objects.items():
                        if not values or any(v is None for v in values.values()):
                            missing.append({'time': t, 'object_type': group, 'object_id': obj})
            series.append(sample)
        limits = ['只使用实际保存样本，不插值、不重跑。保存流量与体积差不能充当求解器完整质量守恒核查。',
                  '满度为圆管水深/直径代理量，不直接证明输水失效、因果或致涝贡献率。',
                  '节点与连接管段体积仅逐对象列出，不相加宣称独立控制体积或可用调蓄量。']
        if cells:
            limits += ['节点—网格关联是几何映射，不是冒溢水源追踪。映射为本次提取使用的当前静态辅助文件，未声称为运行时冻结映射。',
                       '本项目当前 SWMM→CA2D 单向传递；地表水深不能证明回流进入 SWMM 节点。']
        clipped = bool((scope['time_start'] and scope['time_start'] < times[0]) or
                       (scope['time_end'] and scope['time_end'] > times[-1]))
        if clipped:
            limits.append('请求时窗超出该结果保存范围，已标明实际覆盖，不视为完整补齐。')
        value = {'status': 'partial' if missing or clipped else 'complete', 'resolved_scope': scope,
                 'actual_time_start': selected_times[0], 'actual_time_end': selected_times[-1],
                 'saved_step_seconds': intervals, 'series': series, 'missing_samples': missing,
                 'interpretation_limits': limits}
        value['coverage_meaning'] = '完整表示请求范围内的实际保存样本已提取，不代表时窗内连续过程已观测；非保存时刻不插值。'
        if len(selected_times) == 1:
            value['interpretation_limits'].append('本时窗只有一个保存时刻，不能据此描述窗口内涨退过程。')
        if cells:
            value['node_cell_mapping'] = {n: mapped[n] for n in scope['nodes']}
        if native:
            value['native_metadata'] = native['metadata']
        if metric == 'max_fullness':
            maxima = {}
            for link in scope['links']:
                valid = [(s['links'][link]['depth_reference_ratio'], s['time']) for s in series
                         if s['links'][link]['depth_reference_ratio'] is not None]
                peak = max((v for v, _ in valid), default=None)
                maxima[link] = {'maximum': peak, 'unit': 'depth/diameter',
                                'times': [t for v, t in valid if v == peak]}
            value['maxima'] = maxima
        if metric == 'storage_volume_process':
            changes = []
            for a, b in zip(series, series[1:]):
                seconds = (datetime.fromisoformat(b['time']) - datetime.fromisoformat(a['time'])).total_seconds()
                change = {'start': a['time'], 'end': b['time'], 'nodes': {}, 'links': {}}
                for group, field in [('nodes', 'node_volume_m3'), ('links', 'link_volume_m3')]:
                    for obj in scope[group]:
                        va, vb = a[group][obj].get(field), b[group][obj].get(field)
                        delta = vb - va if va is not None and vb is not None else None
                        change[group][obj] = {'delta_volume_m3': delta,
                                             'mean_storage_change_Ls': delta * 1000 / seconds if delta is not None else None}
                changes.append(change)
            value['interval_volume_changes'] = changes
        owner_kind = 'node' if scope['anchor_node'] or scope['nodes'] else 'cell' if cells else 'link'
        owner = scope['anchor_node'] or (scope['nodes'][0] if scope['nodes'] else cells[0] if cells else scope['links'][0])
        row = {'source_model': root.parents[1].name, 'run_id': root.name, 'object_type': owner_kind,
               'object_id': owner, 'event_id': scope['event_id'], 'metric_name': metric, 'unit': 'structured',
               'value': value, 'time_start': selected_times[0], 'time_end': selected_times[-1],
               'calculation_method': VERSION, 'derivation': {'scope_signature': scope_signature(resolved, scope),
                  'source_hashes': before, 'scope_request': {'method': VERSION, 'metric_name': metric,
                      'source_files': resolved['source_files'], 'scope': scope}}}
        if uses_decimal3(package):
            row = round_evidence_row(row)
        row['evidence_id'] = 'SUP_' + digest(row)[:24]
        rows.append(row)
    if _hash_sources(root, resolved['source_files']) != before:
        raise ValueError('Saved source changed during extraction; no supplement committed')
    return {**({'numeric_precision': dict(PRECISION_POLICY)} if uses_decimal3(package) else {}),
            'schema_name': 'swmm_saved_result_supplement', 'schema_version': '1.0',
            'method': VERSION, 'model_name': root.parents[1].name, 'run_id': root.name,
            'request_signature': signature, 'resolved_request': resolved,
            'source_hashes': before, 'evidence_rows': rows}


def load_supplements(root, directory, state, base_rows):
    rows, seen = list(base_rows), {r['evidence_id'] for r in base_rows}
    for entry in state.get('supplements', []):
        name = entry['file']
        if not re.fullmatch(r'supplement_[0-9a-f]{64}\.json', name):
            raise ValueError('Invalid supplement filename')
        path = directory / name
        if sha256_file(path) != entry['sha256']:
            raise ValueError('Immutable supplement changed')
        package = json.loads(path.read_text(encoding='utf-8'))
        if package['model_name'] != state['model_name'] or package['run_id'] != state['run_id']:
            raise ValueError('Supplement belongs to another model/run')
        if _hash_sources(root, package['source_hashes']) != package['source_hashes']:
            raise ValueError('Supplement source changed; evidence is stale')
        for row in package['evidence_rows']:
            original_id = row['evidence_id']
            if original_id in seen:
                raise ValueError('Duplicate supplement evidence ID')
            if 'SUP_' + digest({k: v for k, v in row.items() if k != 'evidence_id'})[:24] != original_id:
                raise ValueError('Supplement content ID mismatch')
            seen.add(original_id)
            rows.append(row)
    return rows


def evidence_binding(state):
    binding = {'snapshot_hash': state['snapshot_hash'], 'supplements': state.get('supplements', []),
               'visible_ids': sorted(state['visible_ids'])}
    return dict(binding, sha256=digest(binding))
