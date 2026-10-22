"""Validate a scope upgrade against saved results, using a disposable copy.

No solver or LLM call. Every source-run file is hashed before/after. Only
topology selection may change; retained process samples and other facts must
match. Existing check_saved_results.py remains the strict equality checker.
"""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile

from check_saved_results import digest, relocate, rebuild

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from workflow_agents.evidence.event_packages import parse_run_input
from workflow_agents.evidence.topology_scope import SCOPE_POLICY, build_topology_scope
from workflow_agents.orchestration.tasks import prepare_task
from workflow_agents.diagnosis.service import build_diagnosis_prompt, serialize_prompt


def normalize_scope_changes(expected, actual):
    """Normalize permitted selection changes; reject alterations to kept facts."""
    expected = copy.deepcopy(expected)
    old_rows = {r['evidence_id']: r for r in expected['evidence_rows']}
    changes = []
    for row in actual['evidence_rows']:
        old = old_rows[row['evidence_id']]
        metric, value, want = row['metric_name'], row['value'], old['value']
        if metric == 'local_structure':
            before = {'nodes': sorted(want['nodes']), 'links': sorted(want['links'])}
            want['nodes'] = {k: want['nodes'][k] for k in value['nodes']}
            want['links'] = {k: want['links'][k] for k in value['links']}
            want['topology_scope'] = copy.deepcopy(value['topology_scope'])
            assert value['topology_scope']['policy'] == SCOPE_POLICY
            changes.append({'event_id': row['event_id'], 'before': before,
                            'after': {'nodes': sorted(value['nodes']), 'links': sorted(value['links'])},
                            'boundaries': value['topology_scope']['boundary_nodes']})
        elif metric == 'local_process_series':
            assert len(want['series']) == len(value['series']), 'Saved samples were dropped'
            for old_sample, new_sample in zip(want['series'], value['series']):
                assert old_sample['time'] == new_sample['time'], 'Saved timestamps changed'
                for kind in ('nodes', 'links'):
                    old_sample[kind] = {k: old_sample[kind][k] for k in new_sample[kind]}
            want['scope_policy'] = value['scope_policy']
        elif metric == 'facilities_and_storage':
            want['local_facilities'] = {k: want['local_facilities'][k] for k in value['local_facilities']}
        elif metric == 'rainfall_context':
            want.pop('created_at', None)
            value.pop('created_at', None)
        assert old == row, f'Changed retained evidence: {row["evidence_id"]}/{metric}'
    expected['schema_version'] = actual['schema_version']
    expected['overview']['event_scope_policy'] = actual['overview']['event_scope_policy']
    # The summary is rebound to the disposable copy, changing its file digest.
    expected['source_hashes']['summary.json'] = actual['source_hashes']['summary.json']
    assert expected == actual, 'Unexpected package changes outside the scope policy'
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--inspect-node', action='append', default=[], help='Additional topology-only target; no invented event')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    source = args.run.resolve()
    source_model = source.parents[1]
    before = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob('*') if p.is_file()}
    baseline = json.loads((source / 'evidence/evidence_package.json').read_text(encoding='utf-8'))
    temp_base = PROJECT / 'outputs/scope-upgrade-validation'
    temp_base.mkdir(parents=True, exist_ok=True)
    result = {'run_id': source.name, 'simulation_performed': False, 'llm_called': False}
    try:
        with tempfile.TemporaryDirectory(prefix='scope-', dir=temp_base) as temporary:
            temporary = Path(temporary).resolve()
            assert temporary.is_relative_to(temp_base.resolve()) and temporary != temp_base.resolve()
            model = temporary / source_model.name
            root = model / 'runs' / source.name
            shutil.copytree(source, root, ignore=shutil.ignore_patterns('diagnosis_tasks', '__pycache__'))
            for name in ('static', 'mapping'):
                if (source_model / name).exists():
                    shutil.copytree(source_model / name, model / name)
            for p in root.glob('*.json'):
                p.write_text(json.dumps(relocate(json.loads(p.read_text(encoding='utf-8')), source_model, model),
                                        ensure_ascii=False, indent=2), encoding='utf-8')
            actual = rebuild(PROJECT, root, 'workflow_agents.evidence.evidence_builder')
            # Keep an untouched copy for source validation/task preparation.
            changes = normalize_scope_changes(relocate(baseline, source_model, model), copy.deepcopy(actual))
            directory, state, rows = prepare_task(root, source_model.name, source.name, '')
            prompt = build_diagnosis_prompt(state, rows)
            parsed = parse_run_input(root / 'swmm/model_with_event.inp')
            extra = {node: build_topology_scope(parsed, node) for node in args.inspect_node}
            old_size = sum(len(json.dumps(r, ensure_ascii=False, separators=(',', ':')))
                           for r in baseline['evidence_rows'] if r['metric_name'] == 'local_process_series')
            new_size = sum(len(json.dumps(r, ensure_ascii=False, separators=(',', ':')))
                           for r in actual['evidence_rows'] if r['metric_name'] == 'local_process_series')
            result.update(status='passed', scope_policy=SCOPE_POLICY, package_version=actual['schema_version'],
                          event_count=actual['overview']['event_count'], event_scope_changes=changes,
                          additional_topology_only_targets=extra, old_process_characters=old_size,
                          new_process_characters=new_size,
                          process_character_reduction_percent=round((1-new_size/old_size)*100, 2) if old_size else 0,
                          prepared_task_event_count=len(state['event_manifest']),
                          serialized_user_prompt_characters=len(serialize_prompt(prompt)),
                          retained_samples_and_other_facts_unchanged=True)
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__, error=str(exc))
    finally:
        after = {p.relative_to(source).as_posix(): digest(p) for p in source.rglob('*') if p.is_file()}
        result['source_run_unchanged'] = before == after
        if not result['source_run_unchanged']:
            result['status'] = 'failed'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
