"""First-pass scope: one upstream link, downstream to a retained boundary.

Directions use input connectivity, not observed flow or inferred causality.
No tributary traversal or automatic boundary-interface expansion occurs here.
"""
from collections import defaultdict


SCOPE_POLICY = 'upstream_one_segment_downstream_confluence_v1'


def build_topology_scope(model, target):
    incoming, outgoing, incident = defaultdict(list), defaultdict(list), defaultdict(list)
    links, nodes = model['links'], model['nodes']
    for key, link in sorted(links.items()):
        outgoing[link['from_node']].append(key)
        incoming[link['to_node']].append(key)
        incident[link['from_node']].append(key)
        incident[link['to_node']].append(key)

    direct = sorted(incident[target])
    # Preserve every target exchange for direct-source accounting, including
    # direct endpoints when the target itself has several downstream exits.
    selected_links = set(direct)
    selected_nodes = {target} | {n for k in direct
                                      for n in (links[k]['from_node'], links[k]['to_node'])}
    traversed_nodes = {target}
    boundaries, boundary_links = defaultdict(set), defaultdict(set)
    traces, unresolved_branches = [], set()
    target_branches = len(outgoing[target]) > 1
    if target_branches:
        # Do not choose an exit or silently traverse every downstream branch.
        boundaries[target].add('downstream_branch_unresolved')
        unresolved_branches.add(target)
        traces.append({'origin_node': target, 'direction': 'downstream', 'role': 'main',
                       'nodes': [target], 'links': [], 'stop_node': target,
                       'stop_reason': 'downstream_branch_unresolved'})

    for first in direct:
        direction = 'upstream' if links[first]['to_node'] == target else 'downstream'
        if direction == 'downstream' and target_branches:
            continue
        key, path_nodes, path_links = first, [target], []
        while True:
            link = links[key]
            node = link['from_node'] if direction == 'upstream' else link['to_node']
            selected_links.add(key)
            selected_nodes.add(node)
            traversed_nodes.add(node)
            repeated = node in path_nodes
            path_nodes = path_nodes + [node]
            path_links = path_links + [key]
            reason = None
            if repeated:
                reason = 'cycle'
            elif node not in nodes:
                reason = 'missing_node_definition'
            elif link['link_type'] != 'CONDUITS':
                reason = 'facility_link'
                boundary_links[key].add(reason)
            elif nodes[node]['node_type'] != 'JUNCTIONS':
                reason = 'special_node:' + nodes[node]['node_type']
            elif len(incoming[node]) > 1:
                reason = 'confluence'
            elif direction == 'upstream':
                reason = 'terminal' if not incoming[node] else 'upstream_one_segment'
            elif not outgoing[node]:
                reason = 'terminal'
            elif len(outgoing[node]) > 1:
                reason = 'downstream_branch_unresolved'
                unresolved_branches.add(node)
            if reason:
                boundaries[node].add(reason)
                traces.append({'origin_node': target, 'direction': direction, 'role': 'main',
                               'nodes': path_nodes, 'links': path_links,
                               'stop_node': node, 'stop_reason': reason})
                break
            key = outgoing[node][0]

    missing = sorted(n for n in selected_nodes if n not in nodes)
    complete = model['available'] and target in nodes and direct and not missing and not unresolved_branches
    return {
        'policy': SCOPE_POLICY, 'target_node': target,
        'status': 'complete_for_policy' if complete else 'partial',
        'direction_basis': 'input from_node/to_node; not inferred actual flow or causal direction',
        'direct_links': direct, 'traces': traces,
        'boundary_nodes': {n: sorted(reasons) for n, reasons in sorted(boundaries.items())},
        'boundary_links': {k: sorted(reasons) for k, reasons in sorted(boundary_links.items())},
        'unresolved_branches': sorted(unresolved_branches),
        'node_roles': {n: ('target' if n == target else 'boundary' if n in boundaries
                           else 'corridor' if n in traversed_nodes else 'direct_endpoint')
                       for n in sorted(selected_nodes)},
        'link_roles': {k: 'facility_boundary' if k in boundary_links else 'corridor'
                       for k in sorted(selected_links)},
        'missing_node_definitions': missing,
        'limitations': [
            'Upstream scope retains only each target incoming link and its endpoint.',
            'Downstream scope retains the first confluence, facility, special node or terminal boundary.',
            'No side branches or extra links at corridor/boundary nodes are automatically included.',
            'A pure downstream split stops with partial coverage; selecting further paths requires supplementation.',
            'Target direct exchanges remain visible when the target itself splits; no branch is followed beyond them.',
            'Intermediate lateral inflows are node processes, not additional direct target sources.',
            'Topology completeness does not establish causal completeness; reversals may need investigation.',
        ],
    }
