import unittest

from workflow_agents.evidence.topology_scope import SCOPE_POLICY, build_topology_scope


def model(edges, special=None, facilities=None):
    special, facilities = special or {}, facilities or {}
    nodes = {n: {'node_type': special.get(n, 'JUNCTIONS')} for edge in edges for n in edge[1:]}
    return {'available': True, 'nodes': nodes, 'links': {
        key: {'from_node': start, 'to_node': end, 'link_type': facilities.get(key, 'CONDUITS')}
        for key, start, end in edges}}


EXAMPLE_EDGES = [
    ('1', '9', '10'), ('4', '19', '20'), ('5', '20', '21'), ('6', '10', '21'),
    ('7', '21', '22'), ('8', '22', '16'), ('10', '17', '18'),
    ('11', '13', '14'), ('12', '14', '15'), ('13', '15', '16'),
    ('14', '23', '24'), ('15', '16', '24'), ('16', '24', '17'),
]


class TopologyScopeTests(unittest.TestCase):
    def test_example_n10_stops_at_n21_without_side_or_boundary_interfaces(self):
        scope = build_topology_scope(model(EXAMPLE_EDGES, special={'18': 'OUTFALLS'}), '10')
        self.assertEqual(scope['policy'], SCOPE_POLICY)
        self.assertEqual(scope['status'], 'complete_for_policy')
        self.assertEqual(set(scope['node_roles']), {'9', '10', '21'})
        self.assertEqual(set(scope['link_roles']), {'1', '6'})
        self.assertEqual(scope['boundary_nodes'], {'9': ['terminal'], '21': ['confluence']})

    def test_example_n21_takes_only_one_segment_on_both_upstream_inputs(self):
        scope = build_topology_scope(model(EXAMPLE_EDGES), '21')
        self.assertEqual(set(scope['node_roles']), {'10', '20', '21', '22', '16'})
        self.assertEqual(set(scope['link_roles']), {'5', '6', '7', '8'})
        self.assertEqual(scope['boundary_nodes']['10'], ['upstream_one_segment'])
        self.assertEqual(scope['boundary_nodes']['20'], ['upstream_one_segment'])
        self.assertEqual(scope['boundary_nodes']['16'], ['confluence'])
        downstream = next(t for t in scope['traces'] if t['direction'] == 'downstream')
        self.assertEqual(downstream['nodes'], ['21', '22', '16'])
        self.assertEqual(downstream['links'], ['7', '8'])
        self.assertTrue(all(t['role'] == 'main' for t in scope['traces']))

    def test_long_downstream_chain_has_no_arbitrary_depth_cap(self):
        scope = build_topology_scope(model([(f'L{i}', f'N{i}', f'N{i+1}') for i in range(140)]), 'N70')
        self.assertEqual(set(scope['link_roles']), {f'L{i}' for i in range(69, 140)})
        self.assertNotIn('N68', scope['node_roles'])
        self.assertEqual(scope['boundary_nodes']['N69'], ['upstream_one_segment'])
        self.assertEqual(scope['boundary_nodes']['N140'], ['terminal'])

    def test_upstream_endpoint_retained_but_its_other_pipes_not_included(self):
        data = model([('a', 'U', 'T'), ('b', 'A', 'U'), ('c', 'B', 'U'),
                      ('side', 'U', 'S'), ('down', 'T', 'D')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual(scope['boundary_nodes']['U'], ['confluence'])
        self.assertEqual(set(scope['node_roles']), {'U', 'T', 'D'})
        self.assertEqual(set(scope['link_roles']), {'a', 'down'})

    def test_downstream_confluence_keeps_node_not_its_side_or_next_link(self):
        data = model([('a', 'T', 'A'), ('b', 'A', 'C'), ('side', 'S', 'C'), ('next', 'C', 'D')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual(set(scope['node_roles']), {'T', 'A', 'C'})
        self.assertEqual(set(scope['link_roles']), {'a', 'b'})
        self.assertEqual(scope['boundary_nodes']['C'], ['confluence'])

    def test_pure_downstream_split_stops_partial_and_requires_path_supplement(self):
        data = model([('a', 'T', 'A'), ('b', 'A', 'D'), ('c', 'D', 'E'), ('d', 'D', 'F')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual(set(scope['link_roles']), {'a', 'b'})
        self.assertEqual(set(scope['node_roles']), {'T', 'A', 'D'})
        self.assertEqual(scope['status'], 'partial')
        self.assertEqual(scope['unresolved_branches'], ['D'])
        self.assertEqual(scope['boundary_nodes']['D'], ['downstream_branch_unresolved'])

    def test_target_confluence_does_not_prevent_downstream_investigation(self):
        data = model([('a', 'A', 'T'), ('b', 'B', 'T'), ('c', 'T', 'C'), ('d', 'C', 'D')])
        scope = build_topology_scope(data, 'T')
        self.assertIn('d', scope['link_roles'])
        self.assertNotIn('T', scope['boundary_nodes'])

    def test_target_split_retains_direct_exchanges_but_does_not_choose_paths(self):
        data = model([('a', 'A', 'T'), ('b', 'B', 'T'), ('c', 'T', 'C'), ('d', 'T', 'D'),
                      ('beyond1', 'C', 'E'), ('beyond2', 'D', 'F')])
        scope = build_topology_scope(data, 'T')
        self.assertEqual(scope['direct_links'], ['a', 'b', 'c', 'd'])
        self.assertEqual(set(scope['link_roles']), {'a', 'b', 'c', 'd'})
        self.assertEqual(scope['status'], 'partial')
        self.assertEqual(scope['unresolved_branches'], ['T'])
        self.assertEqual(scope['node_roles']['C'], 'direct_endpoint')

    def test_cycle_terminates_without_count_cap(self):
        scope = build_topology_scope(model([('a', 'T', 'A'), ('b', 'A', 'B'), ('c', 'B', 'T')]), 'T')
        self.assertIn('cycle', {t['stop_reason'] for t in scope['traces']})
        self.assertEqual(set(scope['link_roles']), {'a', 'b', 'c'})

    def test_storage_outfall_and_divider_are_retained_without_connections_beyond(self):
        for kind in ('STORAGE', 'OUTFALLS', 'DIVIDERS'):
            with self.subTest(kind=kind):
                data = model([('a', 'T', 'A'), ('b', 'A', 'B')], special={'A': kind})
                scope = build_topology_scope(data, 'T')
                self.assertEqual(scope['boundary_nodes']['A'], ['special_node:' + kind])
                self.assertEqual(set(scope['node_roles']), {'T', 'A'})
                self.assertEqual(set(scope['link_roles']), {'a'})

    def test_target_storage_is_an_origin_not_an_immediate_stop(self):
        data = model([('a', 'U', 'T'), ('b', 'T', 'A'), ('c', 'A', 'D')], special={'T': 'STORAGE'})
        self.assertEqual(set(build_topology_scope(data, 'T')['link_roles']), {'a', 'b', 'c'})

    def test_each_facility_type_stops_at_its_body_and_retains_both_endpoints(self):
        for kind in ('PUMPS', 'ORIFICES', 'WEIRS', 'OUTLETS'):
            with self.subTest(kind=kind):
                data = model([('a', 'T', 'A'), ('device', 'A', 'B'), ('next', 'B', 'C')],
                             facilities={'device': kind})
                scope = build_topology_scope(data, 'T')
                self.assertEqual(set(scope['node_roles']), {'T', 'A', 'B'})
                self.assertEqual(set(scope['link_roles']), {'a', 'device'})
                self.assertEqual(scope['boundary_links'], {'device': ['facility_link']})
                self.assertEqual(scope['link_roles']['device'], 'facility_boundary')

    def test_upstream_facility_and_its_endpoint_are_retained_without_crossing_further(self):
        data = model([('far', 'A', 'U'), ('device', 'U', 'T'), ('down', 'T', 'D')],
                     facilities={'device': 'PUMPS'})
        scope = build_topology_scope(data, 'T')
        self.assertEqual(set(scope['link_roles']), {'device', 'down'})
        self.assertEqual(scope['boundary_nodes']['U'], ['facility_link'])

    def test_missing_nodes_and_disconnected_targets_remain_explicit(self):
        data = model([('a', 'T', 'A'), ('b', 'A', 'B')])
        del data['nodes']['A']
        scope = build_topology_scope(data, 'T')
        self.assertEqual(scope['status'], 'partial')
        self.assertEqual(scope['missing_node_definitions'], ['A'])
        self.assertEqual(scope['boundary_nodes']['A'], ['missing_node_definition'])
        self.assertNotIn('b', scope['link_roles'])
        disconnected = build_topology_scope(data, 'Unknown')
        self.assertEqual(disconnected['status'], 'partial')
        self.assertEqual(disconnected['direct_links'], [])

    def test_parallel_downstream_pipes_are_not_silently_chosen(self):
        scope = build_topology_scope(model([('a', 'T', 'A'), ('b', 'A', 'B'), ('c', 'A', 'B')]), 'T')
        self.assertEqual(set(scope['link_roles']), {'a'})
        self.assertEqual(scope['unresolved_branches'], ['A'])


if __name__ == '__main__':
    unittest.main()
