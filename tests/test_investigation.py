import json
from evidence_fixtures import save_fixture
import tempfile
import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from workflow_agents.orchestration.investigation import advance_investigation
from workflow_agents.diagnosis.service import configured_completion


def answer(ref='E0', requests=None):
    return {
        'question_assessments': [{'question_id': f'Q{i}', 'status': 'needs_evidence', 'reason': '需要过程证据'} for i in range(1, 5)],
        'mechanism_assessments': [{'mechanism_id': f'M{i}', 'status': 'not_investigated', 'reason': '当前尚无机制证据'} for i in range(1, 7)],
        'claims': [{'object_type': 'node', 'object_id': 'N1', 'question_id': 'Q1', 'claim_kind': 'fact',
                    'claim_text': '节点存在冒溢记录', 'evidence_ids': [ref],
                    'engineering_reason': '引用保存结果', 'alternatives': '尚需比较来水与排出条件', 'scope': '本次运行N1'}],
        'evidence_requests': requests or [], 'stop_reason': '本轮形成事实记录，机制仍需过程证据'}


class InvestigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'm/runs/r'
        (self.root / 'evidence').mkdir(parents=True)
        rows = [{'evidence_id': f'E{i}', 'object_type': 'node', 'object_id': 'N1',
                 'source_model': 'm', 'run_id': 'r', 'event_id': 'N1:E001',
                 'metric_name': 'flooding', 'value': str(i)} for i in range(65)]
        rows.append(dict(rows[0], evidence_id='E_other', object_id='N2', event_id='N2:E001'))
        save_fixture(self.root, rows)
        self.patcher = patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=self.root)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    async def prepare(self):
        state = await advance_investigation('m', 'r', question='分析N1')
        self.task = state['task_id']
        return state

    async def step(self, action, complete=None, question=''):
        return await advance_investigation('m', 'r', action, self.task, question, complete=complete)

    async def test_prepare_no_network_and_bound_scope(self):
        with patch('workflow_agents.diagnosis.service.configured_completion', new_callable=AsyncMock) as call:
            state = await self.prepare()
        call.assert_not_called()
        self.assertEqual(state['state'], 'ready_for_diagnosis')
        self.assertEqual(len(state['visible_ids']), 65)
        self.assertIn('E64', state['visible_ids'])
        self.assertNotIn('E_other', state['visible_ids'])
        self.assertEqual(state['evidence_selection']['selection_basis'], 'task_scope_and_event_hydraulic_context')
        with self.assertRaises(ValueError):
            await self.step('report')
        with self.assertRaises(ValueError):
            await self.step('diagnose', question='分析另一个问题')

    async def test_full_revision_cycle(self):
        await self.prepare()
        response = await self.step('diagnose', AsyncMock(return_value=answer('MISSING')))
        self.assertEqual(response['state'], 'ready_for_verification')
        response = await self.step('verify')
        self.assertEqual(response['state'], 'revision_requested')
        fixed = AsyncMock(return_value=answer())
        response = await self.step('diagnose', fixed)
        self.assertIn('MISSING', json.dumps(fixed.call_args.args[0]['verification_feedback']))
        self.assertEqual(response['revision'], 2)
        self.assertNotIn('verification', response)
        self.assertIn('previous_verification', response)
        response = await self.step('verify')
        self.assertEqual(response['state'], 'ready_for_report')
        response = await self.step('report')
        self.assertIn('[D2:C1]', response['report'])
        self.assertNotIn('MISSING', response['report'])
        self.assertFalse((self.root / 'diagnosis/diagnosis_claims.json').exists())

    async def test_requested_evidence_is_not_executed_until_confirmed(self):
        await self.prepare()
        req = {'tool': 'evidence_lookup', 'object_id': 'N2', 'metric_name': 'flooding',
               'reason': '补充N2事件指标以调查相关过程'}
        state = await self.step('diagnose', AsyncMock(return_value=answer(requests=[req])))
        self.assertEqual(state['state'], 'ready_for_verification')
        self.assertNotIn('E_other', state['visible_ids'])
        with self.assertRaises(ValueError):
            await self.step('diagnose', AsyncMock(return_value=answer()))
        with self.assertRaises(ValueError):
            await self.step('evidence')
        await self.step('verify')
        state = await self.step('report')
        self.assertEqual(state['state'], 'awaiting_evidence_confirmation')
        self.assertIn('补充N2事件指标', state['report'])
        state = await self.step('evidence')
        self.assertIn('E_other', state['visible_ids'])
        updated = answer('E_other')
        updated['claims'][0]['object_id'] = 'N2'
        read_new = AsyncMock(return_value=updated)
        await self.step('diagnose', read_new)
        self.assertEqual(len(read_new.call_args.args[0]['visible_evidence']), 66)

    async def test_unseen_reference_does_not_pass(self):
        await self.prepare()
        await self.step('diagnose', AsyncMock(return_value=answer('E_other')))
        state = await self.step('verify')
        self.assertEqual(state['state'], 'revision_requested')

    async def test_invalid_json_shape_preserves_state(self):
        await self.prepare()
        with self.assertRaises(ValueError):
            await self.step('diagnose', AsyncMock(return_value={'claims': []}))
        state = json.loads((self.root / 'diagnosis_tasks' / self.task / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(state['revision'], 0)
        self.assertEqual(state['state'], 'ready_for_diagnosis')

    async def test_unavailable_request_finishes_without_repeated_diagnosis(self):
        await self.prepare()
        req = {'tool': 'unavailable', 'object_id': 'N1', 'metric_name': 'head_time_series', 'reason': '区分局部约束和下游影响'}
        complete = AsyncMock(return_value=answer(requests=[req]))
        await self.step('diagnose', complete)
        await self.step('verify')
        state = await self.step('report')
        self.assertEqual(state['state'], 'preliminary_delivered')
        self.assertEqual(state['evidence_closure'], 'capability_gaps')
        for _ in range(2):
            state = await self.step('continue', complete)
        complete.assert_awaited_once()
        self.assertEqual(state['revision'], 1)

    async def test_snapshot_and_claim_tampering_rejected(self):
        await self.prepare()
        await self.step('diagnose', AsyncMock(return_value=answer()))
        await self.step('verify')
        folder = self.root / 'diagnosis_tasks' / self.task
        (folder / 'diagnosis.json').write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            await self.step('report')
        (folder / 'evidence_snapshot.json').write_text('[]', encoding='utf-8')
        with self.assertRaises(ValueError):
            await self.step('verify')

    async def test_task_path_and_event_validation(self):
        with self.assertRaises(ValueError):
            await advance_investigation('m', 'r', 'diagnose', '../bad')
        with self.assertRaises(ValueError):
            await advance_investigation('m', 'r', question='分析N1:E009')

    async def test_model_adapter_with_fake_client(self):
        client = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(content=json.dumps(answer()))))
        with patch.dict(sys.modules, {'workflow_agents.common.llm': SimpleNamespace(deepseek_flash=client)}):
            result = await configured_completion({'original_question': '分析N1'})
        self.assertEqual(result['claims'][0]['evidence_ids'], ['E0'])
        self.assertTrue(client.create.call_args.kwargs['json_output'])

    async def test_unsupported_mechanism_status_rejected(self):
        await self.prepare()
        result = answer()
        result['mechanism_assessments'][0]['status'] = 'not_applicable'
        with self.assertRaises(ValueError):
            await self.step('diagnose', AsyncMock(return_value=result))

    async def test_public_role_dispatch(self):
        import workflow_agents.orchestration.agents as main
        dispatcher = AsyncMock(return_value='task result')
        with patch('workflow_agents.orchestration.agent_actions._investigation_action', dispatcher):
            await main.DiagnosisAgent('m', 'r')
            dispatcher.assert_awaited_with('m', 'r', 'initial', '', '')
            await main.DiagnosisAgent('m', 'r', message='分析N1')
            dispatcher.assert_awaited_with('m', 'r', 'prepare', '', '分析N1')
            await main.DiagnosisAgent('m', 'r', task_id='task')
            dispatcher.assert_awaited_with('m', 'r', 'diagnose', 'task', '')
            await main.EvidenceBuilderAgent('m', 'r', task_id='task')
            dispatcher.assert_awaited_with('m', 'r', 'evidence', 'task')
            await main.VerificationAgent('m', 'r', task_id='task')
            dispatcher.assert_awaited_with('m', 'r', 'verify', 'task')
            with patch('workflow_agents.orchestration.investigation_flow.load_active_task', return_value={'task_id':'task','state':'ready_for_report'}):
                await main.ReportAgent('m', 'r', task_id='task')
            dispatcher.assert_awaited_with('m', 'r', 'report', 'task')


if __name__ == '__main__':
    unittest.main()
