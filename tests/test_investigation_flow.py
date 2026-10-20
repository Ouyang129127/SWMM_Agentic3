"""Regression coverage for stuck web continuation and task failure recovery."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import workflow_agents.orchestration.controller as web_app
import web_app as http_api
from workflow_agents.orchestration import sessions as session_service
from test_initial_diagnosis import fixture, answer
from workflow_agents.orchestration.investigation import advance_investigation
from workflow_agents.orchestration.investigation_flow import load_active_task
from workflow_agents.common.state import build_state, save_state


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.models = Path(self.temp.name)
        self.root = self.models / 'm/runs/r'
        (self.root / 'evidence').mkdir(parents=True)
        fixture(self.root)
        self.patches = [patch('workflow_agents.common.state.MODELS_DIR', self.models),
                        patch('workflow_agents.orchestration.controller.MODELS_DIR', self.models)]
        for p in self.patches:
            p.start()
        save_state(self.root, build_state('m', 'r', self.root, state='EVIDENCE_READY'))

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def transcript(self, task_id='', latest='继续', old=False):
        text = '模型 m，run_id: r。统一证据已准备。'
        if task_id:
            text += f' task_id `{task_id}`'
        messages = [{'role': 'assistant', 'content': text}]
        if old:
            messages += [{'role': 'assistant', 'content': '下一步由DiagnosisAgent进行诊断。'}] * 14
        return messages + [{'role': 'user', 'content': latest}]

    async def initial(self, requests=None):
        with patch('workflow_agents.reporting.pipeline.attach_display_report', new_callable=AsyncMock):
            return await advance_investigation('m', 'r', 'initial',
                        complete=AsyncMock(side_effect=lambda p: answer(p, requests)))

    async def test_continue_only_prepares_initial_even_after_long_chat(self):
        complete = AsyncMock(side_effect=lambda prompt, audit=None: answer(prompt))
        with patch('workflow_agents.diagnosis.service.configured_completion', complete), \
             patch('workflow_agents.orchestration.controller.run_web_orchestrator_agent_turn', new_callable=AsyncMock) as llm:
            reply = await web_app.run_orchestrator_turn(self.transcript(old=True))
        llm.assert_not_called()
        complete.assert_not_awaited()
        state = load_active_task('m', 'r')
        self.assertEqual(state['state'], 'ready_for_diagnosis')
        self.assertIn('尚未调用诊断模型', reply)
        self.assertIn(state['task_id'], reply)
        self.assertEqual([h['action'] for h in state['history']], ['prepare'])

    async def test_report_does_not_overwrite_concurrent_task_change(self):
        state=await self.initial()
        path=self.root/'diagnosis_tasks'/state['task_id']/'task.json'
        async def changed(*args):
            latest=json.loads(path.read_text(encoding='utf-8'))
            latest['concurrent_marker']='newer operation'
            path.write_text(json.dumps(latest),encoding='utf-8')
        with patch('workflow_agents.reporting.pipeline.attach_display_report',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'Task changed during HTML'):
                await advance_investigation('m','r','display_report',state['task_id'])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['concurrent_marker'],'newer operation')

    async def test_validation_failure_is_corrected_in_same_task_with_original_response(self):
        prompts = []
        async def complete(prompt):
            prompts.append(copy.deepcopy(prompt))
            result = answer(prompt)
            if len(prompts) == 1:
                result['claims'] = [{'object_type': 'node', 'object_id': 'N1,N2', 'question_id': 'Q1',
                    'claim_kind': 'clue', 'claim_text': '两个节点综合说明', 'engineering_reason': '保存过程',
                    'alternatives': '待区分', 'scope': '本场', 'evidence_ids': ['N1:E001:local_process_series']}]
            return result
        state = await advance_investigation('m', 'r', 'initial', complete=complete)
        self.assertEqual(state['state'], 'preliminary_delivered')
        self.assertEqual(len(prompts), 2)
        feedback = prompts[1]['validation_feedback']
        self.assertIn('N1,N2', feedback['error'])
        self.assertEqual(feedback['previous_response']['claims'][0]['object_id'], 'N1,N2')
        self.assertEqual(len(list((self.root/'diagnosis_tasks').glob('*/task.json'))), 1)
        self.assertEqual(state['revision'], 1)
        folder = self.root/'diagnosis_tasks'/state['task_id']
        audits = [json.loads(p.read_text(encoding='utf-8')) for p in folder.glob('diagnosis_attempt_*.json')]
        self.assertEqual(sorted(a['status'] for a in audits), ['completed', 'failed'])

    async def test_repeated_invalid_response_returns_task_error_and_never_reports(self):
        complete = AsyncMock(return_value={'claims': []})
        state = await advance_investigation('m', 'r', 'initial', complete=complete)
        self.assertEqual(complete.await_count, 2)
        self.assertEqual(state['revision'], 0)
        self.assertIn('execution_error', state)
        self.assertFalse((self.root/'diagnosis_tasks'/state['task_id']/'report.json').exists())
        self.assertEqual(len(list((self.root/'diagnosis_tasks').glob('*/task.json'))), 1)

    async def test_provider_failure_is_reported_without_contract_repair_or_duplicate_task(self):
        complete = AsyncMock(side_effect=OSError('provider unavailable'))
        state = await advance_investigation('m', 'r', 'initial', complete=complete)
        complete.assert_awaited_once()
        self.assertEqual(state['execution_error'], 'provider unavailable')
        self.assertEqual(state['revision'], 0)
        self.assertEqual(state['last_attempt']['phase'], 'completion')
        self.assertEqual(len(list((self.root/'diagnosis_tasks').glob('*/task.json'))), 1)

    async def test_missing_reference_gets_verification_feedback_before_delivery(self):
        prompts = []
        async def complete(prompt):
            prompts.append(prompt)
            result = answer(prompt)
            result['claims'] = [{'object_type': 'node', 'object_id': 'N1', 'question_id': 'Q1',
                'claim_kind': 'fact', 'claim_text': '保存过程', 'engineering_reason': '保存过程',
                'alternatives': '待区分', 'scope': '本场',
                'evidence_ids': ['MISSING' if len(prompts) == 1 else 'N1:E001:local_process_series']}]
            return result
        state = await advance_investigation('m', 'r', 'initial', complete=complete)
        self.assertEqual(state['state'], 'preliminary_delivered')
        self.assertEqual(state['revision'], 2)
        self.assertIn('MISSING', json.dumps(prompts[1]['verification_feedback']))

    async def test_new_evidence_waits_until_continue_then_delivers_revision(self):
        requests = [{'tool': 'evidence_lookup', 'object_id': 'N2', 'metric_name': 'max_flooding_flow',
                     'reason': '比较相邻节点全场指标'}]
        state = await self.initial(requests)
        self.assertEqual(state['state'], 'awaiting_evidence_confirmation')
        self.assertNotIn('N2:max_flooding_flow', state['visible_ids'])
        before = Path(state['report_file']).read_bytes()
        complete = AsyncMock(side_effect=answer)
        revised = await advance_investigation('m', 'r', 'continue', state['task_id'], complete=complete)
        self.assertEqual(revised['state'], 'preliminary_delivered')
        self.assertEqual(revised['revision'], 2)
        self.assertIn('N2:max_flooding_flow', revised['visible_ids'])
        self.assertEqual(Path(state['report_file']).read_bytes(), before)
        complete.assert_awaited_once()

    async def test_unavailable_duplicate_and_missing_requests_finish_at_first_report(self):
        requests = [
            {'tool': 'unavailable', 'object_id': 'N1,N2', 'reason': '尚未生成储量过程'},
            {'tool': 'evidence_lookup', 'evidence_id': 'N1:E001:local_process_series', 'reason': '重复过程'},
            {'tool': 'evidence_lookup', 'object_id': 'N999', 'reason': '不存在对象'}]
        state = await self.initial(requests)
        self.assertEqual(state['state'], 'preliminary_delivered')
        self.assertEqual([p['status'] for p in state['evidence_plan']], ['unavailable', 'already_visible', 'no_match'])
        no_call = AsyncMock(side_effect=AssertionError('Must not repeat diagnosis'))
        for _ in range(3):
            repeated = await advance_investigation('m', 'r', 'continue', complete=no_call)
            self.assertEqual(repeated['task_id'], state['task_id'])
            self.assertEqual(repeated['revision'], 1)
        no_call.assert_not_called()

    async def test_legacy_waiting_task_closes_without_model_or_duplicate_task(self):
        state = await self.initial([{'tool': 'unavailable', 'reason': '缺少储量'},
                                   {'tool': 'evidence_lookup', 'evidence_id': 'N1:E001:local_process_series', 'reason': '复核已有过程'}])
        path = self.root/'diagnosis_tasks'/state['task_id']/'task.json'
        legacy = json.loads(path.read_text(encoding='utf-8'))
        legacy['state'] = 'awaiting_evidence_confirmation'
        for key in ('evidence_plan', 'evidence_closure', 'unresolved_evidence_requests'):
            legacy.pop(key, None)
        path.write_text(json.dumps(legacy), encoding='utf-8')
        (self.root/'diagnosis_tasks/active_task.json').unlink()
        with patch('workflow_agents.diagnosis.service.configured_completion', new_callable=AsyncMock) as llm:
            reply = await web_app.run_orchestrator_turn(self.transcript(state['task_id']))
            repeated = await web_app.run_orchestrator_turn(self.transcript(state['task_id']))
        llm.assert_not_called()
        current = load_active_task('m', 'r')
        self.assertEqual(current['state'], 'preliminary_delivered')
        self.assertEqual(current['last_evidence_added_ids'], [])
        self.assertEqual(current['revision'], 1)
        self.assertIn('当前保存数据或提取方法不能满足', reply)
        self.assertIn('当前流程已完成', repeated)
        self.assertEqual(len(list((self.root/'diagnosis_tasks').glob('*/task.json'))), 1)

    async def test_initial_is_idempotent_and_frozen_snapshot_is_checked_on_terminal_resume(self):
        state = await self.initial()
        complete = AsyncMock()
        repeated = await advance_investigation('m', 'r', 'initial', complete=complete)
        self.assertEqual(state['task_id'], repeated['task_id'])
        complete.assert_not_called()
        path = self.root/'diagnosis_tasks'/state['task_id']/'evidence_snapshot.json'
        path.write_text('[]', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            await advance_investigation('m', 'r', 'continue', complete=complete)

    async def test_explicit_wrong_run_task_cannot_fall_back_to_other_task(self):
        state = await self.initial()
        path = self.root/'diagnosis_tasks'/state['task_id']/'task.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['run_id'] = 'other'
        path.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'another run'):
            await advance_investigation('m', 'r', 'continue', state['task_id'])

    async def test_next_step_question_is_read_only_and_prompt_has_persisted_state(self):
        state = await self.initial()
        path = self.root/'diagnosis_tasks'/state['task_id']/'task.json'
        before = path.read_bytes()
        reply = await web_app.run_orchestrator_turn(self.transcript(latest='好的，下一步我该做什么？'))
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('初步诊断流程已完成', reply)
        self.assertIn(state['task_id'], web_app.build_task_prompt(self.transcript(old=True)))

    async def test_chat_restores_session_after_server_memory_is_lost(self):
        state = await self.initial()
        logs = self.models/'logs'
        logs.mkdir()
        session_id = 'restart-regression'
        session_service.sessions.pop(session_id, None)
        with patch('workflow_agents.orchestration.sessions.WEB_LOG_DIR', logs):
            session_service.append_session_log(session_id, 'assistant', self.transcript(state['task_id'])[0]['content'])
            transport = httpx.ASGITransport(app=http_api.app)
            async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
                response = await client.post('/api/chat', json={'session_id': session_id, 'message': '继续'})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(len(body['transcript']), 3)
            self.assertIn(state['task_id'], body['reply'])
        session_service.sessions.pop(session_id, None)


if __name__ == '__main__':
    unittest.main()
