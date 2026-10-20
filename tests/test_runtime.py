"""Standalone startup and shared simulation behavior, without models or API keys."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
import web_app
from workflow_agents.orchestration import sessions
from workflow_agents.simulation.agent import SimulationAgent
from workflow_agents.common.schemas import NO_SURFACE_INFLOW


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_installation_serves_page_and_queries_models_without_llm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            models=root/'models'
            models.mkdir()
            with patch('workflow_agents.orchestration.controller.MODELS_DIR',models), \
                 patch('workflow_agents.scenario.validation.MODELS_DIR',models), \
                 patch.object(sessions,'WEB_LOG_DIR',root/'logs'), \
                 patch.dict(sys.modules,{'workflow_agents.common.llm':None}):
                transport=httpx.ASGITransport(app=web_app.app)
                async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
                    page=await client.get('/')
                    self.assertEqual(page.status_code,200)
                    self.assertIn('SWMM-Agentic3',page.text)
                    response=await client.post('/api/chat',json={'message':'列出模型'})
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertIn('No SWMM-2D model projects',response.json()['reply'])
                    sessions.sessions.pop(response.json()['session_id'],None)

    async def test_simulation_agent_preserves_rain_hash_and_no_surface_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'m/runs/r'
            root.mkdir(parents=True)
            (root/'workflow_state.json').write_text(json.dumps({'history':[{'stage':'scenario'}]}))
            request={'model_name':'m','run_id':'r','rainfall_file':'events/rain.txt',
                     'event_name':'rain','scenario_name':'baseline','event_sha256':'confirmed-event-hash'}
            result={'model_name':'m','run_id':'r','run_root':str(root),'created_at':'2026-01-01',
                    'ca2d':{'status':NO_SURFACE_INFLOW,'surface_inflow_status':NO_SURFACE_INFLOW}}
            with patch('workflow_agents.simulation.agent.load_scenario_request',return_value=request), \
                 patch('workflow_agents.simulation.service.run_swmm_2d_project_from_rainfall',
                       return_value='Completed:\n'+json.dumps(result)) as solver:
                await SimulationAgent('m','',run_id='r')
            self.assertEqual(solver.call_args.kwargs['expected_event_sha256'],'confirmed-event-hash')
            state=json.loads((root/'workflow_state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['state'],NO_SURFACE_INFLOW)
            self.assertEqual([item['stage'] for item in state['history']],['scenario','simulation'])
