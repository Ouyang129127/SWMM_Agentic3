from workflow_agents.evidence.evidence_package import PACKAGE_SCHEMA, PACKAGE_VERSION, write_package
from workflow_agents.evidence.topology_scope import SCOPE_POLICY


def save_fixture(root, rows, overview=None, source_hashes=None):
    package = {'schema_name': PACKAGE_SCHEMA, 'schema_version': PACKAGE_VERSION,
               'model_name': root.parents[1].name, 'run_id': root.name,
               'overview': dict(overview or {}, event_scope_policy=SCOPE_POLICY),
               'packages': [], 'source_hashes': source_hashes or {},
               'missing_sources': [], 'evidence_rows': rows}
    write_package(root, package)
    return package
