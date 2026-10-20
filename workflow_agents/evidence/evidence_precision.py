"""Round derived evidence after calculation, never the saved solver inputs."""
import copy


PRECISION_POLICY = {
    'policy': 'derived_evidence_decimal3_v1',
    'decimal_places': 3,
    'small_nonzero': 'preserve original precision when abs(value) < 0.001',
    'parameters_and_thresholds': 'unchanged',
    'calculation_order': 'detect events and calculate statistics before rounding',
    'interpretation': 'Rounded evidence values are approximate; use saved sources for recalculation.',
}

# These are inputs/definitions, including the geometry of supplementary evidence.
EXACT_FIELDS = frozenset({
    'threshold', 'source_threshold', 'positive_tolerance_Ls', 'depth_reference_m',
    'input_fields', 'cross_section_fields', 'geometry_fields', 'roughness',
    'resolved_scope', 'scope_request', 'node_cell_mapping', 'native_metadata',
})
EXACT_METRICS = frozenset({'local_structure', 'facilities_and_storage'})


def round_value(value):
    """Keep JSON types and small signed quantities; do not pad with zeros."""
    if isinstance(value, dict):
        return {key: copy.deepcopy(item) if key in EXACT_FIELDS else round_value(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [round_value(item) for item in value]
    if isinstance(value, float):
        if 0 < abs(value) < 0.001:
            return value
        return round(value, 3)
    return value


def round_evidence_row(row):
    """Keep provenance, thresholds and evidence IDs intact; round only results."""
    result = copy.deepcopy(row)
    if row['metric_name'] not in EXACT_METRICS:
        result['value'] = round_value(row['value'])
    return result


def uses_decimal3(package):
    return package.get('numeric_precision') == PRECISION_POLICY
