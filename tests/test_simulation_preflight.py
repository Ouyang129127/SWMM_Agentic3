"""Exercise real rainfall/static preflight without executing either solver."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from workflow_agents.simulation.runner import run_swmm_2d_project_from_rainfall


class SimulationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.model=Path(self.temporary.name)/'model'
        self.model.mkdir()
        timing={'swmm_save_interval_minutes':5,'boundary_interval_minutes':5,
                'ca2d_save_interval_minutes':5,'post_rainfall_hours':0,
                'boundary_method':'interval_mean','rainfall_time_convention':'interval_start'}
        (self.model/'simulation_timing.json').write_text(json.dumps(timing),encoding='utf-8')

    def test_missing_static_data_is_reported_before_solver_or_run_creation(self):
        with patch('workflow_agents.simulation.runner._resolve_model_root',return_value=self.model), \
             patch('workflow_agents.simulation.runner.export_native_reports') as solver:
            result=run_swmm_2d_project_from_rainfall('model','events/rain.txt')
        self.assertIn('CA2D static model is incomplete',result)
        self.assertIn('elevation.npy',result)
        solver.assert_not_called()
        self.assertFalse((self.model/'runs').exists())

    def test_changed_rainfall_is_rejected_after_real_static_validation(self):
        static=self.model/'static'
        static.mkdir()
        (static/'config.json').write_text(json.dumps({'cell_size':1}),encoding='utf-8')
        for name in ('elevation','smid_grid','flow_mask','building_mask','resistance'):
            np.save(static/(name+'.npy'),np.ones((1,1)))
        (static/'node_to_cell_mapping.csv').write_text('node_id,cell_row,cell_col\nN1,0,0\n',encoding='utf-8')
        scenario=self.model/'swmm/scenarios/baseline'
        scenario.mkdir(parents=True)
        (scenario/'model.inp').write_text('[TITLE]\nPreflight only\n',encoding='utf-8')
        (self.model/'events').mkdir()
        (self.model/'events/rain.txt').write_text('2024-01-01 00:00:00,10\n2024-01-01 00:05:00,0\n',encoding='utf-8')
        with patch('workflow_agents.simulation.runner._resolve_model_root',return_value=self.model), \
             patch('workflow_agents.simulation.runner.export_native_reports') as solver:
            result=run_swmm_2d_project_from_rainfall('model','events/rain.txt',expected_event_sha256='old-hash')
        payload=json.loads(result.split('\n',1)[1])
        self.assertEqual(payload['status'],'INPUT_REJECTED')
        self.assertEqual(payload['input_validation']['code'],'EVENT_HASH_MISMATCH')
        solver.assert_not_called()
        self.assertFalse((self.model/'runs').exists())
