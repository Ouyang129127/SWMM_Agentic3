"""Prepare report facts without simulation or hydraulic cause inference."""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from workflow_agents.evidence.event_packages import parse_run_input
from workflow_agents.evidence.evidence_package import load_package
from workflow_agents.evidence.evidence_precision import round_value, uses_decimal3
from workflow_agents.evidence.sampled_events import identify_events
from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.saved_result_evidence import evidence_binding, load_supplements


SCHEMA = 'swmm_display_materials_v1'
SECTIONS = ('scene', 'rain', 'surface', 'overflow', 'causes', 'conclusions')
TITLES = ('模拟基本场景', '降雨特征', '地表淹没情况与演变',
          '管网冒溢情况与地表联系', '原因分析', '主要结论与关注重点')
MECHANISMS = {'M1': '来水集中与叠加', 'M2': '局部输水约束', 'M3': '下游高水位影响',
              'M4': '井口与水头条件', 'M5': '泵闸运行', 'M6': '初始蓄水与可用调蓄'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def verify_task(root, directory, state):
    """Reuse the investigation's immutable binding; never select revisions by mtime."""
    package = load_package(root, verify_sources=True)
    if state['source_hash'] != sha256_file(root / 'evidence/evidence_package.json'):
        raise ValueError('Report evidence package changed')
    if state['snapshot_hash'] != sha256_file(directory / 'evidence_snapshot.json'):
        raise ValueError('Report snapshot changed')
    diagnosis, verification = read(directory / 'diagnosis.json'), read(directory / 'verification.json')
    if (sha256_file(directory / 'diagnosis.json') != state['diagnosis_hash']
            or sha256_file(directory / 'verification.json') != state['verification_hash']):
        raise ValueError('Report diagnosis or verification changed')
    for doc in (diagnosis, verification):
        if (doc['revision'] != state['revision'] or doc['task_id'] != state['task_id']
                or doc['model_name'] != state['model_name'] or doc['run_id'] != state['run_id']):
            raise ValueError('Report revision/run/task mismatch')
        if doc.get('evidence_binding') != evidence_binding(state):
            raise ValueError('Report evidence binding mismatch')
    if not verification['claim_checks'] or any(
            c['verification_status'] != 'references_verified' for c in verification['claim_checks']):
        raise ValueError('Report requires successful current reference verification')
    rows = load_supplements(root, directory, state, read(directory / 'evidence_snapshot.json'))
    return package, diagnosis, rows


def _metadata_name(path, default):
    # Read display metadata only. YAML values are data, never executable instructions.
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('display_name:'):
                return line.split(':', 1)[1].strip().strip('\"\'') or default
    return default


def rainfall_metrics(path, end):
    frame = pd.read_csv(path)
    frame['timestamp'] = pd.to_datetime(frame.timestamp)
    values = pd.to_numeric(frame.value).to_numpy(float)
    if (len(frame) < 1 or frame.timestamp.duplicated().any()
            or not frame.timestamp.is_monotonic_increasing or not np.isfinite(values).all()
            or (values < 0).any()):
        raise ValueError('Invalid rainfall intervals')
    starts = frame.timestamp.to_numpy(dtype='datetime64[ns]')
    finish = pd.Timestamp(end).to_datetime64()
    if starts[-1] > finish:
        raise ValueError('Rainfall copy extends past declared event end')
    if finish <= starts[0]:raise ValueError('Rainfall event needs a positive time span')
    if starts[-1] < finish:
        # The final intensity also has an interval when an input copy lacks a
        # terminal zero row. Include that interval in both integration and plot.
        frame=pd.concat([frame,pd.DataFrame({'timestamp':[pd.Timestamp(finish)],'value':[0.]})],ignore_index=True)
        starts=frame.timestamp.to_numpy(dtype='datetime64[ns]');values=np.r_[values,0.]
    ends = np.r_[starts[1:], finish]
    dt = (ends - starts) / np.timedelta64(1, 'h')
    amounts = values * dt
    total = float(amounts.sum())
    duration = float((finish - starts[0]) / np.timedelta64(1, 'm'))
    width = min(30., duration)
    # Piecewise-constant integration works for irregular intervals and windows
    # crossing sample boundaries, unlike a hard-coded convolution of six rows.
    offsets = (starts - starts[0]) / np.timedelta64(1, 'm')
    right = (ends - starts[0]) / np.timedelta64(1, 'm')
    candidates = np.unique(np.clip(np.r_[offsets, right - width, 0, duration-width], 0, duration-width))
    window_amounts = [float((values * np.maximum(0, np.minimum(right, x+width)
                       - np.maximum(offsets, x)) / 60).sum()) for x in candidates]
    chosen = int(np.argmax(window_amounts)); start = float(candidates[chosen])
    peak = int(np.argmax(np.where(dt > 0, values, -1)))
    # Plateau-aware local peaks; count is a descriptive signal, not a diagnosis.
    positive = values[dt > 0]
    peaks = []
    i = 0
    while i < len(positive):
        j = i
        while j+1 < len(positive) and positive[j+1] == positive[i]:
            j += 1
        before = positive[i-1] if i else -1
        after = positive[j+1] if j+1 < len(positive) else -1
        if positive[i] > 0 and positive[i] > before and positive[i] > after:
            peaks.append(i)
        i = j+1
    shape = ('无降雨' if total == 0 else '近似均匀降雨' if np.ptp(positive) == 0
             else '单峰降雨' if len(peaks) == 1 else '双峰降雨' if len(peaks) == 2
             else '多峰降雨' if len(peaks) > 2 else '渐变降雨')
    fraction = float(offsets[peak] / duration)
    position = ('前段' if fraction < .25 else '中前段' if fraction < .5
                else '中后段' if fraction < .75 else '后段')
    evolution = ''
    if shape == '单峰降雨':
        rising = peak > 0 and np.all(np.diff(positive[:peak+1]) >= 0)
        falling = peak < len(positive)-1 and np.all(np.diff(positive[peak:]) <= 0)
        evolution = ('降雨前段逐渐增强，' if rising else '') + f'在{position}形成集中雨峰'
        evolution += '，随后逐步减弱。' if falling else '。'
        if not rising:
            evolution = evolution.removeprefix('在')
    elif shape == '双峰降雨':
        evolution = '降雨过程中形成两个雨峰，雨强在两次雨峰之间回落。'
    elif shape == '多峰降雨':
        evolution = '降雨过程中雨强多次增强后回落，形成多个雨峰。'
    elif shape == '近似均匀降雨':
        evolution = '降雨过程中雨强保持平稳，雨量沿时间分布较为均匀。'
    metrics = {'total_mm': total, 'duration_min': duration, 'shape': shape,
               'peak_mm_h': float(values[peak]), 'peak_start': str(frame.timestamp.iloc[peak]),
               'peak_end': str(pd.Timestamp(ends[peak])), 'peak_interval_min': float(dt[peak]*60),
               'window_minutes': width, 'window_start': str(frame.timestamp.iloc[0]+pd.Timedelta(minutes=start)),
               'window_end': str(frame.timestamp.iloc[0]+pd.Timedelta(minutes=start+width)),
               'window_mm': window_amounts[chosen],
               'window_share_percent': window_amounts[chosen]/total*100 if total else 0,
               'evolution_sentence': evolution}
    return metrics, frame, np.r_[0, np.cumsum(amounts[:-1])]


def _series(path, id_column):
    frame = pd.read_csv(path, sep='\t', dtype={id_column: str})
    frame['timestamp'] = pd.to_datetime(frame.date+' '+frame.time)
    if frame.duplicated([id_column, 'timestamp']).any():
        raise ValueError(f'Duplicate report process samples: {path}')
    return frame


def saved_time_metrics(times, target):
    """Timedelta arithmetic avoids assuming DatetimeIndex.asi8 uses ns.

    Pandas can store indices in microseconds. Timestamp.value is always ns;
    subtracting those integer representations silently picks the wrong frame.
    """
    intervals=np.r_[(times[1:]-times[:-1]).total_seconds()/60,0.]
    nearest=int(np.argmin(np.abs((times-pd.Timestamp(target)).total_seconds())))
    return intervals,nearest


def check_rounded_event_volumes(events, raw_events):
    """Reproduce each rounded integral, rather than relaxing mass-audit tolerances."""
    expected = {event['event_id']: round_value(event['estimated_volume_m3']) for event in raw_events}
    if (len(expected) != len(raw_events) or len(events) != len(expected)
            or {event['event_id'] for event in events} != set(expected)
            or any(event['estimated_volume_m3'] != expected[event['event_id']] for event in events)):
        raise ValueError('Report event volumes differ from rounded raw saved flooding')


def prepare_materials(root, directory, state):
    package, diagnosis, evidence = verify_task(root, directory, state)
    summary = read(root/'summary.json')
    if summary['run_id'] != root.name or summary['model_name'] != state['model_name']:
        raise ValueError('Report summary belongs to another run')
    static = Path(summary['static_model'])
    if not static.is_absolute():
        static = root/static
    sources = [root/'summary.json', root/'rainfall_event.txt', root/'swmm/model_with_event.inp',
               root/'swmm/nodes.tsv', root/'swmm/links.tsv', root/'ca2d/surface_depth.tsv',
               static/'config.json', static/'smid_grid.npy', static/'flow_mask.npy',
               static/'building_mask.npy', static/'node_to_cell_mapping.csv',
               directory/'diagnosis.json', directory/'verification.json',
               directory/'evidence_snapshot.json', root/'evidence/evidence_package.json']
    for name in ('road_mask.npy', 'valid_mask.npy', 'cells.csv'):
        if (static/name).exists():
            sources.append(static/name)
    for entry in state.get('supplements', []):
        sources.append(directory/entry['file'])
    md = directory/f"preliminary_report_r{state['revision']}.md"
    if not md.is_file():
        raise FileNotFoundError('Current internal diagnosis Markdown is not delivered yet')
    sources.append(md)
    source_hashes = {str(path.resolve()): sha256_file(path) for path in sources}
    cfg = read(static/'config.json')
    if not math.isfinite(float(cfg['cell_size'])) or float(cfg['cell_size'])<=0:
        raise ValueError('Report cell size must be finite and positive')
    cell_area = float(cfg['cell_size'])**2
    grid = np.load(static/'smid_grid.npy'); flow = np.load(static/'flow_mask.npy').astype(bool)
    building = np.load(static/'building_mask.npy').astype(bool)
    valid = np.load(static/'valid_mask.npy').astype(bool) if (static/'valid_mask.npy').exists() else grid > 0
    road_known = (static/'road_mask.npy').exists()
    road = np.load(static/'road_mask.npy').astype(bool)&flow if road_known else np.zeros_like(flow)
    if any(mask.shape != grid.shape for mask in (flow, building, valid, road)):
        raise ValueError('Report static masks differ in shape')
    green = flow & ~road & ~building
    table = pd.read_csv(root/'ca2d/surface_depth.tsv', sep='\t')
    table['timestamp'] = pd.to_datetime(table.Date+' '+table.Time)
    if table.duplicated(['Smid', 'timestamp']).any():
        raise ValueError('Duplicate surface process samples')
    smids = grid[grid > 0]
    if len(smids) != len(set(smids)) or set(smids) != set(table.Smid):
        raise ValueError('Report surface grid differs from static model')
    pivot = table.pivot(index='timestamp', columns='Smid', values='Depth').reindex(columns=smids)
    array = pivot.to_numpy(float)
    if not np.isfinite(array).all() or (array < 0).any():
        raise ValueError('Missing or invalid surface values')
    times = pivot.index
    if (times[0] != pd.Timestamp(summary['swmm']['simulation_start'])
            or times[-1] != pd.Timestamp(summary['swmm']['simulation_end'])):
        raise ValueError('Surface time coverage differs from simulation')
    depth = np.zeros((len(times), *grid.shape), float); depth[:, grid > 0] = array
    step_minutes,rain_i = saved_time_metrics(times,summary['rainfall']['rainfall_end'])
    # Building-adjacent is an overlapping location attribute, not a third land class.
    padded = np.pad(building, 1); near = np.zeros_like(flow)
    for dr in range(3):
        for dc in range(3):
            near |= padded[dr:dr+grid.shape[0], dc:dc+grid.shape[1]]
    near &= flow
    max_depth = np.where(flow[None], depth, 0).max(axis=(1, 2))
    surface = {'classification_available': road_known, 'thresholds_m': [0.01, 0.15, 0.27, 0.4],
               'max_saved_depth_m': float(max_depth.max()), 'max_depth_time': str(times[int(max_depth.argmax())]),
               'max_solver_depth_m': summary['ca2d'].get('max_depth_m'),
               'saved_step_minutes': sorted(set(float(x) for x in step_minutes if x)),
               'final_max_depth_m': float(max_depth[-1]), 'threshold_metrics': {}, 'process': []}
    areas = {}
    for threshold in (.01, .15):
        wet = (depth >= threshold)&flow[None]
        area = wet.sum(axis=(1, 2))*cell_area; peak = int(area.argmax()); present = np.flatnonzero(area)
        categories = {}
        for label, mask in [('road', road), ('green', green), ('building_adjacent', near)]:
            values = (wet & mask[None]).sum(axis=(1, 2))*cell_area
            categories[label] = {'peak_area_m2': float(values.max()),
                'peak_time': str(times[int(values.argmax())]) if values.max() else None,
                'at_total_peak_m2': float(values[peak]), 'final_area_m2': float(values[-1])}
        surface['threshold_metrics'][str(threshold)] = {
            'max_area_m2': float(area[peak]), 'max_area_time': str(times[peak]) if area[peak] else None,
            'first_saved_time': str(times[present[0]]) if len(present) else None,
            'last_saved_time': str(times[present[-1]]) if len(present) else None,
            'final_area_m2': float(area[-1]), 'rain_end_area_m2': float(area[rain_i]),
            'rain_end_sample_time': str(times[rain_i]), 'ever_wet_area_m2': float(wet.any(axis=0).sum()*cell_area),
            'max_left_interval_duration_min': float((wet*step_minutes[:,None,None]).sum(axis=0).max()),
            'categories': categories}
        areas[threshold] = area
    surface['process'] = [{'time': str(t), 'wet_area_m2': float(areas[.01][i]),
                          'attention_area_m2': float(areas[.15][i]), 'max_depth_m': float(max_depth[i])}
                         for i, t in enumerate(times)]
    rain, rain_frame, cumulative = rainfall_metrics(root/'rainfall_event.txt', summary['rainfall']['rainfall_end'])
    nodes = _series(root/'swmm/nodes.tsv', 'node_id'); links = _series(root/'swmm/links.tsv', 'link_id')
    model = parse_run_input(root/'swmm/model_with_event.inp')
    if model['options'].get('FLOW_UNITS') != 'LPS':
        raise ValueError('Report currently requires saved LPS/m outputs')
    visible = set(state['visible_ids'])
    contexts = [r for r in evidence if r['metric_name']=='event_context' and r['evidence_id'] in visible]
    events = [entry['facts'] for entry in state.get('event_manifest', [])] or [r['value']['event'] for r in contexts]
    if len({e['event_id'] for e in events}) != len(events):
        raise ValueError('Duplicate report events')
    assessments = {a['event_id']: a for a in diagnosis.get('event_assessments', [])}
    selected = {(r.get('event_id'), r['metric_name']): r for r in evidence if r['evidence_id'] in visible}
    mapping = pd.read_csv(static/'node_to_cell_mapping.csv', dtype={'node_id': str})
    numeric_notes = []; event_materials = []; all_ids = set(model['nodes']) | set(model['links'])
    for event in sorted(events, key=lambda e: (e['start'], e['node_id'], e['event_id'])):
        eid, node = event['event_id'], event['node_id']
        process = selected.get((eid,'local_process_series'),{}).get('value',{}).get('series',[])
        if not process:
            raise ValueError(f'Missing report event process: {eid}')
        topology = selected[(eid,'local_structure')]['value']
        composition = selected[(eid,'direct_source_composition')]['value']
        assessment = assessments.get(eid)
        incident = {key: val for key, val in topology['links'].items() if node in (val['from_node'],val['to_node'])}
        samples = []
        for item in process:
            target = item['nodes'][node]; outputs = {}; incoming = {}
            for key, edge in incident.items():
                q = item['links'][key]['signed_model_flow_Ls']
                if q is None:
                    raise ValueError(f'Missing incident flow: {eid}/{key}')
                towards = q*(1 if edge['to_node']==node else -1)
                outputs[key] = max(0.,-towards); incoming[key] = max(0.,towards)
            needed = ('total_inflow_Ls','lateral_inflow_Ls','flooding_Ls','depth_m','head_m')
            if any(target.get(key) is None for key in needed):
                raise ValueError(f'Missing node process: {eid}')
            samples.append({'time': item['time'], **{key:target[key] for key in needed},
                            'outflow_Ls': math.fsum(outputs.values()), 'outgoing_by_link_Ls': outputs,
                            'incoming_by_link_Ls': incoming})
        peak = next(s for s in samples if s['time']==event['peak_time'])
        residual = peak['total_inflow_Ls']-peak['outflow_Ls']-peak['flooding_Ls']
        if abs(residual)>.05:
            numeric_notes.append({'event_id':eid, 'note':'Do not state instantaneous inflow-outflow=flooding equality',
                                  'residual_Ls':residual})
        positions = mapping[mapping.node_id==node]
        if positions.empty:
            raise ValueError(f'Missing node surface mapping: {node}')
        associations = []
        for _, position in positions.iterrows():
            smid = int(position.smid); values = pivot[smid]
            rr, cc = int(position.cell_row), int(position.cell_col)
            if not (0<=rr<grid.shape[0] and 0<=cc<grid.shape[1]) or grid[rr,cc] != smid:
                raise ValueError(f'Mapping grid mismatch: {node}')
            alert_indices = np.flatnonzero(values.to_numpy() >= .15)
            episodes = []
            for group in np.split(alert_indices, np.flatnonzero(np.diff(alert_indices)>1)+1):
                if not len(group): continue
                first,last = int(group[0]),int(group[-1])
                bounded = first>0 and last+1<len(times)
                episodes.append({'first_saved':str(times[first]),'last_saved':str(times[last]),
                    'continuous_episode_upper_estimate_min':float((times[last+1]-times[first-1]).total_seconds()/60) if bounded else None,
                    'boundary_censored':not bounded})
            associations.append({'smid':smid, 'row':rr,'col':cc,
                'land_type':'道路' if road[rr,cc] else '绿地' if road_known and green[rr,cc] else '地表',
                'max_saved_depth_m':float(values.max()),'max_depth_time':str(values.idxmax()),
                'final_depth_m':float(values.iloc[-1]),'attention_episodes':episodes,
                'meaning':'注入网格的水深；不是整个节点邻域的最大值或水源贡献归因'})
        storage = selected.get((eid,'storage_volume_process'))
        event_materials.append({'event':event,'node_id':node,'event_id':eid,'process':samples,
            'source_composition':composition,'incident_links':incident,'surface_associations':associations,
            'assessment':assessment,'storage_evidence':storage['value'] if storage else None,
            'allow_simple_peak_balance':abs(residual)<=.05})
    total_volume = math.fsum(e['estimated_volume_m3'] for e in events)
    # Selected-task totals differ intentionally from whole-run background totals.
    scope = state.get('task_context',{}).get('scope','global')
    if scope=='global':
        raw_positive = set(nodes.loc[nodes.flooding_Ls>1e-9,'node_id'])
        if raw_positive != {e['node_id'] for e in events}:
            raise ValueError('Report event coverage differs from raw overflow nodes')
        ordered=nodes.sort_values(['node_id','timestamp'])
        seconds=(ordered.groupby('node_id').timestamp.shift(-1)-ordered.timestamp).dt.total_seconds().fillna(0)
        raw_volume=float((ordered.flooding_Ls*seconds).sum()/1000)
        if uses_decimal3(package):
            raw_events = identify_events(
                nodes.rename(columns={'flooding_Ls': 'flow_Ls'}).to_dict('records'),
                root.name, state['model_name'])['events']
            check_rounded_event_volumes(events, raw_events)
        elif not math.isclose(raw_volume,total_volume,rel_tol=1e-7,abs_tol=1e-6):
            raise ValueError('Report event volumes differ from raw saved flooding')
    gif=root/'ca2d/ca2d_animation.gif'
    if gif.exists():
        sources.append(gif);source_hashes[str(gif.resolve())]=sha256_file(gif)
    wet_indices=np.flatnonzero(areas[.01]); phase_indices=[]
    candidates = ([int(wet_indices[0])] if len(wet_indices) else [])
    if areas[.15].max(): candidates.append(int(areas[.15].argmax()))
    candidates += [int(areas[.01].argmax()),rain_i,len(times)-1]
    for i in candidates:
        if i not in phase_indices:
            phase_indices.append(i)
    model_root=root.parents[1]
    model_name=_metadata_name(model_root/'model.yaml',state['model_name'])
    scenario=summary.get('scenario_name','')
    scenario_name=_metadata_name(model_root/'swmm/scenarios'/scenario/'scenario.yaml',
                                 {'baseline':'基准方案'}.get(scenario,scenario))
    for p in (model_root/'model.yaml', model_root/'swmm/scenarios'/scenario/'scenario.yaml'):
        if p.exists():source_hashes[str(p.resolve())]=sha256_file(p)
    materials={'schema':SCHEMA,'model_name':state['model_name'],'run_id':root.name,
        'task_id':state['task_id'],'revision':state['revision'],'scope':scope,
        'scene':{'model_display_name':model_name,'scenario_display_name':scenario_name,
                 'simulation_start':summary['swmm']['simulation_start'],'simulation_end':summary['swmm']['simulation_end'],
                 'rain_start':summary['rainfall']['rainfall_start'],'rain_end':summary['rainfall']['rainfall_end'],
                 'post_rain_hours':summary['rainfall']['post_rainfall_hours'],'run_date':summary.get('created_at','')},
        'rain':rain,'surface':surface,'overflow':{'node_count':len({e['node_id'] for e in events}),
            'event_count':len(events),'total_volume_m3':total_volume,
            'first_start':min((e['start'] for e in events),default=None),
            'last_end':max((e['end'] for e in events),default=None)},
        'events':event_materials,'diagnosis_overview':diagnosis.get('run_overview',{}),
        'verified_claims':diagnosis.get('claims',[]),'global_mechanisms':diagnosis.get('mechanism_assessments',[]),
        'task_context':state.get('task_context',{}),'numeric_notes':numeric_notes,
        'internal_markdown':md.read_text(encoding='utf-8'),
        'source_hashes':source_hashes,'evidence_binding':evidence_binding(state)}
    plot_data={'grid':grid,'flow':flow,'road':road,'green':green,'building':building,'valid':valid,
        'classification_available':road_known,'depth':depth,'times':times,'areas':areas,'max_depth':max_depth,
        'phase_indices':phase_indices,'rain_frame':rain_frame,'cumulative_rain':cumulative,'mapping':mapping,
        'gif':gif if gif.exists() else None,'known_ids':sorted(all_ids)}
    return materials,plot_data


def assert_sources_current(materials):
    for name,digest in materials['source_hashes'].items():
        if sha256_file(Path(name)) != digest:
            raise ValueError(f'Report source changed during generation: {name}')
