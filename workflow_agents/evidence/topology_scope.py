"""Deterministic event corridors bounded by model topology, not hop counts.

Input orientation defines search directions only. Actual flow signs are retained
separately in saved evidence and may reverse during the event.
"""
from collections import defaultdict, deque


SCOPE_POLICY = 'confluence_divergence_corridors_v1'


def build_topology_scope(model, target):
    incoming, outgoing, incident = defaultdict(list), defaultdict(list), defaultdict(list)
    links, nodes = model['links'], model['nodes']
    for key, link in sorted(links.items()):
        outgoing[link['from_node']].append(key)
        incoming[link['to_node']].append(key)
        incident[link['from_node']].append(key)
        incident[link['to_node']].append(key)
    direct = sorted(incident[target])
    corridor_nodes, corridor_links, boundaries = {target}, set(), defaultdict(set)
    traces, queue = [], deque()
    for key in direct:
        direction = 'upstream' if links[key]['to_node'] == target else 'downstream'
        queue.append((target, key, direction, 'main', [target], []))
    # Shared paths are expanded once per search direction. Each arriving trace
    # still records its join point; cycles are checked before this shared stop.
    expanded = set()
    while queue:
        origin, key, direction, role, path_nodes, path_links = queue.popleft()
        while True:
            link = links[key]
            node = link['from_node'] if direction == 'upstream' else link['to_node']
            corridor_links.add(key)
            corridor_nodes.add(node)
            repeated = node in path_nodes
            path_nodes = path_nodes + [node]
            path_links = path_links + [key]
            reason = None
            if repeated:
                reason = 'cycle'
            elif node == target:
                reason = 'target_reached'
            elif node not in nodes:
                reason = 'missing_node_definition'
            elif link['link_type'] != 'CONDUITS':
                reason = 'facility_link'
            elif nodes[node]['node_type'] != 'JUNCTIONS':
                reason = 'special_node:' + nodes[node]['node_type']
            elif direction == 'upstream' and len(incoming[node]) > 1:
                reason = 'confluence'
            elif direction == 'downstream' and len(outgoing[node]) > 1:
                reason = 'divergence'
            elif not (incoming[node] if direction == 'upstream' else outgoing[node]):
                reason = 'terminal'
            elif (node, direction) in expanded:
                reason = 'shared_path'
            if reason:
                if reason not in ('shared_path', 'target_reached'):
                    boundaries[node].add(reason)
                traces.append({'origin_node': origin, 'direction': direction, 'role': role,
                               'nodes': path_nodes, 'links': path_links,
                               'stop_node': node, 'stop_reason': reason})
                break
            expanded.add((node, direction))
            if direction == 'downstream':
                # Tributaries entering intermediate downstream junctions are
                # traced upstream to their own previous confluence. At a stop
                # boundary only immediate interfaces are included, never traced.
                for side in incoming[node]:
                    if side != key:
                        queue.append((node, side, 'upstream', 'side_branch', [node], []))
            key = (incoming[node] if direction == 'upstream' else outgoing[node])[0]
    # Every corridor/boundary junction keeps all immediate exchange links.
    # Far endpoints allow head differences, but are not new traversal seeds.
    selected_links = corridor_links | {k for n in corridor_nodes for k in incident[n]}
    selected_nodes = corridor_nodes | {n for k in selected_links
                                      for n in (links[k]['from_node'], links[k]['to_node'])}
    missing = sorted(n for n in selected_nodes if n not in nodes)
    return {
        'policy': SCOPE_POLICY, 'target_node': target,
        'status': 'complete_for_policy' if model['available'] and target in nodes and direct and not missing else 'partial',
        'direction_basis': 'input from_node/to_node; not inferred actual flow or causal direction',
        'direct_links': direct, 'traces': traces,
        'boundary_nodes': {n: sorted(reasons) for n, reasons in sorted(boundaries.items())},
        'node_roles': {n: ('target' if n == target else 'boundary' if n in boundaries
                           else 'corridor' if n in corridor_nodes else 'interface_endpoint')
                       for n in sorted(selected_nodes)},
        'link_roles': {k: 'corridor' if k in corridor_links else 'boundary_interface'
                       for k in sorted(selected_links)},
        'missing_node_definitions': missing,
        'limitations': [
            'Boundary exchange links are included; branches beyond boundaries are not traversed.',
            'Intermediate lateral inflows are node processes, not additional direct target sources.',
            'Topology completeness does not establish causal completeness; reversals may need investigation.',
        ],
    }
