"""Run states end at evidence readiness; diagnosis has its own task states."""
from dataclasses import dataclass
from pathlib import Path

WORKFLOW_SCHEMA_NAME = 'swmm_ca2d_workflow_state'
WORKFLOW_SCHEMA_VERSION = '1.0'
SCENARIO_REQUEST_SCHEMA_NAME = 'swmm_ca2d_scenario_request'
RAINFALL_CONTEXT_SCHEMA_NAME = 'swmm_ca2d_rainfall_context'
START = 'START'
SCENARIO_READY = 'SCENARIO_READY'
RUN_READY = 'RUN_READY'
NO_SURFACE_INFLOW = 'NO_SURFACE_INFLOW'
EVIDENCE_READY = 'EVIDENCE_READY'
FAILED = 'FAILED'
NEXT_STAGE_BY_STATE = {
    START: 'scenario', SCENARIO_READY: 'simulation', RUN_READY: 'evidence_building',
    NO_SURFACE_INFLOW: 'evidence_building', EVIDENCE_READY: 'initial_diagnosis', FAILED: None,
}
TARGET_STATE_BY_STAGE = {'scenario': SCENARIO_READY, 'simulation': RUN_READY, 'evidence_building': EVIDENCE_READY}
COMPLETED_STAGE_BY_STATE = {SCENARIO_READY: 'scenario', RUN_READY: 'simulation', EVIDENCE_READY: 'evidence_building'}


@dataclass(frozen=True)
class RunArtifacts:
    scenario_request: Path
    run_summary: Path
    run_metadata: Path
    swmm_node_flooding: Path
    swmm_nodes: Path
    swmm_links: Path
    ca2d_surface_depth: Path
    evidence_package: Path


def artifacts_for_run(root):
    return RunArtifacts(
        root / 'scenario_request.json', root / 'summary.json', root / 'run.yaml',
        root / 'swmm/node_flooding.tsv', root / 'swmm/nodes.tsv', root / 'swmm/links.tsv',
        root / 'ca2d/surface_depth.tsv', root / 'evidence/evidence_package.json')
