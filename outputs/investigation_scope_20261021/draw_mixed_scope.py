"""Preview: one upstream pipe and one downstream corridor, retaining nodes."""
import ast
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

OUT = Path(__file__).resolve().parent
RUN = OUT.parents[1] / 'models/Example1/runs/diagnostic_single_4h_20mm_5min__baseline__20261020_223309_3641fc9e'
font = FontProperties(fname='C:/Windows/Fonts/msyh.ttc')
plt.rcParams.update({'font.family':font.get_name(),'axes.unicode_minus':False,'svg.fonttype':'none','pdf.fonttype':42})
sections=defaultdict(list)
section=''
for raw in (RUN/'swmm/model_with_event.inp').read_text(encoding='utf-8-sig').splitlines():
    line=raw.split(';',1)[0].strip()
    if line.startswith('[') and line.endswith(']'):section=line[1:-1].upper()
    elif line:sections[section].append(line.split())
coords={p[0]:(float(p[1]),float(p[2])) for p in sections['COORDINATES']}
links={p[0]:(p[1],p[2]) for p in sections['CONDUITS']}
vertices,parcels=defaultdict(list),defaultdict(list)
for p in sections['VERTICES']:vertices[p[0]].append((float(p[1]),float(p[2])))
for p in sections['POLYGONS']:parcels[p[0]].append((float(p[1]),float(p[2])))
outlets={p[0]:p[2] for p in sections['SUBCATCHMENTS']}

# Reuse only the already-reviewed drawing definitions and label placements.
tree=ast.parse((OUT/'draw_scope.py').read_text(encoding='utf-8'))
assignments={'BLUE','INK','HALO','node_offsets','link_offsets','parcel_labels','parcel_colors'}
for node in tree.body:
    allowed=isinstance(node,ast.FunctionDef) and node.name in {'halfway','draw_map'}
    if isinstance(node,ast.Assign):
        targets={n.id for target in node.targets for n in ast.walk(target) if isinstance(n,ast.Name)}
        allowed=bool(targets & assignments)
    if allowed:exec(compile(ast.Module(body=[node],type_ignores=[]),'<reviewed drawing definition>','exec'))

target='10'
up_links={k for k,(a,b) in links.items() if b==target}
up_nodes={target}|{links[k][0] for k in up_links}
incoming,outgoing=defaultdict(list),defaultdict(list)
for key,(a,b) in links.items():incoming[b].append(key);outgoing[a].append(key)
down_links,down_nodes=set(),{target}
outfalls={p[0] for p in sections['OUTFALLS']}
front=list(outgoing[target])
while front:
    key=front.pop()
    if key in down_links:continue
    down_links.add(key)
    node=links[key][1]
    down_nodes.add(node)
    if node not in outfalls and len(outgoing[node])==1:front.extend(outgoing[node])
selected_nodes=up_nodes|down_nodes
selected_links=up_links|down_links
assert selected_nodes=={'9','10','21','22','16','24','17','18'}
assert selected_links=={'1','6','7','8','15','16','10'}
meta={'run_id':RUN.name,'target_node':target,'policy':'upstream_one_pipe_downstream_corridor_nodes_retained',
      'upstream_nodes':sorted(up_nodes,key=int),'upstream_links':sorted(up_links,key=int),
      'downstream_nodes':sorted(down_nodes,key=int),'downstream_links':sorted(down_links,key=int),
      'all_selected_nodes':sorted(selected_nodes,key=int),'all_selected_links':sorted(selected_links,key=int),
      'excluded_nodes':sorted(set(coords)-selected_nodes,key=int),'excluded_links':sorted(set(links)-selected_links,key=int),
      'note':'Preview only; no workflow changes or model calls. Schematic catchments are background, not evidence of surface attribution.'}
(OUT/'mixed-scope-preview.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')

fig,ax=plt.subplots(figsize=(10.2,10.0),facecolor='white')
fig.subplots_adjust(left=0.045,right=0.97,top=0.79,bottom=0.17)
draw_map(ax,selected_nodes,selected_links,ORANGE)
for key in up_links:
    points=[coords[links[key][0]]]+vertices[key]+[coords[links[key][1]]]
    ax.plot(*zip(*points),color=BLUE,linewidth=2.8,solid_capstyle='round',zorder=4)
for node in up_nodes-{target}:
    ax.scatter(*coords[node],s=43,facecolor=BLUE,edgecolor=BLUE,linewidth=1.25,zorder=7)
fig.text(0.065,0.95,'N10：上游一段，下游一支，保留节点',fontsize=19,color=INK)
fig.text(0.065,0.906,'首轮候选范围：8 个节点、7 条管段｜原范围：14 个节点、13 条管段',fontsize=11.5,color=MUTED)
fig.text(0.065,0.868,'上游：N9 — L1 → N10',fontsize=12,color=BLUE)
fig.text(0.065,0.831,'下游：N10 → N21 → N22 → N16 → N24 → N17 → N18（出流口）',fontsize=11.5,color=ORANGE)
legend=[Line2D([],[],marker='*',markersize=13,color='none',markerfacecolor=RED,label='冒溢节点 N10'),
        Line2D([],[],color=BLUE,lw=2.6,marker='o',markersize=5,label='上游一段，含 N9'),
        Line2D([],[],color=ORANGE,lw=2.6,marker='o',markersize=5,label='下游主支，含沿途及末端节点'),
        Line2D([],[],color=BGPIPE,lw=1.4,marker='o',markerfacecolor='white',markersize=5,label='本方案不纳入详细过程'),
        Patch(facecolor=parcel_colors[0],edgecolor='#BFC9BA',label='汇水地块背景')]
fig.legend(handles=legend,loc='lower center',bbox_to_anchor=(0.5,0.108),ncol=3,frameon=False,fontsize=10,
           columnspacing=1.2,handlelength=2)
fig.text(0.065,0.08,'不进入侧支：N19—N20、N13—N14—N15、N23；相应侧支管段也不纳入。',fontsize=10.3,color=INK)
fig.text(0.065,0.05,'本例 N9 已是上游端点，因此“上游一段”与“上游一支”恰好得到相同上游范围。',fontsize=10.0,color=MUTED)
fig.text(0.065,0.022,'依据本次运行的 INP 示意几何绘制；图为范围预览，尚未修改程序或重新诊断。',fontsize=9.5,color=MUTED)
for ext in ['png','svg','pdf']:fig.savefig(OUT/f'upstream-one-downstream-branch.{ext}',dpi=200,facecolor='white')
plt.close(fig)
print(json.dumps(meta,ensure_ascii=True))
