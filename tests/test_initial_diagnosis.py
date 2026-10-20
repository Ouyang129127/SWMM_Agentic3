"""Initial delivery, cited nested bindings, failure retention and dry-run termination."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evidence_fixtures import save_fixture
from workflow_agents.evidence.evidence_package import write_package
from workflow_agents.evidence.evidence_selection import EVENT_GROUPS
from workflow_agents.orchestration.investigation import advance_investigation
from workflow_agents.diagnosis.service import _validate_response
from workflow_agents.diagnosis.contract import apply_reviewed_reference_corrections


def fixture(root, count=2, terminal=False):
    rows, indexes = [], []
    for i in range(count):
        eid = f'N1:E{i+1:03}'
        facts = {'event_id': eid, 'node_id': 'N1', 'model_name': 'm', 'run_id': 'r',
                 'start': f'2024-01-01 00:0{i*2}:00', 'end': f'2024-01-01 00:0{i*2+1}:00',
                 'last_positive_time': f'2024-01-01 00:0{i*2}:00', 'peak_time': f'2024-01-01 00:0{i*2}:00',
                 'peak_flooding_Ls': 2, 'duration_minutes': 0 if terminal else 1,
                 'estimated_volume_m3': 0 if terminal else .12, 'saved_step_seconds': 60,
                 'left_censored': False, 'right_censored': terminal, 'integration_method': 'left_sample_rectangle'}
        ids = []
        for metric in sorted(EVENT_GROUPS):
            ref = eid + ':' + metric
            value = {'status': 'not_built'}
            if metric == 'event_context':
                value = {'event': facts}
            elif metric == 'local_structure':
                value = {'nodes': {'N1': {}, 'N2': {}}, 'links': {'L1': {}}}
            elif metric == 'local_process_series':
                value = {'series': [{'time': facts['start'], 'nodes': {'N1': {'head_m': None}},
                                     'links': {'L1': {'signed_model_flow_Ls': -2}}}]}
            rows.append({'evidence_id': ref, 'source_model': 'm', 'run_id': 'r', 'event_id': eid,
                         'object_type': 'node', 'object_id': 'N1', 'metric_name': metric, 'value': value, 'unit': 'structured'})
            ids.append(ref)
        indexes.append({'event_id': eid, 'node_id': 'N1', 'evidence_ids': ids})
    for node in ('N1', 'N2'):
        for metric, value in [('overflow_event_count', count if node == 'N1' else 0),
                              ('total_flooding_volume', count * .12 if node == 'N1' and not terminal else 0),
                              ('max_flooding_flow', 2 if count and node == 'N1' else 0)]:
            rows.append({'evidence_id': node + ':' + metric, 'source_model': 'm', 'run_id': 'r',
                         'object_type': 'node', 'object_id': node, 'event_id': '', 'metric_name': metric, 'value': value})
    p = save_fixture(root, rows, {'event_count': count, 'overflow_node_count': int(count > 0), 'node_count': 2})
    p['packages'] = indexes
    write_package(root, p)
    return p


def answer(prompt, requests=None):
    mechanisms = [{'mechanism_id': f'M{i}', 'status': 'needs_evidence', 'reason': '缺完整水头／过程证据', 'evidence_ids': []} for i in range(1, 7)]
    return {'question_assessments': [{'question_id': f'Q{i}', 'status': 'answered', 'reason': '事件事实已保存'} for i in range(1, 5)],
            'mechanism_assessments': copy.deepcopy(mechanisms), 'claims': [], 'evidence_requests': requests or [],
            'run_overview': {'text': '本场记录存在冒溢，机制仍有证据缺口。',
                             'evidence_ids': [next(r['evidence_id'] for r in prompt['visible_evidence'] if r['metric_name'] == 'overflow_event_overview')]},
            'event_assessments': [{'event_id': e['event_id'], 'node_id': e['node_id'],
                'process_explanation': '保存时序中存在反向流，水头缺失，不能完成约束／顶托区分。',
                'evidence_ids': [e['event_id'] + ':local_process_series'],
                'mechanism_assessments': copy.deepcopy(mechanisms), 'alternatives': '局部约束与下游影响可能共同作用。',
                'joint_assessment': {'status': 'needs_evidence', 'mechanism_ids': [],
                    'conclusion': '现有证据不足以确定共同作用机制。', 'interaction_explanation': '水头缺失，不能连接来水和排出的过程关系。',
                    'evidence_ids': [e['event_id'] + ':local_process_series'], 'limitations': '候选解释尚未区分。'},
                'evidence_gaps': ['需要同一时段上下游水头，以区分约束和顶托。']} for e in prompt['event_manifest']],
            'stop_reason': '先交付事件事实与初步解释，缺项待补。'}


class InitialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'm/runs/r'
        (self.root / 'evidence').mkdir(parents=True)
        self.package = fixture(self.root)
        self.patcher = patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    async def test_default_task_and_all_events_delivered_before_evidence(self):
        before = (self.root / 'evidence/evidence_package.json').read_bytes()
        complete = AsyncMock(side_effect=lambda p: answer(p, [{'tool': 'unavailable', 'object_id': 'N1', 'reason': '需要更长水头时序以区分机制'}]))
        state = await advance_investigation('m', 'r', 'initial', complete=complete)
        self.assertIsNone(state['task_context']['original_question'])
        self.assertEqual(state['state'], 'preliminary_delivered')
        self.assertEqual(state['evidence_closure'], 'capability_gaps')
        self.assertEqual(state['report_revision'], 1)
        self.assertTrue(Path(state['report_file']).is_file())
        self.assertIn('N1:E001', state['report'])
        self.assertIn('N1:E002', state['report'])
        self.assertIn('0.240000 m³', state['report'])
        self.assertIn('需要更长水头时序', state['report'])
        self.assertEqual((self.root / 'evidence/evidence_package.json').read_bytes(), before)
        complete.assert_awaited_once()
        revised = await advance_investigation('m', 'r', 'continue', state['task_id'], complete=complete)
        self.assertEqual(revised['state'], 'preliminary_delivered')
        complete.assert_awaited_once()
        folder = self.root / 'diagnosis_tasks' / state['task_id']
        self.assertTrue((folder / 'report_r1.json').exists())
        self.assertEqual(json.loads((folder / 'task.json').read_text(encoding='utf-8'))['report_revision'], 1)

    async def test_per_event_requests_are_not_rejected_by_an_arbitrary_count(self):
        requests = [{'tool': 'unavailable', 'object_id': 'N1', 'metric_name': f'variable_{i}',
                     'reason': '需要对应变量以区分事件机制'} for i in range(9)]
        state = await advance_investigation('m', 'r', 'initial', complete=AsyncMock(side_effect=lambda p: answer(p, requests)))
        self.assertEqual(len(state['diagnosis']['evidence_requests']), 9)
        self.assertEqual(state['state'], 'preliminary_delivered')
        self.assertTrue(Path(state['report_file']).exists())

    async def test_missing_or_duplicate_event_is_retained_as_failed_attempt(self):
        state = await advance_investigation('m', 'r')
        for duplicate in (False, True):
            async def incomplete(prompt):
                response = answer(prompt)
                if duplicate:
                    response['event_assessments'][1] = copy.deepcopy(response['event_assessments'][0])
                else:
                    response['event_assessments'].pop()
                return response
            with self.assertRaisesRegex(ValueError, 'every event exactly once'):
                await advance_investigation('m', 'r', 'diagnose', state['task_id'], complete=incomplete)
        folder = self.root / 'diagnosis_tasks' / state['task_id']
        audits = [json.loads(p.read_text(encoding='utf-8')) for p in folder.glob('diagnosis_attempt_*.json')]
        self.assertEqual(len(audits), 2)
        self.assertTrue(all(a['status'] == 'failed' and a['phase'] == 'validation' and a['parsed_response'] for a in audits))
        saved = json.loads((folder / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['revision'], 0)
        self.assertEqual(saved['last_attempt']['status'], 'failed')
        self.assertFalse((folder / 'diagnosis.json').exists())

    async def test_empty_provider_response_retained_before_json_failure(self):
        state = await advance_investigation('m', 'r')
        client = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(content='', finish_reason='length',
                                      usage=SimpleNamespace(prompt_tokens=42, completion_tokens=99))))
        with patch.dict('sys.modules', {'workflow_agents.common.llm': SimpleNamespace(deepseek_flash=client)}):
            with self.assertRaises(json.JSONDecodeError):
                await advance_investigation('m', 'r', 'diagnose', state['task_id'])
        folder = self.root / 'diagnosis_tasks' / state['task_id']
        audit = json.loads(next(folder.glob('diagnosis_attempt_*.json')).read_text(encoding='utf-8'))
        self.assertEqual(audit['raw_response'], '')
        self.assertEqual(audit['finish_reason'], 'length')
        self.assertEqual(audit['phase'], 'parsing')
        self.assertEqual(audit['usage']['completion_tokens'], 99)

    async def test_dry_run_delivers_without_model_call(self):
        fixture(self.root, count=0)
        complete = AsyncMock()
        state = await advance_investigation('m', 'r', 'initial', complete=complete)
        complete.assert_not_called()
        self.assertEqual(state['state'], 'preliminary_delivered')
        self.assertIn('未识别冒溢', state['report'])
        self.assertEqual(state['diagnosis']['event_assessments'], [])

    async def test_dry_index_with_positive_node_peak_is_rejected(self):
        p = fixture(self.root, count=0)
        next(r for r in p['evidence_rows'] if r['evidence_id'] == 'N1:max_flooding_flow')['value'] = 2
        write_package(self.root, p)
        with self.assertRaisesRegex(ValueError, 'flooding peak'):
            await advance_investigation('m', 'r')

    async def test_terminal_positive_sample_with_zero_integral_is_valid(self):
        fixture(self.root, count=1, terminal=True)
        state = await advance_investigation('m', 'r', 'initial', complete=AsyncMock(side_effect=answer))
        self.assertIn('结束未观测', state['report'])
        self.assertIn('0.000000 m³', state['report'])

    async def test_unrelated_known_event_cannot_support_mechanism(self):
        state = await advance_investigation('m', 'r')
        async def bad(prompt):
            result = answer(prompt)
            m = result['event_assessments'][0]['mechanism_assessments'][0]
            m.update(status='supported', evidence_ids=['N1:E002:local_process_series'])
            return result
        with self.assertRaisesRegex(ValueError, 'must cite its event'):
            await advance_investigation('m', 'r', 'diagnose', state['task_id'], complete=bad)

    async def test_explicit_review_revises_without_retrieval_and_keeps_first_report(self):
        state = await advance_investigation('m', 'r', 'initial', complete=AsyncMock(side_effect=answer))
        feedback = {'issues': [{'message': '复核同一时刻的变量，不混用邻近样本。',
                                'evidence_ids': ['N1:E001:local_process_series']}]}
        reviewed = await advance_investigation('m', 'r', 'review', state['task_id'], review_feedback=feedback)
        self.assertEqual(reviewed['state'], 'revision_requested')
        complete = AsyncMock(side_effect=answer)
        revised = await advance_investigation('m', 'r', 'diagnose', state['task_id'], complete=complete)
        self.assertIn('复核同一时刻', json.dumps(complete.call_args.args[0]['verification_feedback'], ensure_ascii=False))
        self.assertEqual(revised['revision'], 2)
        self.assertIn('previous_report', revised)
        self.assertNotIn('report', revised)
        self.assertTrue(Path(state['report_file']).exists())
        self.assertFalse(any(h['action'] == 'evidence' for h in revised['history']))

    async def test_report_event_facts_cannot_be_changed_in_task_state(self):
        state = await advance_investigation('m', 'r')
        folder = self.root / 'diagnosis_tasks' / state['task_id']
        state['event_manifest'][0]['facts']['estimated_volume_m3'] = 99
        (folder / 'task.json').write_text(json.dumps(state), encoding='utf-8')
        complete = AsyncMock(side_effect=answer)
        with self.assertRaisesRegex(ValueError, 'outside the frozen snapshot'):
            await advance_investigation('m', 'r', 'diagnose', state['task_id'], complete=complete)
        complete.assert_not_called()


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.rows = [dict(evidence_id='S1', object_type='node', object_id='N1', event_id='N1:E001',
                         metric_name='local_structure', value={'nodes': {'N1': {}, 'N2': {}}, 'links': {'L1': {}}}),
                     dict(evidence_id='S2', object_type='node', object_id='N3', event_id='N3:E001',
                         metric_name='local_structure', value={'nodes': {'N3': {}}, 'links': {'L2': {}}}),
                     dict(evidence_id='B', object_type='link', object_id='L1', event_id='', metric_name='max_flow', value=2)]

    def check(self, kind='link', obj='L1', event='N1:E001', refs=None):
        r = {'question_assessments': [{'question_id': f'Q{i}', 'status': 'answered', 'reason': '保存事实'} for i in range(1, 5)],
             'mechanism_assessments': [{'mechanism_id': f'M{i}', 'status': 'not_investigated', 'reason': '尚未调查'} for i in range(1, 7)],
             'claims': [{'object_type': kind, 'object_id': obj, 'event_id': event, 'question_id': 'Q1', 'claim_kind': 'clue',
                         'claim_text': '相关过程', 'engineering_reason': '过程证据', 'alternatives': '未确定机制', 'scope': '该事件',
                         'evidence_ids': refs if refs is not None else ['S1']}], 'evidence_requests': [], 'stop_reason': '初步解释'}
        _validate_response(r, self.rows)

    def test_nested_link_and_node_bound_to_target_event_pass(self):
        self.check()
        self.check('node', 'N2')

    def test_direct_composition_declares_links_without_a_source_type_field(self):
        self.rows.append(dict(evidence_id='C', object_type='node', object_id='N1', event_id='N1:E001',
                              metric_name='direct_source_composition', value={'sources': [
                                  {'source_id': 'L1', 'peak_entering_Ls': 2}, {'source_id': 'aggregate_lateral'}]}))
        self.check(refs=['C'])
        with self.assertRaises(ValueError):
            self.check(obj='aggregate_lateral', refs=['C'])

    def test_run_summary_can_cite_its_own_node_record_but_not_another_run(self):
        for row in self.rows:
            row['run_id'] = 'r'
        self.check(kind='run', obj='r', event=None)
        with self.assertRaises(ValueError):
            self.check(kind='run', obj='other', event=None)

    def test_arbitrary_object_wrong_event_unrelated_citation_rejected(self):
        for kwargs in ({'obj': 'invented'}, {'event': 'N3:E001'}, {'refs': ['S2']}, {'refs': ['B']}, {'event': 'N1:E999'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.check(**kwargs)

    def test_reviewed_correction_is_explicit_located_and_does_not_mutate_raw_response(self):
        result = {'evidence_ids': ['S1_typo'], 'claim_text': 'S1_typo is not rewritten in prose'}
        corrections = [{'invalid_id': 'S1_typo', 'evidence_id': 'S1', 'object_type': 'node', 'object_id': 'N1',
                        'event_id': 'N1:E001', 'metric_name': 'local_structure', 'reason': '人工复核定位拼写'}]
        corrected, counts = apply_reviewed_reference_corrections(result, corrections, self.rows, ['S1'])
        self.assertEqual(corrected['evidence_ids'], ['S1'])
        self.assertEqual(result['evidence_ids'], ['S1_typo'])
        self.assertEqual(corrected['claim_text'], result['claim_text'])
        self.assertEqual(counts, {'S1_typo': 1})
        corrections[0]['event_id'] = 'N3:E001'
        with self.assertRaisesRegex(ValueError, 'locator'):
            apply_reviewed_reference_corrections(result, corrections, self.rows, ['S1'])


if __name__ == '__main__':
    unittest.main()
