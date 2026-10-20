"""Reference existence checks only, not hydraulic or causal validation."""


def check_references(claims_payload, evidence_rows, model_name, run_id):
    if claims_payload.get('run_id') != run_id or claims_payload.get('model_name') != model_name:
        raise ValueError('Diagnosis belongs to a different model/run')
    ids = set()
    for row in evidence_rows:
        if str(row.get('run_id', '')) != run_id or str(row.get('source_model', '')) != model_name:
            raise ValueError('Evidence belongs to a different model/run')
        eid = str(row.get('evidence_id', '')).strip()
        if not eid or eid in ids:
            raise ValueError('Evidence IDs must be nonempty and unique')
        ids.add(eid)
    checks, claim_ids = [], set()
    for claim in claims_payload.get('claims', []):
        cid = claim.get('claim_id')
        if not cid or cid in claim_ids:
            raise ValueError('Claim IDs must be nonempty and unique')
        claim_ids.add(cid)
        refs = claim.get('evidence_ids', [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            raise ValueError(f'Malformed evidence_ids: {cid}')
        refs = list(dict.fromkeys(refs))
        missing = [ref for ref in refs if ref not in ids]
        status = 'no_references' if not refs else 'references_missing' if missing else 'references_verified'
        checks.append({'claim_id': cid, 'verification_status': status,
                       'checked_evidence_ids': refs, 'missing_evidence_ids': missing,
                       'issues': ['no evidence references'] if not refs else [f'evidence_id not found: {x}' for x in missing],
                       'next_action': 'report' if status == 'references_verified' else 'revise_diagnosis'})
    total = len(checks)
    passed = sum(c['verification_status'] == 'references_verified' for c in checks)
    return {'schema_name': 'swmm_ca2d_verification_report', 'schema_version': '0.2',
            'model_name': model_name, 'run_id': run_id, 'verification_scope': 'reference_traceability_only',
            'causal_validity_checked': False, 'claim_checks': checks,
            'summary': {'total_claims': total, 'references_verified': passed,
                        'references_missing': sum(c['verification_status'] == 'references_missing' for c in checks),
                        'no_references': sum(c['verification_status'] == 'no_references' for c in checks),
                        'reference_failure_rate': (total - passed) / total if total else None}}
