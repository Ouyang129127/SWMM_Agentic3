"""Offline behavior tests for one approval -> one web workflow action."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import workflow_agents.orchestration.agents as main
import workflow_agents.orchestration.controller as web_app
import web_app as http_api
from workflow_agents.orchestration import sessions as session_service
from autogen_ext.models.replay import ReplayChatCompletionClient
from test_initial_diagnosis import fixture, answer
from workflow_agents.orchestration.investigation import advance_investigation
from workflow_agents.orchestration.investigation_flow import load_active_task
from workflow_agents.common.state import build_state, save_state
from workflow_agents.orchestration.session_state import WebWorkflow
from workflow_agents.interfaces.presentation import chat_history_summary


class StepControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.models = Path(self.temp.name) / 'models'
        self.root = self.models / 'm/runs/r'
        (self.root / 'evidence').mkdir(parents=True)
        fixture(self.root)
        save_state(self.root, build_state('m', 'r', self.root, state='EVIDENCE_READY'))
        self.model = self.models / 'm'
        (self.model / 'events').mkdir()
        (self.model / 'events/rain1.txt').write_text('2024 01 01 00 00 1', encoding='utf-8')
        (self.model / 'swmm/scenarios/baseline').mkdir(parents=True)
        (self.model / 'swmm/scenarios/baseline/model.inp').write_text('[TITLE]\nprobe', encoding='utf-8')
        self.logs = Path(self.temp.name) / 'logs'
        self.logs.mkdir()
        self.patches = [patch('workflow_agents.orchestration.controller.MODELS_DIR', self.models),
                        patch('workflow_agents.common.state.MODELS_DIR', self.models),
                        patch('workflow_agents.scenario.requests.MODELS_DIR', self.models),
                        patch('workflow_agents.orchestration.sessions.WEB_LOG_DIR', self.logs)]
        for p in self.patches:
            p.start()
        self.sessions_before = set(session_service.sessions)
        self.flow = WebWorkflow()
        self.messages = []

    def tearDown(self):
        for identifier in set(session_service.sessions) - self.sessions_before:
            session_service.sessions.pop(identifier, None)
        web_app.run_locks.clear()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    async def turn(self, message):
        self.messages.append({'role': 'user', 'content': message})
        reply = await web_app.run_orchestrator_turn(self.messages, self.flow)
        self.messages.append({'role': 'assistant', 'content': reply})
        return reply

    def bind(self, task_id=''):
        web_app.bind_web_run(self.flow, 'm', 'r', task_id)
        web_app.refresh_web_workflow(self.flow)
        self.flow.wait_for(self.flow.next_action)

    async def initial(self, requests=None):
        with patch('workflow_agents.reporting.pipeline.attach_display_report', new_callable=AsyncMock):
            return await advance_investigation('m', 'r', 'initial',
                       complete=AsyncMock(side_effect=lambda p: answer(p, requests)))

    async def test_original_user_request_only_plans_and_requires_rainfall_choice(self):
        with patch('workflow_agents.orchestration.controller.prepare_scenario_request') as scenario, \
             patch('workflow_agents.orchestration.controller.run_simulation_from_scenario_request') as simulation, \
             patch('workflow_agents.orchestration.controller.advance_investigation', new_callable=AsyncMock) as investigation, \
             patch('workflow_agents.orchestration.controller.run_web_orchestrator_agent_turn', new_callable=AsyncMock) as llm:
            reply = await self.turn('针对m模型，帮我设计一次 SWMM-2D 内涝诊断流程，并一步一步执行。')
            self.assertEqual(self.flow.status, 'awaiting_inputs')
            self.assertIn('events/rain1.txt', reply)
            await self.turn('继续')
            self.assertEqual(self.flow.status, 'awaiting_inputs')
            scenario.assert_not_called()
            simulation.assert_not_called()
            investigation.assert_not_awaited()
            llm.assert_not_awaited()

    async def test_selection_waits_then_confirmation_only_prepares_scenario(self):
        await self.turn('为 m 设计 SWMM-2D 流程')
        await self.turn('选择 events/rain1.txt')
        self.assertEqual(self.flow.next_action, 'scenario')
        self.assertFalse(self.flow.run_id)
        with patch('workflow_agents.orchestration.controller.run_simulation_from_scenario_request') as simulation:
            reply = await self.turn('继续')
        simulation.assert_not_called()
        self.assertIn('尚未执行模拟', reply)
        self.assertEqual(self.flow.next_action, 'simulation')
        root = self.models / 'm/runs' / self.flow.run_id
        self.assertEqual(json.loads((root / 'workflow_state.json').read_text(encoding='utf-8'))['state'], 'SCENARIO_READY')
        self.assertFalse((root / 'summary.json').exists())

    async def test_diagnosis_verification_and_report_each_require_separate_confirmation(self):
        self.bind()
        complete = AsyncMock(side_effect=lambda prompt, audit=None: answer(prompt))
        with patch('workflow_agents.diagnosis.service.configured_completion', complete), \
             patch('workflow_agents.reporting.pipeline.attach_display_report', new_callable=AsyncMock) as html:
            await self.turn('继续')
            complete.assert_not_awaited()
            state = load_active_task('m', 'r', self.flow.task_id)
            self.assertEqual([h['action'] for h in state['history']], ['prepare'])
            await self.turn('继续')
            complete.assert_awaited_once()
            self.assertEqual(self.flow.next_action, 'verify')
            folder = self.root / 'diagnosis_tasks' / self.flow.task_id
            self.assertFalse((folder / 'verification.json').exists())
            self.assertFalse((folder / 'report.json').exists())
            await self.turn('继续')
            self.assertEqual(self.flow.next_action, 'report')
            self.assertTrue((folder / 'verification.json').exists())
            self.assertFalse((folder / 'report.json').exists())
            html.assert_not_awaited()
            await self.turn('继续')
            html.assert_awaited_once()
            self.assertEqual(self.flow.status, 'completed')
            state = load_active_task('m', 'r', self.flow.task_id)
            self.assertEqual([h['action'] for h in state['history']], ['prepare', 'diagnose', 'verify', 'report'])
            await self.turn('继续')
            complete.assert_awaited_once()
            html.assert_awaited_once()

    async def test_simulation_and_evidence_are_separate_confirmations(self):
        await self.turn('为 m 设计流程，降雨 events/rain1.txt')
        await self.turn('继续')
        root = self.models / 'm/runs' / self.flow.run_id
        def simulate(request):
            save_state(root, build_state('m', self.flow.run_id, root, state='RUN_READY'))
            return '模拟已完成'
        with patch('workflow_agents.orchestration.controller.run_simulation_from_scenario_request', side_effect=simulate) as simulation, \
             patch('workflow_agents.orchestration.controller.run_workflow_stage') as evidence:
            await self.turn('继续')
        simulation.assert_called_once()
        evidence.assert_not_called()
        self.assertEqual(self.flow.next_action, 'evidence_building')

    async def test_supplement_stops_before_new_diagnosis(self):
        requests = [{'tool': 'evidence_lookup', 'object_id': 'N2', 'metric_name': 'max_flooding_flow', 'reason': '比较相邻节点'}]
        state = await self.initial(requests)
        self.bind(state['task_id'])
        complete = AsyncMock()
        with patch('workflow_agents.diagnosis.service.configured_completion', complete):
            await self.turn('继续')
        complete.assert_not_awaited()
        stored = load_active_task('m', 'r', self.flow.task_id)
        self.assertEqual(stored['state'], 'ready_for_diagnosis')
        self.assertEqual(stored['revision'], 1)
        self.assertIn('N2:max_flooding_flow', stored['visible_ids'])

    async def test_verification_failure_stops_before_repair(self):
        self.bind()
        await self.turn('继续')
        def bad_reference(prompt, audit=None):
            result = answer(prompt)
            result['claims'] = [{'object_type': 'node', 'object_id': 'N1', 'question_id': 'Q1',
                'claim_kind': 'fact', 'claim_text': '保存过程', 'engineering_reason': '保存过程',
                'alternatives': '待区分', 'scope': '本场', 'evidence_ids': ['MISSING']}]
            return result
        complete = AsyncMock(side_effect=bad_reference)
        with patch('workflow_agents.diagnosis.service.configured_completion', complete):
            await self.turn('继续')
            await self.turn('继续')
        complete.assert_awaited_once()
        stored = load_active_task('m', 'r', self.flow.task_id)
        self.assertEqual(stored['state'], 'revision_requested')
        self.assertEqual(self.flow.next_action, 'diagnose')
        self.assertEqual(stored['revision'], 1)

    async def test_failed_diagnosis_preserves_task_and_never_verifies(self):
        self.bind()
        await self.turn('继续')
        task_id = self.flow.task_id
        with patch('workflow_agents.diagnosis.service.configured_completion', new=AsyncMock(side_effect=OSError('provider unavailable'))):
            reply = await self.turn('继续')
        self.assertIn('provider unavailable', reply)
        self.assertEqual(self.flow.task_id, task_id)
        self.assertEqual(self.flow.next_action, 'diagnose')
        self.assertFalse((self.root / 'diagnosis_tasks' / task_id / 'verification.json').exists())

    async def test_failed_simulation_does_not_advance(self):
        await self.turn('为 m 设计流程，降雨 events/rain1.txt')
        await self.turn('继续')
        with patch('workflow_agents.orchestration.controller.run_simulation_from_scenario_request', return_value='INPUT_REJECTED'):
            reply = await self.turn('继续')
        self.assertIn('未执行后续步骤', reply)
        self.assertEqual(self.flow.next_action, 'simulation')

    async def test_status_and_pause_do_not_execute_and_resume_waits_again(self):
        self.bind()
        with patch('workflow_agents.orchestration.controller.advance_investigation', new_callable=AsyncMock) as operation:
            await self.turn('下一步是什么？')
            await self.turn('暂停')
            reply = await self.turn('继续')
        operation.assert_not_awaited()
        self.assertIn('本轮未执行', reply)
        self.assertEqual(self.flow.status, 'awaiting_confirmation')

    async def test_old_inventory_does_not_select_a_run_for_a_new_plan(self):
        self.messages.append({'role': 'assistant', 'content': 'old inventory models/m/runs/r run_id: r EVIDENCE_READY'})
        await self.turn('为 m 重新设计诊断流程，events/rain1.txt')
        self.assertFalse(self.flow.run_id)
        self.assertEqual(self.flow.next_action, 'scenario')

    async def test_stale_disk_progress_requires_reconfirmation(self):
        self.bind()
        state = await advance_investigation('m', 'r', 'prepare')
        reply = await self.turn('继续')
        self.assertIn('本轮未执行', reply)
        self.assertEqual(self.flow.task_id, state['task_id'])
        self.assertEqual(self.flow.next_action, 'diagnose')
        self.assertEqual(len(list((self.root / 'diagnosis_tasks').glob('*/task.json'))), 1)

    async def test_two_sessions_cannot_use_same_approval_to_prepare_duplicate_tasks(self):
        self.bind()
        other = WebWorkflow.from_dict(self.flow.to_dict())
        replies = await asyncio.gather(web_app.execute_web_step(self.flow), web_app.execute_web_step(other))
        self.assertEqual(len(list((self.root / 'diagnosis_tasks').glob('*/task.json'))), 1)
        self.assertTrue(any('本轮未执行' in r for r in replies))

    async def test_new_scenarios_have_distinct_runs_even_with_same_model_and_rainfall(self):
        await self.turn('为 m 设计流程，events/rain1.txt')
        other = WebWorkflow.from_dict(self.flow.to_dict())
        await asyncio.gather(web_app.execute_web_step(self.flow), web_app.execute_web_step(other))
        self.assertNotEqual(self.flow.run_id, other.run_id)
        self.assertEqual(web_app.extract_workflow_run_id(web_app.workflow_status_text(self.flow)), self.flow.run_id)

    async def test_changed_question_only_proposes_new_task(self):
        state = await self.initial()
        self.bind(state['task_id'])
        self.flow.status = 'completed'
        before = len(list((self.root / 'diagnosis_tasks').glob('*/task.json')))
        await self.turn('为什么 N1 冒溢？')
        self.assertEqual(self.flow.next_action, 'prepare_diagnosis')
        self.assertFalse(self.flow.task_id)
        self.assertEqual(len(list((self.root / 'diagnosis_tasks').glob('*/task.json'))), before)
        await self.turn('继续')
        self.assertNotEqual(self.flow.task_id, state['task_id'])
        self.assertEqual(self.flow.next_action, 'diagnose')

    async def test_server_restart_restores_pending_plan(self):
        transport = httpx.ASGITransport(app=http_api.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            response = await client.post('/api/chat', json={'message': '为 m 设计流程，events/rain1.txt'})
            data = response.json()
            identifier = data['session_id']
            session_service.sessions.pop(identifier)
            with patch('workflow_agents.orchestration.controller.run_simulation_from_scenario_request') as simulation:
                response = await client.post('/api/chat', json={'session_id': identifier, 'message': '继续',
                                                                'flow_version': data['workflow']['version']})
            simulation.assert_not_called()
            self.assertEqual(response.status_code, 200)
            restored = response.json()['workflow']
            self.assertTrue(restored['run_id'])
            self.assertEqual(restored['next_action'], 'simulation')

    async def test_duplicate_confirmation_version_is_rejected(self):
        transport = httpx.ASGITransport(app=http_api.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            data = (await client.post('/api/chat', json={'message': '为 m 设计流程，events/rain1.txt'})).json()
            body = {'session_id': data['session_id'], 'message': '继续', 'flow_version': data['workflow']['version']}
            first = await client.post('/api/chat', json=body)
            with patch('workflow_agents.orchestration.controller.run_simulation_from_scenario_request') as simulation:
                duplicate = await client.post('/api/chat', json=body)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(duplicate.status_code, 409)
            simulation.assert_not_called()

    async def test_interrupted_approval_never_replays_automatically(self):
        self.bind()
        self.flow.status = 'running'
        restored = WebWorkflow.from_dict(self.flow.to_dict())
        with patch('workflow_agents.orchestration.controller.advance_investigation', new_callable=AsyncMock) as operation:
            reply = await web_app.run_orchestrator_turn([{'role': 'user', 'content': '继续'}], restored)
        operation.assert_not_awaited()
        self.assertIn('本轮未执行', reply)
        self.assertEqual(restored.status, 'awaiting_confirmation')

    async def test_context_limit_stops_same_payload_retry_and_allows_scoped_task(self):
        self.bind()
        await self.turn('继续')
        task_id = self.flow.task_id
        error = "This model's maximum context length is 1048576 tokens. However, you requested 7705156 tokens (7705156 in the messages, 0 in the completion)."
        complete = AsyncMock(side_effect=ValueError(error))
        with patch('workflow_agents.diagnosis.service.configured_completion', complete):
            reply = await self.turn('继续')
            await self.turn('继续')
        complete.assert_awaited_once()
        self.assertIn('7,705,156', chat_history_summary(reply))
        self.assertEqual(self.flow.status, 'context_blocked')
        self.assertEqual(self.flow.task_id, task_id)
        self.assertFalse(self.flow.next_action)
        await self.turn('只分析 N1:E001 的冒溢原因')
        self.assertEqual(self.flow.next_action, 'prepare_diagnosis')
        await self.turn('继续')
        self.assertNotEqual(self.flow.task_id, task_id)
        self.assertEqual(self.flow.next_action, 'diagnose')

    async def test_full_report_is_linked_not_repeated_in_chat(self):
        state = await self.initial()
        before = Path(state['report_file']).read_bytes()
        reply = web_app.format_investigation_result(state)
        self.assertIn('查看完整文字报告', reply)
        self.assertIn('/models/', reply)
        self.assertNotIn(state['report'], reply)
        self.assertEqual(Path(state['report_file']).read_bytes(), before)
        self.assertLess(len(chat_history_summary(reply)), 800)

    async def test_folded_debug_evidence_is_not_replayed_into_planning_prompt(self):
        self.bind()
        transcript = [{'role': 'assistant', 'content': '结果：已完成。\n\n:::details 完整证据\nDEBUG_PAYLOAD_SENTINEL\n::: '},
                      {'role': 'user', 'content': '解释当前工作安排'}]
        prompt = web_app.build_task_prompt(transcript, self.flow)
        self.assertNotIn('DEBUG_PAYLOAD_SENTINEL', prompt)
        self.assertIn('结果：已完成', prompt)
        self.assertIn('解释当前工作安排', prompt)

    async def test_web_model_has_one_text_turn_and_no_execution_tools(self):
        import sys
        from types import SimpleNamespace
        replay = ReplayChatCompletionClient(['Final Answer: Plan, waiting for confirmation.', 'Should never be generated.'])
        with patch.dict(sys.modules, {'workflow_agents.common.llm': SimpleNamespace(deepseek_flash=replay)}), \
             patch.object(replay, 'create', wraps=replay.create) as create, \
             patch('workflow_agents.simulation.agent.SimulationAgent', new_callable=AsyncMock) as simulation:
            reply = await main.run_web_orchestrator_agent_turn('Please design a workflow step by step.')
        self.assertIn('waiting for confirmation', reply)
        create.assert_awaited_once()
        self.assertFalse(create.await_args.kwargs.get('tools'))
        simulation.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
