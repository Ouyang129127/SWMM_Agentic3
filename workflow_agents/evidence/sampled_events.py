"""Overflow episodes at saved-sample resolution; no interpolation or merging."""
import math
from datetime import datetime


def identify_events(records, run_id, model_name, tolerance=1e-9):
    """Left-sample intervals; terminal samples have no additional duration.

    End is the first non-positive sample (exclusive), or the last saved time.
    Volume uses the same left-rectangle convention, not a solver water budget.
    """
    by_node = {}
    for row in records:
        node = str(row['node_id'])
        stamp = datetime.fromisoformat(str(row.get('DateTime') or f"{row['date']} {row['time']}"))
        flow = float(row['flow_Ls'])
        if not math.isfinite(flow):
            raise ValueError(f'Non-finite flooding: {node} {stamp}')
        by_node.setdefault(node, []).append((stamp, flow))
    events = []
    for node, samples in sorted(by_node.items()):
        samples.sort()
        deltas = [(b[0] - a[0]).total_seconds() for a, b in zip(samples, samples[1:])]
        if any(dt <= 0 for dt in deltas):
            raise ValueError(f'Duplicate timestamps: {node}')
        if deltas and (any(abs(dt - deltas[0]) > 1e-6 for dt in deltas[:-1]) or deltas[-1] > deltas[0] + 1e-6):
            raise ValueError(f'Irregular saved timestamps: {node}; explicit gap handling required')
        start = None
        number = 0
        for index in range(len(samples) + 1):
            positive = index < len(samples) and samples[index][1] > tolerance
            if positive and start is None:
                start = index
            if not positive and start is not None:
                number += 1
                stop = min(index, len(samples) - 1)
                peak = max(range(start, index), key=lambda i: samples[i][1])
                events.append({
                    'event_id': f'{node}:E{number:03d}', 'node_id': node,
                    'run_id': run_id, 'model_name': model_name, 'occurrence': number,
                    'start': samples[start][0].isoformat(sep=' '),
                    'end': samples[stop][0].isoformat(sep=' '),
                    'last_positive_time': samples[index - 1][0].isoformat(sep=' '),
                    'peak_time': samples[peak][0].isoformat(sep=' '),
                    'peak_flooding_Ls': samples[peak][1],
                    'duration_minutes': sum(deltas[start:stop]) / 60,
                    'estimated_volume_m3': sum(samples[i][1] * deltas[i] for i in range(start, stop)) / 1000,
                    'positive_sample_count': index - start,
                    'left_censored': start == 0, 'right_censored': index == len(samples),
                    'saved_step_seconds': deltas[0] if deltas else None,
                    'integration_method': 'left_sample_rectangle',
                })
                start = None
    return {'schema_name': 'sampled_overflow_events', 'schema_version': '0.1',
            'run_id': run_id, 'model_name': model_name, 'positive_tolerance_Ls': tolerance,
            'resolution': 'saved_samples', 'events': events}
