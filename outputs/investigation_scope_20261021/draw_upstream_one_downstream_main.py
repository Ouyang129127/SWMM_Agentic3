"""Preview a candidate scope using saved data; no runtime policy changes."""
import copy
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

OUT=Path(__file__).resolve().parent
RUN=OUT.parents[1]/'models/Example1/runs/diagnostic_single_4h_20mm_5min__baseline__20261020_223309_3641fc9e'
font=FontProperties(fname='C:/Windows/Fonts/msyh.ttc')
plt.rcParams.update({'font.family':font.get_name(),'axes.unicode_minus':False,'pdf.fonttype':42,'svg.fonttype':'none'})
sections=defaultdict(list); section=''
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
incoming,outgoing=defaultdict(list),defaultdict(list)
for key,(a,b) in links.items():incoming[b].append(key);outgoing[a].append(key)
target='10'
up_links=set(incoming[target]);up_nodes={links[k][0] for k in up_links}
down_nodes,down_links={target},set()
paths=[]
for first in outgoing[target]:
    key=first;trace=[target]
    while True:
        a,b=links[key];down_links.add(key);down_nodes.add(b);trace.append(b)
        if b=='18' or len(outgoing[b])!=1 or b in trace[:-1]:break
        key=outgoing[b][0]
    paths.append(trace)
selected_nodes=up_nodes|down_nodes
selected_links=up_links|down_links
assert all(a in selected_nodes and b in selected_nodes for k,(a,b) in links.items() if k in selected_links)
package=json.loads((RUN/'evidence/evidence_package.json').read_text(encoding='utf-8'))
source=next(r for r in package['evidence_rows'] if r['metric_name']=='local_process_series')
subset=copy.deepcopy(source)
for sample in subset['value']['series']:
    sample['nodes']={k:v for k,v in sample['nodes'].items() if k in selected_nodes}
    sample['links']={k:v for k,v in sample['links'].items() if k in selected_links}
size=lambda r:len(json.dumps(r,ensure_ascii=False,separators=(',',':')))
audit={'run_id':RUN.name,'target':target,'upstream_policy':'one incoming pipe per direct upstream path; retain both endpoints',
       'downstream_policy':'main path to next divergence or outfall; retain endpoints; no entering tributary expansion',
       'upstream_nodes':sorted(up_nodes,key=int),'upstream_links':sorted(up_links,key=int),
       'downstream_paths':paths,'nodes':sorted(selected_nodes,key=int),'links':sorted(selected_links,key=int),
       'excluded_nodes':sorted(set(coords)-selected_nodes,key=int),'excluded_links':sorted(set(links)-selected_links,key=int),
       'all_selected_node_series_retained':True,'samples':len(subset['value']['series']),
       'original_process_row_characters':size(source),'subset_process_row_characters':size(subset),
       'process_character_reduction_percent':round(100*(1-size(subset)/size(source)),2),
       'runtime_modified':False,'model_called':False,
       'note':'Preview only. Pipe orientation is from INP, not a finding of actual hydraulic influence. All parcel polygons are background.'}
(OUT/'upstream-one-downstream-main-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')

BLUE,ORANGE,RED,INK,MUTED,GRAY='#24778A','#BA7124','#C43D43','#253B46','#687976','#C7D0CD'
halo=[pe.withStroke(linewidth=3.3,foreground='white')]
node_offsets={'9':(8,2),'10':(-44,10),'13':(-30,1),'14':(7,8),'15':(-29,-16),'16':(-12,12),
              '17':(-34,-9),'18':(9,-1),'19':(8,3),'20':(-6,10),'21':(7,2),'22':(8,2),'23':(8,1),'24':(6,-17)}
link_offsets={'1':(-17,1),'4':(0,10),'5':(-2,13),'6':(-3,-17),'7':(-18,0),'8':(-19,-4),'10':(14,0),
              '11':(-2,-15),'12':(12,0),'13':(-1,-15),'14':(7,9),'15':(8,13),'16':(3,10)}
parcel_labels={'1':(133,510),'2':(397,533),'3':(85,309),'4':(231,355),'5':(145,126),'6':(512,204),'7':(582,364),'8':(571,80)}
parcel_colors=['#EEF0E3','#E8EEDE','#EEECE4','#E9EFE8','#F1EDE3','#E7EEE4','#F0EFE3','#E7ECE6']
def middle(points):
    lengths=[((b[0]-a[0])**2+(b[1]-a[1])**2)**.5 for a,b in zip(points,points[1:])]
    remain=sum(lengths)/2
    for a,b,length in zip(points,points[1:],lengths):
        if remain<=length:
            t=remain/length;return a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])
        remain-=length
    return points[-1]
def draw(ax,new):
    ns=selected_nodes if new else set(coords);ls=selected_links if new else set(links)
    for i,key in enumerate(sorted(parcels,key=int)):
        ax.add_patch(Polygon(parcels[key],closed=True,facecolor=parcel_colors[i],edgecolor='#BFC9BA',lw=.8,zorder=0))
        ax.text(*parcel_labels[key],f'地块{key}\n汇入 N{outlets[key]}',ha='center',va='center',fontsize=10,color='#64745F',linespacing=1.5)
    for key,(a,b) in links.items():
        pts=[coords[a]]+vertices[key]+[coords[b]];active=key in ls
        color=ORANGE if new and key in up_links else BLUE if active else GRAY
        ax.plot(*zip(*pts),color=color,lw=2.9 if active else 1.3,solid_capstyle='round',zorder=3)
        ax.annotate('L'+key,middle(pts),xytext=link_offsets[key],textcoords='offset points',ha='center',va='center',
                    fontsize=10.2,color=INK if active else '#83918E',path_effects=halo,zorder=5)
    for key,xy in coords.items():
        active=key in ns;color=ORANGE if new and key in up_nodes else BLUE if active else GRAY
        ax.scatter([xy[0]],[xy[1]],s=55 if active else 34,marker='s' if key=='18' else 'o',
                   facecolor=color if active else 'white',edgecolor=color,linewidth=1.3,zorder=6)
        ax.annotate('N'+key,xy,xytext=node_offsets[key],textcoords='offset points',fontsize=10.8,
                    color=INK if active else MUTED,va='center',path_effects=halo,zorder=7)
    ax.scatter(*coords[target],s=260,marker='*',facecolor=RED,edgecolor='white',linewidth=1,zorder=9)
    ax.set_xlim(0,680);ax.set_ylim(0,655);ax.set_aspect('equal');ax.axis('off')

fig,axes=plt.subplots(1,2,figsize=(13.6,9),facecolor='white')
fig.subplots_adjust(left=.025,right=.985,top=.78,bottom=.17,wspace=.075)
for ax,new,title,sub in [(axes[0],False,'原首轮范围','14 个节点 · 13 条管段'),
                         (axes[1],True,'候选：上游一段＋下游主支，保留节点','8 个节点 · 7 条管段')]:
    draw(ax,new)
    ax.text(0,1.10,title,transform=ax.transAxes,fontsize=13.2,color=INK)
    ax.text(0,1.045,sub,transform=ax.transAxes,fontsize=12,color=BLUE)
    ax.text(0,.996,'事件窗口 01:36:35—01:53:45｜207 个时刻',transform=ax.transAxes,fontsize=9.7,color=MUTED)
fig.text(.035,.952,'N10冒溢：上游一段、下游主支',fontsize=21,color=INK)
fig.text(.035,.907,'候选范围预览｜所选管段两端节点均保留详细过程；下游不进入沿程汇入支路',fontsize=11,color=MUTED)
handles=[Line2D([],[],marker='*',color='none',markerfacecolor=RED,markersize=13,label='冒溢节点 N10'),
         Line2D([],[],color=BLUE,marker='o',lw=2.8,markersize=5,label='纳入范围／下游主支'),
         Line2D([],[],color=ORANGE,marker='o',lw=2.8,markersize=5,label='上游一段'),
         Line2D([],[],color=GRAY,marker='o',markerfacecolor='white',lw=1.3,markersize=5,label='本方案不提取详细过程'),
         Patch(facecolor=parcel_colors[0],edgecolor='#BFC9BA',label='汇水地块')]
fig.legend(handles=handles,loc='lower center',bbox_to_anchor=(.5,.111),ncol=5,frameon=False,fontsize=9.8,columnspacing=1.4)
fig.text(.035,.080,'上游：N9 —L1→ N10；下游：N10 —L6→ N21 —L7→ N22 —L8→ N16 —L15→ N24 —L16→ N17 —L10→ N18',fontsize=10,color=INK)
fig.text(.035,.047,'同一事件、同一采样时刻：过程记录由 668,122 减至 394,894 字符（减少 40.9%；不是整份请求的 token 降幅）。',fontsize=10,color=INK)
fig.text(.035,.018,'采用本次模型示意坐标和汇水区边界，非实测地籍。仅预览候选取数范围；未改正式程序、未重新调用模型。',fontsize=9.2,color=MUTED)
for ext in ['png','pdf','svg']:fig.savefig(OUT/f'upstream-one-downstream-main.{ext}',dpi=210,facecolor='white')
plt.close(fig)
print(json.dumps(audit,ensure_ascii=True))
