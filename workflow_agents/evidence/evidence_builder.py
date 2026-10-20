"""Deterministic EvidenceBuilder: one package, all saved events, no LLM."""
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from workflow_agents.evidence.rainfall_context import build_rainfall_context
from workflow_agents.evidence.event_packages import build_event_evidence, parse_run_input
from workflow_agents.evidence.evidence_package import PACKAGE_SCHEMA, PACKAGE_VERSION, write_package
from workflow_agents.evidence.evidence_precision import PRECISION_POLICY, round_evidence_row
from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.sampled_events import identify_events
from workflow_agents.common.state import resolve_run_root

POSITIVE_FLOW_LS = 1e-9
POSITIVE_DEPTH_M = 0.005
FLOW_DIRECTION_EPS_LS = 1e-6

def _interval_volume(group: pd.DataFrame) -> float:
    group = group.sort_values("DateTime")
    seconds = group["DateTime"].diff().dt.total_seconds().fillna(0.0)
    mean_flow = (group["flow_Ls"] + group["flow_Ls"].shift(1)) * 0.5
    return float((mean_flow.fillna(0.0) * seconds).sum() / 1000.0)


def _interval_duration(times: pd.Series, selected: pd.Series) -> float:
    """Left-interval duration; a terminal state has no extra time weight."""
    frame = pd.DataFrame({"time": times, "selected": selected}).sort_values("time")
    minutes = (frame["time"].shift(-1) - frame["time"]).dt.total_seconds().fillna(0.0) / 60.0
    return float(minutes[frame["selected"]].sum())


def _read_tsv(path, object_column, fields):
    if not path.exists():
        raise FileNotFoundError(f'Missing source file: {path}')
    df = pd.read_csv(path, sep='\t', dtype={object_column: str})
    missing = {object_column, 'date', 'time', *fields} - set(df.columns)
    if missing or df.empty:
        raise ValueError(f'Empty or incomplete source table: {path}; missing={sorted(missing)}')
    if df[object_column].isna().any():
        raise ValueError(f'Missing object ID: {path}')
    df['DateTime'] = pd.to_datetime(df['date'].astype(str) + ' ' + df['time'].astype(str), errors='raise')
    if df.duplicated([object_column, 'DateTime']).any():
        raise ValueError(f'Duplicate object timestamps: {path}')
    for field in fields:
        df[field] = pd.to_numeric(df[field], errors='raise')
        if not np.isfinite(df[field]).all():
            raise ValueError(f'Non-finite {field}: {path}; missing values are not zero')
    return df.sort_values([object_column, 'DateTime'])


def _rainfall_path(root, summary):
    for key in ('simulation_rainfall_file', 'rainfall_event_copy', 'rainfall_file'):
        if summary.get(key):
            path = Path(summary[key])
            if path.is_file():
                return path
            if (root / path).is_file():
                return root / path
    return root / 'rainfall_event.txt'


def build_evidence_for_run(model_name, run_id):
    root = resolve_run_root(model_name, run_id)
    model_name, run_id = root.parents[1].name, root.name
    summary_path = root / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    if summary.get('run_id', run_id) != run_id or summary.get('model_name', model_name) != model_name:
        raise ValueError('Run summary belongs to another model/run')
    rain_path = _rainfall_path(root, summary)
    sources = [summary_path, root / 'swmm/model_with_event.inp', root / 'swmm/model.out',
               root / 'swmm/nodes.tsv', root / 'swmm/links.tsv', root / 'ca2d/surface_depth.tsv', rain_path]
    def source_name(path):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return str(path.resolve())
    before = {source_name(path): sha256_file(path) for path in sources if path.is_file()}
    missing_sources = sorted(source_name(path) for path in sources if not path.exists())
    model = parse_run_input(root / 'swmm/model_with_event.inp')
    if not model['available'] or model['options'].get('FLOW_UNITS', '').upper() != 'LPS':
        raise ValueError('Evidence extraction currently requires the run input with SI/LPS units')
    nodes = _read_tsv(root / 'swmm/nodes.tsv', 'node_id', ['depth_m', 'flooding_Ls'])
    if (nodes['flooding_Ls'] < -POSITIVE_FLOW_LS).any():
        raise ValueError('Negative node flooding is not an overflow observation')
    catalog = identify_events(nodes.rename(columns={'flooding_Ls': 'flow_Ls'}).to_dict('records'), run_id, model_name)
    local = build_event_evidence(root, catalog)
    rows = []
    def add(group, kind, obj, metric, value, unit, source, method, threshold='', at=None, **details):
        key = f'{kind}|{obj}|{metric}'
        start, end = group['DateTime'].min(), group['DateTime'].max()
        row = {'evidence_id': 'STAT_' + hashlib.sha256(key.encode()).hexdigest()[:20],
               'source_model': model_name, 'run_id': run_id, 'event_id': '',
               'event_name': summary.get('event_name', ''), 'scenario_name': summary.get('scenario_name', ''),
               'object_type': kind, 'object_id': str(obj), 'metric_name': metric,
               'value': float(value), 'unit': unit, 'source_file': source,
               'time_start': str(start), 'time_end': str(end),
               'calculation_method': method, 'threshold': threshold,
               'at_time': str(at) if at is not None else None, **details}
        rows.append(row)
    for node, group in nodes.groupby('node_id', sort=True):
        events = [e for e in catalog['events'] if e['node_id'] == node]
        facts = [('total_flooding_volume', sum(e['estimated_volume_m3'] for e in events), 'm3', 'sum_event_left_sample_volumes'),
                 ('max_flooding_flow', group['flooding_Ls'].max(), 'L/s', 'maximum_saved_sample'),
                 ('flooding_duration', sum(e['duration_minutes'] for e in events), 'min', 'sum_event_left_interval_durations'),
                 ('overflow_event_count', len(events), 'count', 'positive_saved_sample_segments')]
        for metric, value, unit, method in facts:
            add(group, 'node', node, metric, value, unit, 'swmm/nodes.tsv', method,
                at=group.loc[group['flooding_Ls'].idxmax(), 'DateTime'] if metric == 'max_flooding_flow' else None)
        index = group['depth_m'].idxmax()
        add(group, 'node', node, 'max_node_depth', group.loc[index, 'depth_m'], 'm', 'swmm/nodes.tsv',
            'maximum_saved_sample', at=group.loc[index, 'DateTime'])
    links = _read_tsv(root / 'swmm/links.tsv', 'link_id', ['flow_Ls', 'depth_m'])
    for link, group in links.groupby('link_id', sort=True):
        for metric, values, unit in [('max_flow', group['flow_Ls'].abs(), 'L/s'), ('max_link_depth', group['depth_m'], 'm')]:
            index = values.idxmax()
            add(group, 'link', link, metric, values.loc[index], unit, 'swmm/links.tsv',
                'maximum_absolute_saved_flow' if metric == 'max_flow' else 'maximum_saved_sample', at=group.loc[index, 'DateTime'])
        signs = [1 if v > 0 else -1 for v in group['flow_Ls'] if abs(v) > FLOW_DIRECTION_EPS_LS]
        changes = sum(a != b for a, b in zip(signs, signs[1:]))
        add(group, 'link', link, 'flow_direction_changes', changes, 'count', 'swmm/links.tsv',
            'sign_changes_ignoring_near_zero', threshold=FLOW_DIRECTION_EPS_LS)
        geometry = model['links'].get(link, {}).get('cross_section_fields')
        try:
            reference = float(geometry[1]) if geometry else 0
        except (ValueError, IndexError):
            reference = 0
        if reference > 0:
            fullness = group['depth_m'].clip(lower=0) / reference
            index = fullness.idxmax()
            definition = {'depth_reference_m': reference, 'reference_method': 'XSECTIONS_Geom1',
                          'interpretation': 'depth/reference proxy; not proof of pressure, capacity failure or bottleneck'}
            add(group, 'link', link, 'max_fullness', fullness.loc[index], 'ratio', 'swmm/links.tsv',
                'maximum_depth_over_section_reference', at=group.loc[index, 'DateTime'], **definition)
            for metric, threshold, selected in [('fullness_ge_0_8_duration', .8, fullness >= .8),
                                                 ('fullness_ge_0_95_duration', .95, fullness >= .95),
                                                 ('depth_above_section_reference_duration', 1., fullness > 1.)]:
                add(group, 'link', link, metric, _interval_duration(group['DateTime'], selected), 'min',
                    'swmm/links.tsv', 'saved_left_interval_threshold_duration', threshold=threshold, **definition)
    surface_path = root / 'ca2d/surface_depth.tsv'
    if surface_path.exists():
        surface = pd.read_csv(surface_path, sep='\t', dtype={'Smid': str}).rename(columns={'Date': 'date', 'Time': 'time'})
        required = {'Smid', 'date', 'time', 'Depth'}
        if not required <= set(surface.columns) or surface.empty:
            raise ValueError('Empty or incomplete surface results')
        surface['DateTime'] = pd.to_datetime(surface['date'].astype(str) + ' ' + surface['time'].astype(str), errors='raise')
        surface['Depth'] = pd.to_numeric(surface['Depth'], errors='raise')
        if surface['Smid'].isna().any() or not np.isfinite(surface['Depth']).all() or surface.duplicated(['Smid', 'DateTime']).any():
            raise ValueError('Invalid surface objects, timestamps or depth; missing is not zero')
        for cell, group in surface.sort_values(['Smid', 'DateTime']).groupby('Smid', sort=True):
            index = group['Depth'].idxmax()
            add(group, 'cell', cell, 'max_depth', group.loc[index, 'Depth'], 'm', 'ca2d/surface_depth.tsv',
                'maximum_saved_sample', at=group.loc[index, 'DateTime'])
            add(group, 'cell', cell, 'ponding_duration', _interval_duration(group['DateTime'], group['Depth'] > POSITIVE_DEPTH_M),
                'min', 'ca2d/surface_depth.tsv', 'saved_left_interval_threshold_duration', threshold=POSITIVE_DEPTH_M)
        surface_status = 'available'
    elif summary.get('ca2d', {}).get('status') == 'NO_SURFACE_INFLOW':
        surface_status = 'not_simulated_no_surface_inflow'
    else:
        raise FileNotFoundError('Missing surface result without explicit NO_SURFACE_INFLOW status')
    rainfall = build_rainfall_context(rain_path, event_name=summary.get('event_name', ''),
                                     run_id=run_id, model_name=model_name, scenario_name=summary.get('scenario_name', ''))
    rows.extend(local['evidence_rows'])
    rows.append({'evidence_id': 'CTX_RAINFALL', 'source_model': model_name, 'run_id': run_id,
                 'object_type': 'run', 'object_id': run_id, 'metric_name': 'rainfall_context',
                 'value': rainfall, 'unit': 'structured', 'source_file': source_name(rain_path),
                 'calculation_method': 'rainfall_context_v1'})
    after = {source_name(path): sha256_file(path) for path in sources if path.is_file()}
    if before != after or missing_sources != sorted(source_name(path) for path in sources if not path.exists()):
        raise ValueError('Run artifacts changed during evidence extraction')
    package = {'schema_name': PACKAGE_SCHEMA, 'schema_version': PACKAGE_VERSION,
               'model_name': model_name, 'run_id': run_id,
               'event_name': summary.get('event_name', ''), 'scenario_name': summary.get('scenario_name', ''),
               'source_hashes': before, 'missing_sources': missing_sources,
               'event_detection': {k: v for k, v in catalog.items() if k not in ('events', 'model_name', 'run_id')},
               'overview': {**local['overview'], 'surface_status': surface_status,
                            'evidence_count': len(rows), 'metrics': dict(sorted(Counter(r['metric_name'] for r in rows).items()))},
               'packages': local['packages'],
               'numeric_precision': dict(PRECISION_POLICY),
               'evidence_rows': [round_evidence_row(row) for row in rows]}
    if summary.get('model_context'):
        package['overview']['model_context'] = summary['model_context']
    path = write_package(root, package)
    return {'model_name': model_name, 'run_id': run_id, 'evidence_package': str(path),
            'schema_version': PACKAGE_VERSION, **package['overview']}
