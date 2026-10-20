"""Regression coverage for the single evidence contract and removed routes."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from workflow_agents.evidence.evidence_builder import build_evidence_for_run
from workflow_agents.evidence.evidence_package import load_package
from workflow_agents.evidence.evidence_precision import PRECISION_POLICY
from workflow_agents.evidence.evidence_selection import EVENT_GROUPS
from workflow_agents.orchestration.investigation import advance_investigation
from workflow_agents.diagnosis.service import build_diagnosis_prompt
from workflow_agents.common.hashing import sha256_file
from workflow_agents.orchestration.run_preparation import run_workflow_stage
from workflow_agents.common.state import infer_state_from_artifacts, load_or_initialize_state


class UnifiedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'm/runs/r'
        (self.root / 'swmm').mkdir(parents=True)
        (self.root / 'ca2d').mkdir()
        (self.root / 'summary.json').write_text(json.dumps({'model_name': 'm', 'run_id': 'r', 'event_name': 'rain'}))
        (self.root / 'swmm/model_with_event.inp').write_text(
            '[OPTIONS]\nFLOW_UNITS LPS\n[JUNCTIONS]\nN0 9 3 0 0 0\nN1 8 3 0 0 0\nN2 7 3 0 0 0\n'
            '[CONDUITS]\nL1 N0 N1 100 .013 0 0\nL2 N1 N2 100 .013 0 0\n'
            '[XSECTIONS]\nL1 CIRCULAR 1 0 0 0 1\nL2 CIRCULAR 1 0 0 0 1\n')
        self.nodes = [dict(node_id=node, date='2024-01-01', time=f'00:0{i}:00', depth_m=.5,
                           flooding_Ls=[0, 2, 0, 3, 0][i] if node == 'N1' else 0)
                      for node in ('N0', 'N1', 'N2') for i in range(5)]
        self.write_tsv('swmm/nodes.tsv', self.nodes)
        self.write_tsv('swmm/links.tsv', [dict(link_id=link, date='2024-01-01', time=f'00:0{i}:00',
                       depth_m=.5, flow_Ls=[0, 10, -4, 10, 0][i]) for link in ('L1', 'L2') for i in range(5)])
        self.write_tsv('ca2d/surface_depth.tsv', [dict(Smid='1', Date='2024-01-01', Time=f'00:0{i}:00',
                       Depth=[0, .01, .02, 0, 0][i]) for i in range(5)])

    def tearDown(self):
        self.temp.cleanup()

    def write_tsv(self, name, rows):
        with (self.root / name).open('w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter='\t')
            writer.writeheader()
            writer.writerows(rows)

    def build(self):
        with patch('workflow_agents.evidence.evidence_builder.resolve_run_root', return_value=self.root):
            return build_evidence_for_run('m', 'r')

    @staticmethod
    def answer(claims=None):
        return {'question_assessments': [{'question_id': f'Q{i}', 'status': 'needs_evidence', 'reason': '有缺项'} for i in range(1, 5)],
                'mechanism_assessments': [{'mechanism_id': f'M{i}', 'status': 'not_investigated', 'reason': '尚未调查'} for i in range(1, 7)],
                'claims': claims or [], 'evidence_requests': [], 'stop_reason': '报告已取得事实和缺项'}

    def test_one_file_canonical_events_and_full_node_coverage(self):
        result = self.build()
        self.assertEqual([p.name for p in (self.root / 'evidence').iterdir()], ['evidence_package.json'])
        package = load_package(self.root, verify_sources=True)
        self.assertEqual(result['event_count'], 2)
        self.assertEqual(result['overflow_node_count'], 1)
        self.assertEqual(len(package['packages']), 2)
        rows = package['evidence_rows']
        for event in package['packages']:
            evidence = [r for r in rows if r['evidence_id'] in event['evidence_ids']]
            self.assertEqual({r['metric_name'] for r in evidence}, EVENT_GROUPS)
        self.assertNotIn('event_estimated_volume', {r['metric_name'] for r in rows})
        total = next(r for r in rows if r['object_id'] == 'N1' and r['metric_name'] == 'total_flooding_volume')
        self.assertAlmostEqual(total['value'], .3)
        self.assertEqual(total['source_file'], 'swmm/nodes.tsv')
        self.assertEqual(total['time_start'], '2024-01-01 00:00:00')
        self.assertEqual(total['time_end'], '2024-01-01 00:04:00')
        self.assertEqual({r['object_id'] for r in rows if r['metric_name'] == 'total_flooding_volume'}, {'N0', 'N1', 'N2'})

    def test_invalid_numeric_data_preserves_existing_package(self):
        self.build()
        before = (self.root / 'evidence/evidence_package.json').read_bytes()
        self.nodes[0]['depth_m'] = 'NaN'
        self.write_tsv('swmm/nodes.tsv', self.nodes)
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            self.build()
        self.assertEqual((self.root / 'evidence/evidence_package.json').read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'source changed'):
            load_package(self.root, verify_sources=True)

    async def test_rounding_happens_after_detection_extrema_and_threshold_counts(self):
        for row in self.nodes:
            row['depth_m'] = .4852335453033447
            if row['node_id'] == 'N1':
                row['flooding_Ls'] = [0, 2.000456789, 0, 1.0000001e-9, 0][int(row['time'][4])]
        self.write_tsv('swmm/nodes.tsv', self.nodes)
        self.write_tsv('ca2d/surface_depth.tsv', [dict(Smid='1', Date='2024-01-01', Time=f'00:0{i}:00',
                       Depth=depth) for i, depth in enumerate([0, .00500001, .4852335453033447, 0, 0])])
        paths = [self.root / name for name in ('swmm/nodes.tsv', 'swmm/model_with_event.inp', 'ca2d/surface_depth.tsv')]
        before = {path: sha256_file(path) for path in paths}
        self.build()
        package = load_package(self.root, verify_sources=True)
        self.assertEqual(package['numeric_precision'], PRECISION_POLICY)
        self.assertEqual(package['overview']['event_count'], 2)
        rows = package['evidence_rows']
        depth = next(r for r in rows if r['metric_name'] == 'max_depth')
        self.assertEqual(depth['value'], .485)
        self.assertEqual(depth['at_time'], '2024-01-01 00:02:00')
        ponding = next(r for r in rows if r['metric_name'] == 'ponding_duration')
        self.assertEqual(ponding['value'], 2)
        self.assertEqual(ponding['threshold'], .005)
        contexts = [r['value']['event'] for r in rows if r['metric_name'] == 'event_context']
        self.assertEqual(contexts[0]['estimated_volume_m3'], .12)
        self.assertEqual(contexts[1]['peak_flooding_Ls'], 1.0000001e-9)
        process = next(r for r in rows if r['metric_name'] == 'local_process_series')
        self.assertEqual(process['value']['series'][1]['nodes']['N1']['depth_m'], .485)
        self.assertEqual({path: sha256_file(path) for path in paths}, before)
        with patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root):
            state = await advance_investigation('m', 'r')
        self.assertEqual(len(state['event_manifest']), 2)
        snapshot = json.loads((self.root / 'diagnosis_tasks' / state['task_id'] / 'evidence_snapshot.json').read_text())
        self.assertEqual(build_diagnosis_prompt(state, snapshot)['numeric_precision'], PRECISION_POLICY)

    def test_no_surface_requires_explicit_simulation_status(self):
        (self.root / 'ca2d/surface_depth.tsv').unlink()
        with self.assertRaisesRegex(FileNotFoundError, 'NO_SURFACE_INFLOW'):
            self.build()
        summary = {'model_name': 'm', 'run_id': 'r', 'ca2d': {'status': 'NO_SURFACE_INFLOW'}}
        (self.root / 'summary.json').write_text(json.dumps(summary))
        self.assertEqual(self.build()['surface_status'], 'not_simulated_no_surface_inflow')

    async def test_prepare_only_uses_package_and_keeps_rainfall_context(self):
        self.build()
        # Neither of the old two evidence files exists here.
        with patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root):
            state = await advance_investigation('m', 'r', question='分析N1:E001')
        self.assertIn('CTX_RAINFALL', state['visible_ids'])
        snap = json.loads((self.root / 'diagnosis_tasks' / state['task_id'] / 'evidence_snapshot.json').read_text())
        self.assertEqual(len([r for r in snap if r.get('event_id') == 'N1:E001']), 6)

    async def test_full_task_cycle_from_merged_evidence(self):
        self.build()
        with patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root):
            state = await advance_investigation('m', 'r', question='分析N1:E001')
            ref = next(e for e in load_package(self.root)['evidence_rows'] if e['metric_name'] == 'event_context')
            claim = {'object_type': 'node', 'object_id': 'N1', 'event_id': 'N1:E001', 'question_id': 'Q1',
                     'claim_kind': 'fact', 'claim_text': '保存记录识别出本次冒溢。', 'evidence_ids': [ref['evidence_id']],
                     'engineering_reason': '事件目录', 'alternatives': '原因尚未核实', 'scope': '本次事件'}
            response = await advance_investigation('m', 'r', 'diagnose', state['task_id'], complete=AsyncMock(return_value=self.answer([claim])))
            self.assertEqual(response['state'], 'ready_for_verification')
            response = await advance_investigation('m', 'r', 'verify', state['task_id'])
            self.assertEqual(response['state'], 'ready_for_report')
            response = await advance_investigation('m', 'r', 'report', state['task_id'])
            self.assertIn(claim['claim_text'], response['report'])
            self.assertFalse((self.root / 'diagnosis').exists())

    async def test_empty_diagnosis_cannot_pass_completion(self):
        self.build()
        with patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root):
            state = await advance_investigation('m', 'r', question='分析N1')
            await advance_investigation('m', 'r', 'diagnose', state['task_id'], complete=AsyncMock(return_value=self.answer()))
            state = await advance_investigation('m', 'r', 'verify', state['task_id'])
            self.assertEqual(state['state'], 'revision_requested')
            with self.assertRaises(ValueError):
                await advance_investigation('m', 'r', 'report', state['task_id'])

    async def test_old_evidence_is_not_a_fallback(self):
        (self.root / 'evidence').mkdir()
        (self.root / 'evidence/evidence_table.csv').write_text('old')
        (self.root / 'evidence/first_pass_evidence.json').write_text('{}')
        with patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root):
            with self.assertRaisesRegex(FileNotFoundError, '重新构建'):
                await advance_investigation('m', 'r', question='分析N1')

    def test_old_completed_state_and_artifacts_do_not_count(self):
        (self.root / 'diagnosis').mkdir()
        (self.root / 'verification').mkdir()
        (self.root / 'diagnosis/diagnosis_claims.json').write_text('{}')
        (self.root / 'diagnosis/risk_ranking.csv').write_text('old')
        (self.root / 'verification/verification_report.json').write_text('{}')
        (self.root / 'verification/unsupported_rate.txt').write_text('0')
        # State readiness is based on the modern simulation outputs, never old verification.
        self.assertEqual(infer_state_from_artifacts(self.root), 'RUN_READY')
        (self.root / 'workflow_state.json').write_text(json.dumps({'schema_version': '0.1', 'state': 'VERIFIED_READY'}))
        with patch('workflow_agents.common.state.resolve_run_root', return_value=self.root):
            _, state = load_or_initialize_state('m', 'r')
        self.assertEqual(state['state'], 'RUN_READY')

    def test_rule_stage_is_removed(self):
        for stage in ('diagnosis', 'verification'):
            with self.assertRaisesRegex(ValueError, '已删除'):
                run_workflow_stage('m', 'r', target_stage=stage)

    def test_current_evidence_state_requires_the_actual_package(self):
        (self.root / 'workflow_state.json').write_text(json.dumps({
            'schema_version': '1.0', 'model_name': 'm', 'run_id': 'r', 'state': 'EVIDENCE_READY'}))
        with patch('workflow_agents.common.state.resolve_run_root', return_value=self.root):
            _, state = load_or_initialize_state('m', 'r')
        self.assertEqual(state['state'], 'RUN_READY')

    def test_obsolete_unified_package_can_be_rebuilt_without_false_ready_state(self):
        self.build()
        path = self.root / 'evidence/evidence_package.json'
        package = json.loads(path.read_text(encoding='utf-8'))
        package['schema_version'] = '1.0'
        path.write_text(json.dumps(package), encoding='utf-8')
        (self.root / 'workflow_state.json').write_text(json.dumps({
            'schema_version': '1.0', 'model_name': 'm', 'run_id': 'r', 'state': 'EVIDENCE_READY'}))
        with patch('workflow_agents.common.state.resolve_run_root', return_value=self.root):
            _, state = load_or_initialize_state('m', 'r')
        self.assertEqual(state['state'], 'RUN_READY')
        self.assertNotIn('evidence_building', state['completed_stages'])
        with patch('workflow_agents.common.state.resolve_run_root', return_value=self.root), \
             patch('workflow_agents.evidence.evidence_builder.resolve_run_root', return_value=self.root):
            response = run_workflow_stage('m', 'r')
        self.assertTrue(response['ok'])
        self.assertEqual(load_package(self.root)['schema_version'], '1.1')

    def test_run_stage_builds_only_evidence_and_returns_task_preparation(self):
        state = {'model_name': 'm', 'run_id': 'r', 'state': 'RUN_READY'}
        with patch('workflow_agents.orchestration.run_preparation.load_or_initialize_state', return_value=(self.root, state)), \
             patch('workflow_agents.evidence.evidence_builder.resolve_run_root', return_value=self.root):
            response = run_workflow_stage('m', 'r')
        self.assertTrue(response['ok'])
        self.assertEqual(response['state']['state'], 'EVIDENCE_READY')
        self.assertEqual(response['next_action'], 'initial_diagnosis')
        self.assertEqual(response['state']['next_allowed_stage'], 'initial_diagnosis')
        self.assertFalse((self.root / 'diagnosis_tasks').exists())

    async def test_public_roles_have_no_old_fallback_or_code_agents(self):
        import workflow_agents.orchestration.agents as main
        import workflow_agents.simulation.runner as tools
        for name in ('LegacyTaskExecutor', 'CodeRunner', 'DataAnalyzer', 'WorkflowStageRunner'):
            self.assertFalse(hasattr(main, name))
        for name in ('diagnose_run', 'verify_run_diagnosis', 'generate_run_report', 'build_run_evidence_graph'):
            self.assertFalse(hasattr(tools, name))
        with self.assertRaisesRegex(ValueError, 'task_id'):
            await main.VerificationAgent('m', 'r')
        with patch('workflow_agents.orchestration.investigation_flow.load_active_task', return_value=None):
            with self.assertRaisesRegex(ValueError, '诊断与引用核查'):
                await main.ReportAgent('m', 'r')

    async def test_unbound_web_diagnosis_and_report_are_planning_only(self):
        import workflow_agents.orchestration.controller as web_app
        import web_app as http_api
        from workflow_agents.orchestration import sessions as session_service
        with patch('workflow_agents.orchestration.controller.run_web_orchestrator_agent_turn', new_callable=AsyncMock, return_value='task route') as role:
            for question in ('立即诊断', '立即核查', '立即生成报告'):
                result = await web_app.run_orchestrator_turn([{'role': 'user', 'content': question}])
                self.assertEqual(result, 'task route')
            self.assertEqual(role.await_count, 3)
            self.assertTrue(all(call.kwargs['planning_only'] for call in role.await_args_list))
            self.assertFalse(hasattr(web_app, 'deterministic_workflow_stage_evidence'))
