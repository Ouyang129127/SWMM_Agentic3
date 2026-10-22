"""Saved-result capability, provenance and revision workflow regression tests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_initial_diagnosis import fixture, answer
from workflow_agents.orchestration.investigation import advance_investigation
from workflow_agents.evidence.requests import evidence_plan
from workflow_agents.diagnosis.contract import validate_initial, initial_reference_claims, row_subjects
from workflow_agents.evidence.saved_result_evidence import plan_request, extract, load_supplements
from workflow_agents.evidence.evidence_package import write_package
from workflow_agents.evidence.evidence_precision import PRECISION_POLICY
from workflow_agents.evidence.saved_result_evidence import digest


class SavedResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'm/runs/r'
        (self.root / 'evidence').mkdir(parents=True)
        self.package = fixture(self.root)
        (self.root / 'swmm').mkdir()
        (self.root / 'swmm/model_with_event.inp').write_text(
            '[OPTIONS]\nFLOW_UNITS LPS\nREPORT_STEP 00:01:00\n'
            '[JUNCTIONS]\nN1 0 1 0\nN2 0 1 0\n'
            '[CONDUITS]\nL1 N1 N2 10 .01 0 0\n'
            '[XSECTIONS]\nL1 CIRCULAR 1 0 0 0\n', encoding='utf-8')
        (self.root / 'swmm/links.tsv').write_text(
            'link_id\tdate\ttime\tflow_Ls\tdepth_m\n' + ''.join(
            f'L1\t2024-01-01\t00:{i:02}:00\t{-i}\t{min(i/10, 1)}\n' for i in range(21)), encoding='utf-8')
        self.patcher = patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def request(self, **kwargs):
        return dict({'tool': 'saved_result_extract', 'metric_name': 'fullness_time_series',
                     'object_type': 'link', 'object_ids': ['L1'], 'event_ids': ['N1:E001'],
                     'time_start': '2024-01-01 00:00:00', 'time_end': '2024-01-01 00:20:00',
                     'reason': '补充事件前后保存过程，以判断排出受限与来水的同期关系。'}, **kwargs)

    def test_new_precision_applies_before_supplement_content_hash(self):
        self.package['numeric_precision'] = dict(PRECISION_POLICY)
        write_package(self.root, self.package)
        path = self.root / 'swmm/links.tsv'
        path.write_text(path.read_text(encoding='utf-8').replace('\t0.1\n', '\t0.123456789\n'), encoding='utf-8')
        before = path.read_bytes()
        plan = plan_request(self.root, self.package['evidence_rows'], self.request())
        result = extract(self.root, plan)
        row = result['evidence_rows'][0]
        self.assertEqual(result['numeric_precision'], PRECISION_POLICY)
        self.assertEqual(row['value']['series'][1]['links']['L1']['depth_m'], .123)
        self.assertEqual(row['evidence_id'], 'SUP_' + digest({k: v for k, v in row.items() if k != 'evidence_id'})[:24])
        self.assertEqual(path.read_bytes(), before)

    async def test_saved_samples_revision_and_immutable_initial_report(self):
        source = (self.root / 'evidence/evidence_package.json').read_bytes()
        request = self.request()
        first = await advance_investigation('m', 'r', 'initial', complete=AsyncMock(side_effect=lambda p: answer(p, [request, copy.deepcopy(request)])))
        self.assertEqual(first['state'], 'awaiting_evidence_confirmation')
        folder = self.root / 'diagnosis_tasks' / first['task_id']
        initial_report = Path(first['report_file']).read_bytes()
        snapshot = (folder / 'evidence_snapshot.json').read_bytes()
        self.assertEqual(list(folder.glob('supplement_*.json')), [])
        observed = []

        async def revised(prompt):
            rows = [r for r in prompt['visible_evidence'] if r['evidence_id'].startswith('SUP_')]
            observed.extend(rows)
            self.assertEqual(len(rows[0]['value']['series']), 21)
            self.assertEqual(rows[0]['value']['series'][10]['links']['L1']['depth_reference_ratio'], 1)
            self.assertEqual(rows[0]['value']['series'][10]['links']['L1']['flow_Ls'], -10)
            return answer(prompt, [request])  # repeated request must close without new work

        second = await advance_investigation('m', 'r', 'continue', first['task_id'], complete=revised)
        self.assertEqual(second['revision'], 2)
        self.assertEqual(second['state'], 'preliminary_delivered')
        self.assertEqual(second['evidence_plan'][0]['status'], 'already_visible')
        self.assertEqual(Path(first['report_file']).read_bytes(), initial_report)
        self.assertEqual((folder / 'evidence_snapshot.json').read_bytes(), snapshot)
        self.assertEqual((self.root / 'evidence/evidence_package.json').read_bytes(), source)
        self.assertEqual(len(list(folder.glob('supplement_*.json'))), 1)
        self.assertIn('综合机制判断', second['report'])
        self.assertEqual(second['diagnosis']['evidence_binding'], second['verification']['evidence_binding'])
        for _ in range(2):
            stopped = await advance_investigation('m', 'r', 'continue', first['task_id'],
                                                complete=AsyncMock(side_effect=AssertionError('no repeat')))
            self.assertEqual(stopped['revision'], 2)
        self.assertEqual(len(list(folder.glob('supplement_*.json'))), 1)
        self.assertIn(('link', 'L1'), row_subjects(observed[0]))
        source_path = self.root / 'swmm/links.tsv'
        saved = source_path.read_bytes()
        source_path.write_bytes(saved + b'\n')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            await advance_investigation('m', 'r', 'status', first['task_id'])
        source_path.write_bytes(saved)
        (folder / second['supplements'][0]['file']).write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Immutable supplement changed'):
            await advance_investigation('m', 'r', 'status', first['task_id'])

    async def test_legacy_closed_task_replans_and_semicolon_events_work(self):
        request = {'tool': 'unavailable', 'metric_name': 'fullness_time_series',
                   'event_id': 'N1:E001;N1:E002', 'object_type': 'link', 'reason': '检查直接管段满度'}
        first = await advance_investigation('m', 'r', 'initial', complete=AsyncMock(side_effect=lambda p: answer(p, [request])))
        folder = self.root / 'diagnosis_tasks' / first['task_id']
        path = folder / 'task.json'
        stored = json.loads(path.read_text(encoding='utf-8'))
        stored.update(state='preliminary_delivered', pending_evidence_requests=False, evidence_closure='capability_gaps')
        path.write_text(json.dumps(stored), encoding='utf-8')
        second = await advance_investigation('m', 'r', 'continue', first['task_id'], complete=AsyncMock(side_effect=answer))
        self.assertEqual(second['revision'], 2)
        rows = json.loads((folder / second['supplements'][0]['file']).read_text(encoding='utf-8'))['evidence_rows']
        self.assertEqual({r['event_id'] for r in rows}, {'N1:E001', 'N1:E002'})
        self.assertEqual(rows[0]['value']['resolved_scope']['links'], ['L1'])

    def test_event_maximum_uses_requested_window_not_run_scalar(self):
        req = self.request(metric_name='max_fullness', time_end='2024-01-01 00:03:00')
        result = extract(self.root, plan_request(self.root, self.package['evidence_rows'], req))
        value = result['evidence_rows'][0]['value']
        self.assertEqual(value['maxima']['L1']['maximum'], .3)
        self.assertEqual(value['maxima']['L1']['times'], ['2024-01-01 00:03:00'])

    def test_explicit_supplement_can_cross_initial_confluence_boundary(self):
        from workflow_agents.evidence.event_packages import parse_run_input
        from workflow_agents.evidence.topology_scope import build_topology_scope
        path = self.root / 'swmm/model_with_event.inp'
        path.write_text(path.read_text(encoding='utf-8') +
                        '\n[JUNCTIONS]\nN3 0 1 0\nS 0 1 0\n'
                        '[CONDUITS]\nL2 N2 N3 10 .01 0 0\nSIDE S N2 10 .01 0 0\n'
                        '[XSECTIONS]\nL2 CIRCULAR 1 0 0 0\n', encoding='utf-8')
        scope = build_topology_scope(parse_run_input(path), 'N1')
        self.assertEqual(set(scope['link_roles']), {'L1'})
        self.assertEqual(scope['boundary_nodes']['N2'], ['confluence'])
        table = self.root / 'swmm/links.tsv'
        with table.open('a', encoding='utf-8') as stream:
            stream.writelines(f'L2\t2024-01-01\t00:{i:02}:00\t{i}\t0.5\n' for i in range(21))
        initial_package = (self.root / 'evidence/evidence_package.json').read_bytes()
        request = self.request(object_ids=['L2'])
        plan = plan_request(self.root, self.package['evidence_rows'], request)
        self.assertTrue(plan['requires_extraction'])
        supplement = extract(self.root, plan)
        self.assertEqual(supplement['evidence_rows'][0]['value']['resolved_scope']['links'], ['L2'])
        samples = supplement['evidence_rows'][0]['value']['series']
        self.assertEqual(len(samples), 21)
        self.assertEqual(set(samples[0]['links']), {'L2'})
        self.assertEqual((self.root / 'evidence/evidence_package.json').read_bytes(), initial_package)

    def test_finer_resolution_unknown_object_and_unknown_method_are_explicit(self):
        rows = self.package['evidence_rows']
        plan = plan_request(self.root, rows, self.request(sample_interval_seconds=10))
        self.assertEqual(plan['status'], 'unavailable')
        self.assertEqual(plan['next_investigation'], 'finer_saved_output_required')
        self.assertEqual(plan_request(self.root, rows, self.request(object_ids=['L999']))['status'], 'no_match')
        self.assertEqual(plan_request(self.root, rows, self.request(metric_name='counterfactual_causal_contribution'))['status'], 'unavailable')
        with self.assertRaisesRegex(ValueError, 'Unsupported saved-result request'):
            plan_request(self.root, rows, self.request(code='print(1)'))

    def test_duplicate_requests_resolve_once_and_missing_is_not_zero(self):
        path = self.root / 'swmm/links.tsv'
        path.write_text(path.read_text(encoding='utf-8').replace('\t0.2\n', '\tNaN\n'), encoding='utf-8')
        req = self.request()
        result = extract(self.root, plan_request(self.root, self.package['evidence_rows'], req))
        row = result['evidence_rows'][0]
        self.assertIsNone(row['value']['series'][2]['links']['L1']['depth_reference_ratio'])
        self.assertEqual(row['value']['status'], 'partial')
        plan = evidence_plan(self.package['evidence_rows'] + [row], [row['evidence_id']], [req, copy.deepcopy(req)], self.root)
        self.assertEqual([p['status'] for p in plan], ['already_visible', 'already_visible'])

    def test_subset_of_completed_batch_is_reused_instead_of_false_progress(self):
        req = self.request(event_ids=['N1:E001', 'N1:E002'])
        result = extract(self.root, plan_request(self.root, self.package['evidence_rows'], req))
        rows = self.package['evidence_rows'] + result['evidence_rows']
        subset = self.request(event_ids=['N1:E002'])
        plan = evidence_plan(rows, [r['evidence_id'] for r in result['evidence_rows']], [subset], self.root)
        self.assertEqual(plan[0]['status'], 'already_visible')
        self.assertFalse(plan[0]['requires_extraction'])
        self.assertEqual(len(plan[0]['matching_evidence_ids']), 1)

    def test_iso_window_and_explicit_equivalent_default_scope_reuse_evidence(self):
        req = self.request()
        req.pop('time_start')
        req.pop('time_end')
        result = extract(self.root, plan_request(self.root, self.package['evidence_rows'], req))
        scope = result['evidence_rows'][0]['value']['resolved_scope']
        explicit = dict(req, time_start=scope['time_start'].replace(' ', 'T'),
                        time_end=scope['time_end'].replace(' ', 'T'))
        rows = self.package['evidence_rows'] + result['evidence_rows']
        plan = evidence_plan(rows, [r['evidence_id'] for r in result['evidence_rows']], [explicit], self.root)
        self.assertEqual(plan[0]['status'], 'already_visible')

    async def test_window_without_samples_closes_and_does_not_repeat_extraction(self):
        req = self.request(time_start='2024-01-01 01:00:00', time_end='2024-01-01 01:10:00')
        first = await advance_investigation('m', 'r', 'initial', complete=AsyncMock(side_effect=lambda p: answer(p, [req])))
        completion = AsyncMock(side_effect=AssertionError('No new evidence, no new diagnosis'))
        second = await advance_investigation('m', 'r', 'continue', first['task_id'], complete=completion)
        self.assertEqual(second['state'], 'preliminary_delivered')
        self.assertEqual(second['evidence_plan'][0]['status'], 'no_match')
        self.assertFalse(second['pending_evidence_requests'])
        self.assertEqual(len(second['supplemental_request_outcomes']), 1)
        repeated = await advance_investigation('m', 'r', 'continue', first['task_id'], complete=completion)
        self.assertEqual(len(repeated['supplemental_request_outcomes']), 1)
        self.assertEqual(repeated['revision'], 1)
        completion.assert_not_called()

    async def test_web_continue_only_supplements_without_diagnosis_or_report(self):
        import workflow_agents.orchestration.controller as web_app
        import web_app as http_api
        from workflow_agents.orchestration import sessions as session_service
        from workflow_agents.common.state import EVIDENCE_READY
        with patch('workflow_agents.reporting.pipeline.attach_display_report', new_callable=AsyncMock):
            first = await advance_investigation('m', 'r', 'initial', complete=AsyncMock(side_effect=lambda p: answer(p, [self.request()])))
        # Exercise the web's durable task routing, not a mocked evidence result.
        with patch('workflow_agents.orchestration.controller.load_or_initialize_state', return_value=(self.root, {'state': EVIDENCE_READY})), \
             patch('workflow_agents.common.state.resolve_run_root', return_value=self.root), \
             patch('workflow_agents.diagnosis.service.configured_completion', new_callable=AsyncMock) as completion:
            transcript = [{'role': 'assistant', 'content': f'model_name=m run_id=r task_id={first["task_id"]} awaiting_evidence_confirmation'},
                          {'role': 'user', 'content': '继续'}]
            reply = await web_app.run_orchestrator_turn(transcript)
        completion.assert_not_awaited()
        self.assertIn('本轮只执行', reply)
        folder = self.root / 'diagnosis_tasks' / first['task_id']
        stored = json.loads((folder / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['revision'], 1)
        self.assertEqual(stored['state'], 'ready_for_diagnosis')
        self.assertEqual(len(stored['supplements']), 1)

    def test_native_volume_changes_no_simulation_and_source_change_rejected(self):
        (self.root / 'swmm/model.out').write_bytes(b'saved')
        native = {'nodes': {n: {f'2024-01-01 00:0{i}:00': {'node_volume_m3': i, 'head_m': i+1} for i in range(4)}
                            for n in ('N1', 'N2')},
                  'links': {'L1': {f'2024-01-01 00:0{i}:00': {'link_volume_m3': i*2, 'depth_m': .2} for i in range(4)}}, 'metadata': {}}
        req = self.request(metric_name='storage_volume_process', object_type='node', object_ids=['N1'], time_end='2024-01-01 00:03:00')
        plan = plan_request(self.root, self.package['evidence_rows'], req)
        with patch('workflow_agents.evidence.saved_result_evidence._native', return_value=native):
            result = extract(self.root, plan)
        delta = result['evidence_rows'][0]['value']['interval_volume_changes'][0]
        self.assertEqual(delta['links']['L1']['delta_volume_m3'], 2)
        self.assertAlmostEqual(delta['nodes']['N1']['mean_storage_change_Ls'], 1000/60)
        self.assertEqual(plan['resolved_request']['scopes'][0]['nodes'], ['N1', 'N2'])

        def changed(*args):
            (self.root / 'swmm/model.out').write_bytes(b'changed')
            return native

        with patch('workflow_agents.evidence.saved_result_evidence._native', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed during extraction'):
                extract(self.root, plan)

    def test_native_units_are_verified_before_reading_series(self):
        from unittest.mock import MagicMock
        from workflow_agents.evidence.saved_result_evidence import _native
        output = MagicMock()
        output.__enter__.return_value.units = {'system': 'US', 'flow': 'CFS'}
        with patch('pyswmm.Output', return_value=output):
            with self.assertRaisesRegex(ValueError, 'not SI/LPS'):
                _native(self.root / 'swmm/model.out', ['N1'], ['L1'])
        output.__enter__.return_value.node_series.assert_not_called()

    def test_surface_samples_are_geometric_not_tracer_or_feedback(self):
        static = self.root.parents[1] / 'static'
        static.mkdir()
        (static / 'node_to_cell_mapping.csv').write_text('node_id,smid,distance_m\nN1,42,1.5\n', encoding='utf-8')
        (self.root / 'summary.json').write_text(json.dumps({'model_name': 'm', 'run_id': 'r', 'static_model': str(static),
            'simulation_timing': {'ca2d_save_interval_minutes': 5}}), encoding='utf-8')
        (self.root / 'ca2d').mkdir()
        (self.root / 'ca2d').mkdir(exist_ok=True)
        (self.root / 'ca2d/surface_depth.tsv').write_text('Smid\tDate\tTime\tDepth\n42\t2024-01-01\t00:00:00\t0\n42\t2024-01-01\t00:05:00\t.037\n42\t2024-01-01\t00:10:00\t0\n', encoding='utf-8')
        req = self.request(metric_name='ponding_depth_time_series', object_type='node', object_ids=['N1'], time_end='2024-01-01 00:10:00')
        result = extract(self.root, plan_request(self.root, self.package['evidence_rows'], req))
        row = result['evidence_rows'][0]
        self.assertEqual(row['value']['series'][1]['cells']['42']['depth_m'], .037)
        self.assertIn(('cell', '42'), row_subjects(row))
        self.assertTrue(any('单向' in s for s in row['value']['interpretation_limits']))
        self.assertIn(str(static / 'node_to_cell_mapping.csv'), result['source_hashes'])
        self.assertEqual(plan_request(self.root, self.package['evidence_rows'], dict(req, sample_interval_seconds=60))['status'], 'unavailable')

    async def test_joint_judgment_cannot_promote_candidate_or_cite_another_event(self):
        state = await advance_investigation('m', 'r')
        from workflow_agents.diagnosis.service import build_diagnosis_prompt
        rows = json.loads((self.root / 'diagnosis_tasks' / state['task_id'] / 'evidence_snapshot.json').read_text(encoding='utf-8'))
        rows = [r for r in rows if r['evidence_id'] in state['visible_ids']]
        result = answer(build_diagnosis_prompt(state, rows))
        item = result['event_assessments'][0]
        ref = item['evidence_ids'][0]
        for m in item['mechanism_assessments'][:2]:
            m.update(status='supported', evidence_ids=[ref])
        item['joint_assessment'].update(status='supported', mechanism_ids=['M1', 'M2'])
        validate_initial(result, state['event_manifest'], rows, require_joint=True)
        self.assertTrue(any(c['claim_id'].endswith(':JOINT') for c in initial_reference_claims(result)))
        item['mechanism_assessments'][1]['status'] = 'candidate'
        with self.assertRaisesRegex(ValueError, 'only include supported'):
            validate_initial(result, state['event_manifest'], rows, require_joint=True)
        item['joint_assessment']['status'] = 'candidate'
        item['joint_assessment']['evidence_ids'] = result['event_assessments'][1]['evidence_ids']
        with self.assertRaisesRegex(ValueError, 'own visible event'):
            validate_initial(result, state['event_manifest'], rows, require_joint=True)
