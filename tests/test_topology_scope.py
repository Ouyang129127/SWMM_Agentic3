import unittest

from workflow_agents.evidence.topology_scope import SCOPE_POLICY, build_topology_scope


def model(edges, special=None, facilities=None):
    special, facilities = special or {}, facilities or set()
    nodes = {n: {'node_type': special.get(n, 'JUNCTIONS')} for edge in edges for n in edge[1:]}
    return {'available': True, 'nodes': nodes, 'links': {
        key: {'from_node': start, 'to_node': end, 'link_type': 'PUMPS' if key in facilities else 'CONDUITS'}
        for key, start, end in edges}}


class TopologyScopeTests(unittest.TestCase):
    def test_long_chain_is_not_limited_by_depth_or_count(self):
        data = model([(f'L{i}', f'N{i}', f'N{i+1}') for i in range(140)])
        scope = build_topology_scope(data, 'N70')
        self.assertEqual(scope['policy'], SCOPE_POLICY)
        self.assertEqual(len(scope['link_roles']), 140)
        self.assertEqual(set(scope['boundary_nodes']), {'N0', 'N140'})
        self.assertEqual({t['stop_reason'] for t in scope['traces']}, {'terminal'})

    def test_upstream_confluence_includes_all_interfaces_not_their_branches(self):
        data = model([('a', 'U', 'T'), ('b', 'A', 'U'), ('c', 'B', 'U'),
                      ('d', 'AA', 'A'), ('e', 'BB', 'B'), ('f', 'T', 'D')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual(scope['boundary_nodes']['U'], ['confluence'])
        self.assertEqual(set(scope['link_roles']), {'a', 'b', 'c', 'f'})
        self.assertEqual(scope['node_roles']['A'], 'interface_endpoint')
        self.assertNotIn('AA', scope['node_roles'])

    def test_downstream_divergence_and_intermediate_side_branch(self):
        data = model([('main1', 'T', 'C'), ('main2', 'C', 'D'),
                      ('exit1', 'D', 'E'), ('exit2', 'D', 'F'), ('beyond', 'E', 'G'),
                      ('side1', 'S', 'C'), ('side2', 'B', 'S'),
                      ('in1', 'A', 'B'), ('in2', 'X', 'B'), ('far', 'Y', 'X')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual(scope['boundary_nodes']['D'], ['divergence'])
        self.assertEqual(scope['boundary_nodes']['B'], ['confluence'])
        self.assertEqual(set(scope['link_roles']), set(data['links']) - {'beyond', 'far'})
        side = next(t for t in scope['traces'] if t['role'] == 'side_branch')
        self.assertEqual(side['nodes'], ['C', 'S', 'B'])
        self.assertEqual(side['stop_reason'], 'confluence')

    def test_upstream_diversion_keeps_exchange_without_following_it(self):
        data = model([('a', 'U', 'T'), ('b', 'A', 'U'), ('c', 'U', 'S'), ('d', 'S', 'X')])
        scope = build_topology_scope(data, 'T')
        self.assertIn('b', scope['link_roles'])
        self.assertEqual(scope['link_roles']['c'], 'boundary_interface')
        self.assertNotIn('d', scope['link_roles'])

    def test_target_branching_does_not_prevent_starting_every_direct_path(self):
        data = model([('a', 'A', 'T'), ('b', 'B', 'T'), ('c', 'T', 'C'), ('d', 'T', 'D')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual(len(scope['traces']), 4)
        self.assertEqual(scope['direct_links'], ['a', 'b', 'c', 'd'])

    def test_cycle_terminates_without_arbitrary_cap(self):
        data = model([('a', 'T', 'A'), ('b', 'A', 'B'), ('c', 'B', 'T')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual({t['stop_reason'] for t in scope['traces']}, {'cycle'})
        self.assertEqual(len(scope['link_roles']), 3)

    def test_facilities_and_special_nodes_stop_and_keep_interfaces(self):
        edges = [('a', 'T', 'A'), ('b', 'A', 'B'), ('c', 'B', 'C')]
        for data, stop in [(model(edges, special={'A': 'STORAGE'}), 'special_node:STORAGE'),
                           (model(edges, facilities={'a'}), 'facility_link')]:
            scope = build_topology_scope(data, 'T')
            self.assertEqual(scope['boundary_nodes']['A'], [stop])
            self.assertEqual(set(scope['link_roles']), {'a', 'b'})
            self.assertNotIn('c', scope['link_roles'])

    def test_missing_node_and_disconnected_target_are_explicit(self):
        data = model([('a', 'T', 'A'), ('b', 'A', 'B')])
        del data['nodes']['A']
        scope = build_topology_scope(data, 'T')
        self.assertEqual(scope['status'], 'partial')
        self.assertEqual(scope['missing_node_definitions'], ['A'])
        self.assertEqual(scope['boundary_nodes']['A'], ['missing_node_definition'])
        disconnected = build_topology_scope(data, 'Unknown')
        self.assertEqual(disconnected['status'], 'partial')
        self.assertEqual(disconnected['direct_links'], [])

    def test_parallel_links_are_distinct_exchanges(self):
        data = model([('a', 'T', 'A'), ('b', 'A', 'B'), ('c', 'A', 'B')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual(scope['boundary_nodes']['A'], ['divergence'])
        self.assertEqual(set(scope['link_roles']), {'a', 'b', 'c'})


if __name__ == '__main__':
    unittest.main()
