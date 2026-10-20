import json
import os
from evidence_fixtures import save_fixture
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from workflow_agents.evidence.evidence_selection import EVENT_GROUPS, SELECTION_VERSION, lookup_evidence, select_initial_evidence
from workflow_agents.orchestration.investigation import advance_investigation
from workflow_agents.diagnosis.service import build_diagnosis_prompt
from workflow_agents.verification.reference_checks import check_references
from workflow_agents.common.hashing import sha256_file


def row(eid, kind='node', obj='N1', metric='max_flooding_flow', value='0', event='', unit='L/s', **kwargs):
    return dict(evidence_id=eid, object_type=kind, object_id=obj, metric_name=metric,
                value=value, event_id=event, unit=unit, source_model='m', run_id='r', **kwargs)


def scope(node=None, event=None):
    return {'scope': 'event' if event else 'node' if node else 'global',
            'node_id': node, 'event_id': event, 'original_question': '分析'}


def packages(count=12):
    result = []
    for i in range(count):
        for metric in sorted(EVENT_GROUPS):
            value = {'status': 'available'}
            if metric == 'local_structure':
                value = {'nodes': {'N1': {}, 'N2': {}}, 'links': {'L1': {}}}
            result.append(row(f'FP{i}_{metric}', metric=metric, event=f'N1:E{i:03}', value=value))
    return result


class SelectionTests(unittest.TestCase):
    def test_all_event_groups_survive_order_changes_and_irrelevant_grid_growth(self):
        required = packages()  # 72 rows: a fixed 60-row gate loses events.
        initial = select_initial_evidence(required, scope('N1'), 'm', 'r')
        shuffled = required + [row(f'cell{i}', 'cell', str(i), 'max_depth', '0', unit='m') for i in range(2000)]
        random.Random(42).shuffle(shuffled)
        actual = select_initial_evidence(shuffled, scope('N1'), 'm', 'r')
        self.assertEqual(actual, initial)
        self.assertEqual(len(actual[1]), 72)
        self.assertTrue(all(not event['missing_groups'] for event in actual[2]['event_coverage']))

    def test_global_keeps_every_event_without_sending_grid_rows(self):
        rows = packages() + [row(f'C{i}', 'cell', str(i), 'max_depth', str(i), unit='m') for i in range(1000)]
        derived, ids, manifest = select_initial_evidence(rows, scope(), 'm', 'r')
        self.assertEqual(len(manifest['event_coverage']), 12)
        self.assertTrue(all(r['evidence_id'] in ids for r in packages()))
        self.assertFalse(any(eid.startswith('C') for eid in ids))
        summary = derived[0]
        self.assertEqual(summary['value']['source_row_count'], 1000)
        self.assertEqual(summary['value']['maximum'], 999)
        # All rows affect statistics, not just the first or most severe subset.
        self.assertEqual(summary['value']['zero_value_count'], 1)
        reversed_result = select_initial_evidence(list(reversed(rows)), scope(), 'm', 'r')
        self.assertEqual((derived, ids, manifest), reversed_result)

    def test_event_scope_includes_connected_context_but_not_other_events(self):
        rows = packages(2) + [row('N2bg', obj='N2'), row('link', 'link', 'L1', 'max_fullness', '.9'),
                              row('unrelated', obj='N3'), row('other', obj='N2', event='N2:E001')]
        _, ids, manifest = select_initial_evidence(rows, scope('N1', 'N1:E001'), 'm', 'r')
        self.assertEqual(len(ids), 8)
        self.assertIn('N2bg', ids)
        self.assertIn('link', ids)
        self.assertNotIn('other', ids)
        self.assertNotIn('unrelated', ids)
        self.assertEqual(manifest['related_links'], ['L1'])

    def test_missing_package_types_are_explicit(self):
        rows = [row('event', metric='event_duration', event='N1:E001', unit='min')]
        _, ids, manifest = select_initial_evidence(rows, scope('N1'), 'm', 'r')
        self.assertEqual(ids, ['event'])
        self.assertEqual(set(manifest['event_coverage'][0]['missing_groups']), EVENT_GROUPS)

    def test_no_overflow_still_has_all_metric_classes_and_separate_units(self):
        rows = [row('n'), row('l', 'link', 'L1', 'max_fullness', '1', unit='ratio'),
                row('m', 'cell', 'C1', 'max_depth', '', unit='m'),
                row('ft', 'cell', 'C2', 'max_depth', '3', unit='ft')]
        derived, ids, manifest = select_initial_evidence(rows, scope(), 'm', 'r')
        self.assertEqual(len(derived), 4)
        self.assertEqual(len(ids), 4)
        missing = next(r for r in derived if r['unit'] == 'm')
        self.assertIsNone(missing['value']['maximum'])
        self.assertEqual(missing['value']['missing_or_nonfinite_count'], 1)
        self.assertEqual(manifest['event_coverage'], [])
        self.assertEqual(next(r for r in derived if r['unit'] == 'ratio')['value']['maximum'], 1)

    def test_summary_groups_do_not_mix_time_or_methods(self):
        rows = [row('a', time_start='t1', calculation_method='left'),
                row('b', time_start='t2', calculation_method='left'),
                row('c', time_start='t1', calculation_method='trapezoid')]
        derived, _, _ = select_initial_evidence(rows, scope(), 'm', 'r')
        self.assertEqual(len(derived), 2)
        left = next(r for r in derived if r['source_calculation_method'] == 'left')
        self.assertEqual(len(left['value']['by_source_window']['records']), 2)
        self.assertEqual([r[0] for r in left['value']['by_source_window']['records']], ['t1', 't2'])

    def test_different_thresholds_are_not_combined(self):
        rows = [row('a', threshold='.8'), row('b', threshold='.95')]
        derived, _, _ = select_initial_evidence(rows, scope(), 'm', 'r')
        self.assertEqual(len(derived), 2)


    def test_prompt_keeps_original_metadata_and_source_paths(self):
        rows = [row('a', source_file='swmm/nodes.tsv'), row('b', source_file='swmm/links.tsv')]
        state = {'task_context': scope('N1'), 'visible_ids': ['a', 'b'],
                 'evidence_overview': {}, 'observations': []}
        prompt = build_diagnosis_prompt(state, rows)
        restored = prompt['visible_evidence']
        self.assertEqual(restored, rows)

    def test_summary_is_citable_without_pretending_raw_ids_were_seen(self):
        derived, _, _ = select_initial_evidence([row('raw')], scope(), 'm', 'r')
        claims = {'source_model': 'm', 'model_name': 'm', 'run_id': 'r', 'claims': [
            {'claim_id': 'c1', 'evidence_ids': [derived[0]['evidence_id']]},
            {'claim_id': 'c2', 'evidence_ids': ['raw']}]}
        checked = check_references(claims, derived, 'm', 'r')['claim_checks']
        self.assertEqual(checked[0]['verification_status'], 'references_verified')
        self.assertEqual(checked[1]['verification_status'], 'references_missing')

    def test_queries_return_full_matching_set_and_reject_positional_scans(self):
        rows = [row(f'E{i}') for i in range(101)]
        result = lookup_evidence(rows, {'object_id': 'N1'})
        self.assertEqual(len(result['rows']), 101)
        self.assertTrue(result['complete_match_set'])
        for request in ({}, {'object_type': 'node'}, {'object_id': 'N1', 'offset': 60}):
            with self.assertRaises(ValueError):
                lookup_evidence(rows, request)

    def test_original_series_preserves_null_missing_signed_flow_and_all_samples(self):
        series = [{'time': str(i), 'nodes': {'N1': {'head': 1.0000000000001 + i,
                  'method': 'native_output', 'missing': None}}, 'flow': -i, 'empty': {}}
                  for i in range(100)]
        series[3]['nodes']['N1'].pop('missing')
        before = json.dumps(series)
        original = row('S', metric='local_process_series', value={'series': series})
        state = {'task_context': scope('N1'), 'visible_ids': ['S'], 'evidence_overview': {}, 'observations': []}
        prompt = build_diagnosis_prompt(state, [original])
        self.assertEqual(json.dumps(prompt['visible_evidence'][0]), json.dumps(original))
        self.assertIsInstance(prompt['visible_evidence'][0]['value']['series'], list)
        self.assertEqual(json.dumps(series), before)
        # A consumer editing its request cannot alter the frozen evidence.
        prompt['visible_evidence'][0]['value']['series'][0]['nodes']['N1']['head'] = 999
        self.assertEqual(json.dumps(series), before)

    def test_expanded_scope_is_sent_as_complete_original_json(self):
        series = [{'time': str(i), 'nodes': {f'N{j}': {'head_m': j + i / 3,
                  'lateral_inflow_Ls': None if j == 2 else j, 'head_method': 'native_output'} for j in range(32)},
                  'links': {f'L{j}': {'signed_model_flow_Ls': -j * i, 'depth_m': .3} for j in range(31)}}
                  for i in range(8)]
        series[2]['nodes']['N2'].pop('lateral_inflow_Ls')
        records = [row('P', metric='local_process_series', value={'series': series})]
        for i in range(3):
            records.append(row(f'S{i}', metric='local_structure', event=f'N1:E{i}', value={
                'nodes': {f'N{j}': {'invert_elevation': j, 'node_type': 'JUNCTIONS'} for j in range(32)},
                'links': {f'L{j}': {'from_node': f'N{j}', 'to_node': f'N{j+1}'} for j in range(31)},
                'topology_scope': {'target_node': 'N1', 'event_test': i}}))
        records.extend(row(f'B{i}', obj=f'N{i}', value=i, unit='L/s', calculation_method='left',
                           source_file='swmm/nodes.tsv', time_start='t1', time_end='t2') for i in range(100))
        original = json.dumps(records, sort_keys=True)
        state = {'task_context': scope('N1'), 'visible_ids': [r['evidence_id'] for r in records],
                 'evidence_overview': {}, 'observations': []}
        prompt = build_diagnosis_prompt(state, records)
        self.assertEqual(json.dumps(records, sort_keys=True), original)
        restored = prompt['visible_evidence']
        self.assertEqual({r['evidence_id']: r for r in restored}, {r['evidence_id']: r for r in records})
        supplied = next(r for r in prompt['visible_evidence'] if r['evidence_id'] == 'P')
        self.assertIsInstance(supplied['value']['series'], list)
        self.assertEqual(json.dumps(restored, sort_keys=True), original)
        for key in ('evidence_shared_metadata', 'hydraulic_structure', 'evidence_source_files', 'transport_integrity'):
            self.assertNotIn(key, prompt)

    def test_original_structure_keeps_each_event_definition_and_empty_values(self):
        records = [row('a', metric='local_structure', event='N1:E1', value={'nodes': {'N1': {'head': 2}}, 'links': {}}),
                   row('b', metric='local_structure', event='N1:E2', value={'nodes': {'N1': {'head': 3}, 'N2': None}, 'links': {}})]
        state = {'task_context': scope(), 'visible_ids': ['a', 'b'], 'evidence_overview': {}, 'observations': []}
        restored = build_diagnosis_prompt(state, records)['visible_evidence']
        self.assertEqual(restored, records)

    def test_retired_environment_cap_cannot_reject_or_truncate_evidence(self):
        rows = [row('huge', value='x' * 130000)]
        state = {'task_context': scope('N1'), 'visible_ids': ['huge'],
                 'evidence_overview': {}, 'observations': []}
        with patch.dict(os.environ, {'DIAGNOSIS_MAX_CONTEXT_CHARACTERS': '1'}):
            prompt = build_diagnosis_prompt(state, rows)
        self.assertEqual(prompt['visible_evidence'], rows)
        self.assertNotIn('context_policy', prompt)

    def test_large_complete_request_has_no_local_limit_policy(self):
        records = [row('huge', value='x' * 130000)]
        state = {'task_context': scope('N1'), 'visible_ids': ['huge'], 'evidence_overview': {}, 'observations': []}
        prompt = build_diagnosis_prompt(state, records)
        self.assertNotIn('context_policy', prompt)
        self.assertEqual(prompt['visible_evidence'], records)

    def test_original_transport_still_rejects_mixed_run_bindings(self):
        records = [row('a'), dict(row('b'), run_id='other')]
        state = {'task_context': scope('N1'), 'visible_ids': ['a', 'b'], 'evidence_overview': {}, 'observations': []}
        with self.assertRaisesRegex(ValueError, 'mixes model/run'):
            build_diagnosis_prompt(state, records)

    def test_invalid_retired_cap_values_are_ignored(self):
        records = [row('huge', value='x' * 130000)]
        state = {'task_context': scope('N1'), 'visible_ids': ['huge'],
                 'evidence_overview': {}, 'observations': []}
        for setting in ('-1', 'abc', '1.5'):
            with self.subTest(setting=setting), patch.dict(os.environ, {'DIAGNOSIS_MAX_CONTEXT_CHARACTERS': setting}):
                self.assertEqual(build_diagnosis_prompt(state, records)['visible_evidence'], records)


class SelectionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'm/runs/r'
        (self.root / 'evidence').mkdir(parents=True)
        self.rows = [row(f'E{i}', event='N1:E001') for i in range(75)]
        self.rows.append(row('other', obj='N2', value='x' * 130000))
        save_fixture(self.root, self.rows)
        self.patcher = patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root)
        self.patcher.start()
        self.state = await advance_investigation('m', 'r', question='分析N1')
        self.task = self.state['task_id']
        self.folder = self.root / 'diagnosis_tasks' / self.task

    async def asyncTearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def answer(self, requests=None):
        return {'question_assessments': [{'question_id': f'Q{i}', 'status': 'needs_evidence', 'reason': '缺过程'} for i in range(1, 5)],
                'mechanism_assessments': [{'mechanism_id': f'M{i}', 'status': 'not_investigated', 'reason': '未调查'} for i in range(1, 7)],
                'claims': [{'object_type': 'node', 'object_id': 'N1', 'question_id': 'Q1', 'claim_kind': 'fact',
                            'claim_text': '本场保存有N1事件', 'evidence_ids': ['E0'], 'engineering_reason': '保存记录',
                            'alternatives': '机制待查', 'scope': '本场N1'}],
                'evidence_requests': requests or [], 'stop_reason': '需补证'}

    async def test_old_task_format_is_rejected_without_model_call(self):
        old = dict(self.state)
        old.pop('evidence_package_version')
        (self.folder / 'task.json').write_text(json.dumps(old), encoding='utf-8')
        complete = AsyncMock()
        with self.assertRaisesRegex(ValueError, '旧任务格式'):
            await advance_investigation('m', 'r', 'diagnose', self.task, complete=complete)
        complete.assert_not_called()
        self.assertEqual(json.loads((self.folder / 'task.json').read_text(encoding='utf-8')), old)

    async def test_large_evidence_request_is_added_without_truncation(self):
        req = {'tool': 'evidence_lookup', 'object_id': 'N2', 'reason': '补查相关节点'}
        await advance_investigation('m', 'r', 'diagnose', self.task,
                                    complete=AsyncMock(return_value=self.answer([req])))
        await advance_investigation('m', 'r', 'verify', self.task)
        await advance_investigation('m', 'r', 'report', self.task)
        with patch.dict(os.environ, {'DIAGNOSIS_MAX_CONTEXT_CHARACTERS': '1'}):
            state = await advance_investigation('m', 'r', 'evidence', self.task)
        saved = json.loads((self.folder / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['state'], 'ready_for_diagnosis')
        self.assertIn('other', saved['visible_ids'])
        prompt = build_diagnosis_prompt(state, self.rows)
        supplied = next(r for r in prompt['visible_evidence'] if r['evidence_id'] == 'other')
        self.assertEqual(supplied['value'], 'x' * 130000)


if __name__ == '__main__':
    unittest.main()
