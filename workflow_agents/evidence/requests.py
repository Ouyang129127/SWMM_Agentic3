from workflow_agents.evidence.evidence_selection import lookup_evidence

def evidence_plan(rows, visible_ids, requests, root=None, request_outcomes=()):
    visible = set(visible_ids)
    plan = []
    for request in requests:
        if root is not None and request.get('metric_name'):
            from workflow_agents.evidence.saved_result_evidence import METRICS, FIELDS, plan_request
            # Prefer event-scoped saved-result calculations to a whole-run
            # scalar, including recognized requests from older diagnoses.
            if (request.get('metric_name') in METRICS and request.get('tool') in {'unavailable', 'saved_result_extract'} and not request.get('evidence_id')
                    or request.get('metric_name') == 'max_fullness' and (request.get('event_id') or request.get('event_ids')) and not request.get('evidence_id')
                    or request.get('tool') == 'saved_result_extract'):
                item = plan_request(root, rows, {k: v for k, v in request.items() if k in FIELDS})
                if item.get('requires_extraction'):
                    from workflow_agents.evidence.saved_result_evidence import _hash_sources
                    outcome = next((o for o in request_outcomes if o['request_signature'] == item['request_signature']), None)
                    if outcome and _hash_sources(root, outcome['source_hashes']) == outcome['source_hashes']:
                        item.update(status=outcome['status'], reason=outcome['reason'], requires_extraction=False)
                ids = item.get('matching_evidence_ids', [])
                new = sorted(set(ids) - visible)
                if ids and not item.get('requires_extraction'):
                    item['status'] = 'available' if new else 'already_visible'
                item.update(request=request, matching_evidence_ids=ids, new_evidence_ids=new)
                plan.append(item)
                continue
        if request['tool'] == 'unavailable':
            ids, new, status = [], [], 'unavailable'
        else:
            matches = lookup_evidence(rows, request)['rows']
            ids = sorted({r['evidence_id'] for r in matches})
            new = sorted(set(ids) - visible)
            status = 'available' if new else 'already_visible' if ids else 'no_match'
        plan.append({'request': request, 'status': status,
                     'matching_evidence_ids': ids, 'new_evidence_ids': new})
    return plan

def apply_evidence_plan(state, plan):
    state['evidence_plan'] = plan
    state['pending_evidence_requests'] = any(p['status'] == 'available' for p in plan)
    state['unresolved_evidence_requests'] = [p for p in plan if p['status'] in {'unavailable', 'no_match'}]
    state['evidence_closure'] = ('actionable' if state['pending_evidence_requests'] else
                                 'capability_gaps' if state['unresolved_evidence_requests'] else
                                 'already_visible' if plan else 'no_requests')
