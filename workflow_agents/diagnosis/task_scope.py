"""Bind the original question to a global, node or event scope."""
import re


def select_scope(message, node_ids, node_id=None, event_id=None):
    if node_id is not None and node_id not in node_ids:
        raise ValueError(f'Unknown node: {node_id}')
    # Chinese text commonly touches identifiers, e.g. 分析P6; do not use \w here.
    mentioned = [n for n in sorted(node_ids) if re.search(r'(?<![A-Za-z0-9_-])' + re.escape(n) + r'(?![A-Za-z0-9_-])', message)]
    if node_id:
        mentioned = [node_id]
    if len(mentioned) > 1:
        raise ValueError('Multiple nodes specified; please select a node or request a global report')
    if not mentioned and re.search(r'(?:节点|node)\s*[:：]?\s*[A-Za-z0-9_-]+', message, re.I):
        raise ValueError('Requested node could not be matched; specify a valid node ID')
    selected = mentioned[0] if mentioned else None
    found = re.findall(r'([A-Za-z0-9_-]+:E\d+)', message)
    if len(set(found)) > 1:
        raise ValueError('Multiple events specified')
    event_id = event_id or (found[0] if found else None)
    if event_id:
        event_node = event_id.rsplit(':E', 1)[0]
        if event_node not in node_ids or (selected and selected != event_node):
            raise ValueError('Event and node do not match')
        selected = event_node
    if selected is None and any(x in message for x in ('该节点', '这个节点', '该点', '这一点', '该事件', '这次冒溢')):
        raise ValueError('Specify the node/event referenced by this follow-up')
    return {'scope': 'event' if event_id else 'node' if selected else 'global',
            'node_id': selected, 'event_id': event_id, 'original_question': message}
