"""Read-only scope audit and map from the specified run's frozen evidence."""
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D
from matplotlib.patches import Polygon, Patch

PROJECT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
RUN = PROJECT / 'models/Example1/runs/diagnostic_single_4h_20mm_5min__baseline__20261020_223309_3641fc9e'
TASK = RUN / 'diagnosis_tasks/4eab2228f2a54ef180355bcb8f31bbaf'
FONT = FontProperties(fname='C:/Windows/Fonts/msyh.ttc')
plt.rcParams.update({'font.family': FONT.get_name(), 'axes.unicode_minus': False,
                     'svg.fonttype': 'none', 'pdf.fonttype': 42})

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

sections = defaultdict(list)
section = ''
for raw in (RUN / 'swmm/model_with_event.inp').read_text(encoding='utf-8-sig').splitlines():
    line = raw.split(';', 1)[0].strip()
    if line.startswith('[') and line.endswith(']'):
        section = line[1:-1].upper()
    elif line:
        sections[section].append(line.split())
coords = {p[0]: (float(p[1]), float(p[2])) for p in sections['COORDINATES']}
links = {p[0]: (p[1], p[2]) for p in sections['CONDUITS']}
vertices, parcels = defaultdict(list), defaultdict(list)
for p in sections['VERTICES']:
    vertices[p[0]].append((float(p[1]), float(p[2])))
for p in sections['POLYGONS']:
    parcels[p[0]].append((float(p[1]), float(p[2])))
outlets = {p[0]: p[2] for p in sections['SUBCATCHMENTS']}
package = read(RUN / 'evidence/evidence_package.json')
assert package['run_id'] == RUN.name
event_rows = [r for r in package['evidence_rows'] if r.get('event_id') == '10:E001']
structure = next(r['value'] for r in event_rows if r['metric_name'] == 'local_structure')
process = next(r['value'] for r in event_rows if r['metric_name'] == 'local_process_series')
initial_nodes, initial_links = set(structure['nodes']), set(structure['links'])
supplements = {}
source_files = ['swmm/model_with_event.inp', 'evidence/evidence_package.json']
for path in sorted(TASK.glob('supplement_*.json')):
    supplement = read(path)
    assert supplement['run_id'] == RUN.name
    for rel, digest in supplement['source_hashes'].items():
        assert hashlib.sha256((RUN / rel).read_bytes()).hexdigest() == digest, rel
    source_files.append(str(path.relative_to(RUN)))
    for row in supplement['evidence_rows']:
        scope = row['value']['resolved_scope']
        observed_nodes = set().union(*(set(s.get('nodes', {})) for s in row['value']['series']))
        observed_links = set().union(*(set(s.get('links', {})) for s in row['value']['series']))
        assert observed_nodes == set(scope['nodes'])
        assert observed_links == set(scope['links'])
        supplements[row['metric_name']] = {
            'nodes': sorted(observed_nodes, key=int), 'links': sorted(observed_links, key=int),
            'start': row['value']['actual_time_start'], 'end': row['value']['actual_time_end'],
            'samples': len(row['value']['series']), 'evidence_id': row['evidence_id'],
            'file': str(path.relative_to(RUN)),
        }
storage = supplements['storage_volume_process']
local = supplements['local_process_series']
assert storage['nodes'] == local['nodes'] and storage['links'] == local['links']
full = supplements['fullness_time_series']
all_supp_nodes = set(storage['nodes']) | set(full['nodes'])
all_supp_links = set(storage['links']) | set(full['links'])
assert all_supp_nodes <= initial_nodes and all_supp_links <= initial_links

audit = {
    'run_id': RUN.name, 'event_id': '10:E001', 'overflow_nodes': ['10'],
    'initial': {'nodes': sorted(initial_nodes, key=int), 'links': sorted(initial_links, key=int),
                'start': process['series'][0]['time'], 'end': process['series'][-1]['time'],
                'samples': len(process['series'])},
    'supplements': supplements,
    'new_spatial_nodes': sorted(all_supp_nodes - initial_nodes),
    'new_spatial_links': sorted(all_supp_links - initial_links),
    'parcels': {k: {'outlet_node': outlets[k], 'polygon': v} for k, v in parcels.items()},
    'source_hashes_verified': True,
    'geometry_note': 'Run INP COORDINATES/VERTICES/POLYGONS; anonymous schematic local coordinates, not surveyed cadastral geometry.',
    'sources': source_files,
}
(OUT / 'scope_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
with (OUT / 'object_membership.csv').open('w', encoding='utf-8-sig', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['object_type', 'object_id', 'first_investigation', 'storage_and_process_supplement', 'fullness_supplement', 'overflow'])
    for kind, objects in [('node', coords), ('link', links)]:
        for obj in sorted(objects, key=int):
            writer.writerow([kind, obj, obj in (initial_nodes if kind == 'node' else initial_links),
                             obj in set(storage['nodes' if kind == 'node' else 'links']),
                             obj in set(full['nodes' if kind == 'node' else 'links']),
                             kind == 'node' and obj == '10'])

BLUE, ORANGE, PURPLE, RED = '#24778A', '#BA7124', '#7963A8', '#C43D43'
INK, MUTED, BGPIPE = '#253B46', '#687976', '#CCD3D2'
HALO = [pe.withStroke(linewidth=3.3, foreground='#FFFFFF')]
node_offsets = {'9': (8, 2), '10': (-44, 10), '13': (-30, 1), '14': (7, 8),
                '15': (-29, -16), '16': (-12, 12), '17': (-34, -9), '18': (9, -1),
                '19': (8, 3), '20': (-6, 10), '21': (7, 2), '22': (8, 2),
                '23': (8, 1), '24': (6, -17)}
link_offsets = {'1': (-17, 1), '4': (0, 10), '5': (-2, 13), '6': (-3, -17),
                '7': (-18, 0), '8': (-19, -4), '10': (14, 0), '11': (-2, -15),
                '12': (12, 0), '13': (-1, -15), '14': (7, 9), '15': (8, 13), '16': (3, 10)}
parcel_labels = {'1': (133, 510), '2': (397, 533), '3': (85, 309), '4': (231, 355),
                 '5': (145, 126), '6': (512, 204), '7': (582, 364), '8': (571, 80)}
parcel_colors = ['#EEF0E3', '#E8EEDE', '#EEECE4', '#E9EFE8', '#F1EDE3', '#E7EEE4', '#F0EFE3', '#E7ECE6']

def halfway(points):
    lengths = [((b[0]-a[0])**2+(b[1]-a[1])**2)**0.5 for a,b in zip(points,points[1:])]
    remain = sum(lengths)*0.5
    for a,b,length in zip(points,points[1:],lengths):
        if remain <= length:
            t = remain / length
            return (a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1]))
        remain -= length
    return points[-1]

def draw_map(ax, selected_nodes, selected_links, color):
    for index, key in enumerate(sorted(parcels, key=int)):
        ax.add_patch(Polygon(parcels[key], closed=True, facecolor=parcel_colors[index],
                             edgecolor='#BFC9BA', linewidth=0.8, zorder=0))
        x,y = parcel_labels[key]
        ax.text(x,y,f'地块{key}\n汇入 N{outlets[key]}',ha='center',va='center',
                fontsize=9.3,color='#64745F',linespacing=1.5,zorder=1)
    for key, (start,end) in links.items():
        points = [coords[start]]+vertices[key]+[coords[end]]
        active = key in selected_links
        ax.plot(*zip(*points), color=color if active else BGPIPE, linewidth=2.5 if active else 1.25,
                solid_capstyle='round',zorder=3)
        x,y = halfway(points)
        ax.annotate(f'L{key}',(x,y),xytext=link_offsets[key],textcoords='offset points',
                    ha='center',va='center',fontsize=9.4,color=INK if active else '#83918E',
                    path_effects=HALO,zorder=5)
    for key, (x,y) in coords.items():
        active = key in selected_nodes
        ax.scatter([x],[y],s=43 if active else 31,marker='s' if key=='18' else 'o',
                   facecolor=color if active else '#FFFFFF',edgecolor=color if active else '#A5B0AD',
                   linewidth=1.25,zorder=6)
        ax.annotate(f'N{key}',(x,y),xytext=node_offsets[key],textcoords='offset points',
                    fontsize=10,color=INK if active else MUTED,va='center',path_effects=HALO,zorder=8)
    x,y = coords['10']
    ax.scatter([x],[y],s=230,marker='*',facecolor=RED,edgecolor='#FFFFFF',linewidth=0.9,zorder=9)
    ax.set_xlim(0,680);ax.set_ylim(0,655);ax.set_aspect('equal');ax.axis('off')

fig, axes = plt.subplots(1,3,figsize=(18.8,8.6),facecolor='white')
fig.subplots_adjust(left=0.022,right=0.985,top=0.77,bottom=0.155,wspace=0.08)
panels = [
    (initial_nodes, initial_links, BLUE, 'A  首轮调查', '14 个节点 · 13 条管段（全网）', '01:36:35—01:53:45｜207 个时刻'),
    (set(storage['nodes']),set(storage['links']),ORANGE,'B  补证：储量与水力过程','5 个节点 · 4 条管段','01:25:00—02:05:00｜481 个时刻'),
    (set(full['nodes']),set(full['links']),PURPLE,'C  补证：管段充满度','4 条管段；未提取节点时序','01:25:00—02:05:00｜481 个时刻'),
]
for ax,(ns,ls,color,title,count,time) in zip(axes,panels):
    draw_map(ax,ns,ls,color)
    ax.text(0.0,1.12,title,transform=ax.transAxes,fontsize=15,color=INK,weight='medium')
    ax.text(0.0,1.065,count,transform=ax.transAxes,fontsize=11,color=color)
    ax.text(0.0,1.015,time,transform=ax.transAxes,fontsize=9.8,color=MUTED)
fig.text(0.032,0.955,'节点10冒溢：首轮调查与后续补证范围',fontsize=21,color=INK,weight='medium')
fig.text(0.032,0.913,'Example1 · 20 mm 单峰雨 · 事件 10:E001 · 模拟日期 2024-01-01',fontsize=11.5,color=MUTED)
legend=[Line2D([],[],marker='*',markersize=13,color='none',markerfacecolor=RED,label='冒溢节点 N10'),
        Line2D([],[],color=BLUE,lw=2.6,marker='o',markersize=5,label='首轮调查'),
        Line2D([],[],color=ORANGE,lw=2.6,marker='o',markersize=5,label='储量／过程补证'),
        Line2D([],[],color=PURPLE,lw=2.6,label='充满度补证'),
        Line2D([],[],color=BGPIPE,lw=1.5,marker='o',markerfacecolor='white',markersize=5,label='本面板未提取的对象'),
        Patch(facecolor=parcel_colors[0],edgecolor='#BFC9BA',label='汇水地块边界')]
fig.legend(handles=legend,loc='lower center',bbox_to_anchor=(0.5,0.095),ncol=6,frameon=False,fontsize=10,
           handlelength=2,columnspacing=1.6)
fig.text(0.035,0.064,'N = 节点，L = 管段；N18 为出流口。B、C 全部位于 A 范围内：空间新增 0 节点、0 管段。',fontsize=10.5,color=INK)
fig.text(0.035,0.032,'地块仅作空间背景；采用本次 INP 中的示意坐标与多边形，不代表实测地籍边界。补证图表示已提取数据的范围，不表示补证后的诊断已完成。',fontsize=9.5,color=MUTED)
for ext in ['png','svg','pdf']:
    fig.savefig(OUT/f'investigation-scope.{ext}',dpi=210,facecolor='white')
plt.close(fig)
print(json.dumps({'output':str(OUT),'initial_nodes':len(initial_nodes),'initial_links':len(initial_links),
                  'supplement_nodes':sorted(all_supp_nodes,key=int),'supplement_links':sorted(all_supp_links,key=int),
                  'new_nodes':audit['new_spatial_nodes'],'new_links':audit['new_spatial_links']},ensure_ascii=False))
