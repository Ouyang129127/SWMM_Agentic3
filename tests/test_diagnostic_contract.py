import copy
import json
import unittest
from datetime import datetime, timedelta

from workflow_agents.evidence.sampled_events import identify_events
from workflow_agents.verification.reference_checks import check_references
from workflow_agents.diagnosis.task_scope import select_scope


def records(flows, node='N1'):
    return [{'node_id': node, 'DateTime': str(datetime(2024, 1, 1) + timedelta(minutes=i)), 'flow_Ls': f}
            for i, f in enumerate(flows)]


class EventTests(unittest.TestCase):
    def test_separate_positive_runs(self):
        result = identify_events(records([0, 2, 3, 0, 4, 0]), 'r', 'm')['events']
        self.assertEqual([e['event_id'] for e in result], ['N1:E001', 'N1:E002'])
        self.assertEqual([e['duration_minutes'] for e in result], [2, 1])
        self.assertAlmostEqual(result[0]['estimated_volume_m3'], .3)
        self.assertEqual(result[0]['end'], '2024-01-01 00:03:00')

    def test_dry_singleton_and_terminal(self):
        self.assertEqual(identify_events(records([0, 0]), 'r', 'm')['events'], [])
        event = identify_events(records([5]), 'r', 'm')['events'][0]
        self.assertTrue(event['right_censored'])
        self.assertEqual(event['duration_minutes'], 0)
        self.assertEqual(event['estimated_volume_m3'], 0)

    def test_missing_and_duplicates(self):
        for data in (records([0, float('nan')]), records([0, 1]) + records([0])[:1]):
            with self.assertRaises(ValueError):
                identify_events(data, 'r', 'm')
        data = records([0, 1, 2, 0])
        del data[2]
        with self.assertRaises(ValueError):
            identify_events(data, 'r', 'm')


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'evidence_id': 'E1', 'source_model': 'm', 'run_id': 'r', 'value': 0}]
        self.payload = {'model_name': 'm', 'run_id': 'r', 'claims': [
            {'claim_id': 'C1', 'evidence_ids': ['E1']},
            {'claim_id': 'C2', 'evidence_ids': ['E9']},
            {'claim_id': 'C3', 'evidence_ids': []}]}

    def test_three_reference_states(self):
        result = check_references(self.payload, self.rows, 'm', 'r')
        self.assertEqual([x['verification_status'] for x in result['claim_checks']],
                         ['references_verified', 'references_missing', 'no_references'])
        self.assertFalse(result['causal_validity_checked'])
        self.assertAlmostEqual(result['summary']['reference_failure_rate'], 2 / 3)

    def test_foreign_run_and_duplicate(self):
        bad = copy.deepcopy(self.rows)
        bad[0]['run_id'] = 'other'
        with self.assertRaises(ValueError):
            check_references(self.payload, bad, 'm', 'r')
        with self.assertRaises(ValueError):
            check_references(self.payload, self.rows * 2, 'm', 'r')

    def test_no_claims_not_perfect_score(self):
        self.payload['claims'] = []
        self.assertIsNone(check_references(self.payload, self.rows, 'm', 'r')['summary']['reference_failure_rate'])


class ScopeTests(unittest.TestCase):
    def test_global_and_node(self):
        self.assertEqual(select_scope('给我整体评估报告', {'N1', 'N10'})['scope'], 'global')
        self.assertEqual(select_scope('分析N1为什么冒溢', {'N1', 'N10'})['node_id'], 'N1')
        self.assertEqual(select_scope('N1:E002', {'N1'})['event_id'], 'N1:E002')

    def test_unknown_and_ambiguous(self):
        for message in ('分析节点 N9', '分析N1和N10', '为什么这个节点冒溢'):
            with self.assertRaises(ValueError):
                select_scope(message, {'N1', 'N10'})
