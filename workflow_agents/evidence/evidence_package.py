"""The single persisted EvidenceBuilder contract; no legacy file fallback."""
import json
from pathlib import Path

from workflow_agents.verification.reference_checks import check_references
from workflow_agents.common.hashing import sha256_file
from workflow_agents.evidence.topology_scope import SCOPE_POLICY

PACKAGE_SCHEMA = 'swmm_ca2d_evidence_package'
PACKAGE_VERSION = '1.2'


def validate_package(package, model_name, run_id):
    if not isinstance(package, dict) or package.get('schema_name') != PACKAGE_SCHEMA or package.get('schema_version') != PACKAGE_VERSION:
        raise ValueError('证据包格式不受支持；请重新构建统一证据包。')
    if package.get('model_name') != model_name or package.get('run_id') != run_id:
        raise ValueError('Evidence package belongs to another model/run')
    if not isinstance(package.get('evidence_rows'), list) or not isinstance(package.get('overview'), dict):
        raise ValueError('Evidence package requires evidence_rows and overview')
    if package['overview'].get('event_scope_policy') != SCOPE_POLICY:
        raise ValueError('首轮拓扑范围已更新；请用已有模拟重建统一证据包并创建新诊断任务，无须重新模拟。')
    check_references({'model_name': model_name, 'run_id': run_id, 'claims': []}, package['evidence_rows'], model_name, run_id)
    return package


def load_package(root, verify_sources=False):
    path = root / 'evidence/evidence_package.json'
    if not path.exists():
        raise FileNotFoundError('缺少 evidence/evidence_package.json；请用已有模拟结果重新构建证据。旧CSV/首轮JSON不再作为输入。')
    package = validate_package(json.loads(path.read_text(encoding='utf-8')), root.parents[1].name, root.name)
    if verify_sources:
        for name, digest in package.get('source_hashes', {}).items():
            source = Path(name)
            if not source.is_absolute():
                source = root / source
            if not source.is_file() or sha256_file(source) != digest:
                raise ValueError(f'Evidence source changed; rebuild evidence: {name}')
        for name in package.get('missing_sources', []):
            source = Path(name)
            if not source.is_absolute():
                source = root / source
            if source.exists():
                raise ValueError(f'Previously missing source is now available; rebuild evidence: {name}')
    return package


def write_package(root, package):
    validate_package(package, root.parents[1].name, root.name)
    path = root / 'evidence/evidence_package.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(package, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)
    return path
