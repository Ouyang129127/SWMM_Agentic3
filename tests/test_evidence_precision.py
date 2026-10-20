"""Precision boundaries and reproducible report volume audits."""
import copy
import json
import unittest

from workflow_agents.evidence.evidence_precision import round_evidence_row, round_value
from workflow_agents.reporting.materials import check_rounded_event_volumes
from workflow_agents.evidence.sampled_events import identify_events


class PrecisionTests(unittest.TestCase):
    def test_depths_are_numeric_and_small_signed_flows_stay_nonzero(self):
        source = {'depth_m': 0.4852335453033447, 'head_m': 10.123456789,
                  'flows': [0.000400123, -0.000400123, 1.0000001e-9, 0.0],
                  'missing': None, 'available': True, 'node_id': '0.4852335453033447'}
        result = round_value(source)
        self.assertEqual(result['depth_m'], .485)
        self.assertEqual(result['head_m'], 10.123)
        self.assertEqual(result['flows'], source['flows'])
        self.assertIsNone(result['missing'])
        self.assertIs(result['available'], True)
        self.assertEqual(result['node_id'], source['node_id'])
        self.assertIsInstance(json.loads(json.dumps(result))['depth_m'], float)
        self.assertEqual(source['depth_m'], .4852335453033447)

    def test_parameters_and_provenance_do_not_change(self):
        for metric in ('local_structure', 'facilities_and_storage'):
            row = {'metric_name': metric, 'value': {'roughness': .013123456,
                   'invert_elevation': 10.123456789, 'input_fields': ['.013123456']}}
            self.assertEqual(round_evidence_row(row), row)
        row = {'metric_name': 'max_fullness', 'value': 1.23456789,
               'threshold': 1e-9, 'depth_reference_m': 1.23456789,
               'evidence_id': 'original', 'source_hashes': {'raw': 'abc'}}
        result = round_evidence_row(row)
        self.assertEqual(result['value'], 1.235)
        self.assertEqual({k: v for k, v in result.items() if k != 'value'},
                         {k: v for k, v in row.items() if k != 'value'})

    def test_report_reproduces_each_event_before_summing(self):
        records = [{'node_id': 'N1', 'date': '2024-01-01', 'time': f'00:0{i}:00',
                    'flow_Ls': flow} for i, flow in enumerate([0, .02498765, 0, .02498765, 0])]
        raw = identify_events(records, 'r', 'm')['events']
        rounded = round_value(raw)
        self.assertEqual([e['estimated_volume_m3'] for e in rounded], [.001, .001])
        self.assertNotEqual(round_value(sum(e['estimated_volume_m3'] for e in raw)), .002)
        check_rounded_event_volumes(rounded, raw)
        # Equal and opposite corruption must fail even when the total agrees.
        corrupt = copy.deepcopy(rounded)
        corrupt[0]['estimated_volume_m3'] += .001
        corrupt[1]['estimated_volume_m3'] -= .001
        with self.assertRaisesRegex(ValueError, 'rounded raw'):
            check_rounded_event_volumes(corrupt, raw)
        with self.assertRaisesRegex(ValueError, 'rounded raw'):
            check_rounded_event_volumes(rounded[:-1], raw)
