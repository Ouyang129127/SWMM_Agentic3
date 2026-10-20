"""Bind the agreed rainfall prose to measured rainfall intervals."""
import json
from pathlib import Path

from workflow_agents.reporting.scene import _clock, _number, _timestamp


TEMPLATE = json.loads((Path(__file__).parent / 'prompts' / 'rain_template_v1.json').read_text(encoding='utf-8'))
VERSION = TEMPLATE['version']


def build_rain(materials):
    rain, scene = materials['rain'], materials['scene']
    total = float(rain['total_mm'])
    _number(total)
    if total == 0:
        return [TEMPLATE['dry']]
    shape = rain['shape']
    peak_keys = ('peak_start', 'peak_interval_min', 'peak_mm_h')
    if not all(key in rain for key in peak_keys):
        # Sparse historical materials retain only their observed shape.
        return [f'本场降雨为{shape}。']
    origin = _timestamp(scene.get('rain_start') or scene['simulation_start'])
    slots = dict(shape=shape.removesuffix('降雨'),
                 peak_label='雨峰' if shape == '单峰降雨' else '主要雨峰',
                 peak_time=_clock(_timestamp(rain['peak_start']), origin),
                 interval=_number(rain['peak_interval_min']),
                 intensity=f"{float(rain['peak_mm_h']):.2f}")
    template = ('peak_opening' if shape in ('单峰降雨', '双峰降雨', '多峰降雨')
                else 'uniform_opening' if shape == '近似均匀降雨' else 'other_opening')
    opening = TEMPLATE[template].format(**slots)
    window_keys = ('window_minutes', 'window_start', 'window_end', 'window_mm', 'window_share_percent')
    evolution = rain.get('evolution_sentence', '')
    if not all(key in rain for key in window_keys):
        return [opening] + ([evolution] if evolution else [])
    window = TEMPLATE['window'].format(window=_number(rain['window_minutes']),
        start=_clock(_timestamp(rain['window_start']), origin),
        end=_clock(_timestamp(rain['window_end']), origin),
        amount=f"{float(rain['window_mm']):.1f}", share=f"{float(rain['window_share_percent']):.0f}")
    duration = float(rain.get('duration_min', 0))
    width = float(rain['window_minutes'])
    # Compare the strongest window with an even distribution of the same total.
    # This is a prose selection rule, not a rainfall grade or warning threshold.
    concentrated = (0 < width < duration and
                    float(rain['window_share_percent']) >= 1.5 * width / duration * 100)
    ending = TEMPLATE['concentrated'] if concentrated else ''
    return [opening, evolution + window + ending]
