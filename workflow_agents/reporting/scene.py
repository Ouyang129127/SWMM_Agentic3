"""Assemble the agreed scene sentences from current simulation materials."""
from datetime import datetime
import json
import math
from pathlib import Path


TEMPLATE = json.loads((Path(__file__).parent / 'prompts' / 'scene_template_v1.json').read_text(encoding='utf-8'))
VERSION = TEMPLATE['version']


def _timestamp(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def _number(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('Invalid scene quantity')
    return f'{value:g}'


def _duration(minutes):
    minutes = float(minutes)
    _number(minutes)
    hours = math.floor(minutes / 60)
    remainder = minutes - hours * 60
    if math.isclose(remainder, 60., abs_tol=1e-9):
        hours += 1
        remainder = 0.
    if math.isclose(remainder, 0., abs_tol=1e-9):
        return f'{hours}小时' if hours else '0分钟'
    return (f'{hours}小时' if hours else '') + _number(remainder) + '分钟'


def _clock(value, start):
    days = (value.date() - start.date()).days
    prefix = ('' if days == 0 else '次日' if days == 1 else '前一日' if days == -1
              else f'{days}天后' if days > 1 else f'{-days}天前')
    time = (value.strftime('%H:%M:%S.%f').rstrip('0') if value.microsecond else
            value.strftime('%H:%M:%S' if value.second else '%H:%M'))
    return prefix + time


def build_scene(materials):
    """Return two prose paragraphs; clock qualifiers preserve midnight changes."""
    scene, rain = materials['scene'], materials['rain']
    start, end = (_timestamp(scene[key]) for key in ('simulation_start', 'simulation_end'))
    if end < start:
        raise ValueError('Simulation ends before it starts')
    rain_start = _timestamp(scene.get('rain_start') or scene['simulation_start'])
    rain_end = _timestamp(scene['rain_end'])
    if rain_end < rain_start:
        raise ValueError('Rainfall ends before it starts')
    model = TEMPLATE['display_aliases'].get(scene['model_display_name'], scene['model_display_name'])
    total = float(rain['total_mm'])
    _number(total)
    if total == 0:
        opening = TEMPLATE['dry_opening'].format(model=model, scenario=scene['scenario_display_name'])
        timing = f'模拟从{_clock(start, start)}开始，计算至{_clock(end, start)}，观察无降雨条件下管网和地表的变化。'
    else:
        duration = rain.get('duration_min')
        if duration is None:
            duration = (rain_end - rain_start).total_seconds() / 60
        opening = TEMPLATE['opening'].format(model=model, scenario=scene['scenario_display_name'],
            duration=_duration(duration), total=_number(total), shape=rain['shape'])
        timing = f'模拟从{_clock(start, start)}开始，'
        if end < rain_end:
            timing += f'计算至{_clock(end, start)}，覆盖降雨过程中的管网和地表变化。'
        else:
            if rain_start > start:
                timing += f'降雨自{_clock(rain_start, start)}持续至{_clock(rain_end, start)}，'
            else:
                timing += f'降雨于{_clock(rain_end, start)}结束，'
            if end > rain_end:
                coverage = _duration((end - rain_end).total_seconds() / 60)
                timing += f'计算继续至{_clock(end, start)}，覆盖降雨期间及雨停后{coverage}的管网和地表变化。'
            else:
                timing += '计算同时结束，覆盖降雨期间的管网和地表变化。'
    analyzed = any(event.get('assessment') for event in materials.get('events', []))
    outline = TEMPLATE['report_outline' if analyzed else 'report_outline_without_event_analysis']
    return [opening + timing, outline]
