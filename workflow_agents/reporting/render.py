"""Escaped, self-contained HTML with deterministic tables, figures and navigation."""
import base64
import html
from pathlib import Path

from workflow_agents.reporting.materials import SECTIONS, TITLES
from workflow_agents.reporting.narrative import expand
from workflow_agents.reporting.surface import BASIS as SURFACE_BASIS


CSS='''
:root{--ink:#223d35;--muted:#64776e;--accent:#087b72;--rule:#dae4de}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#edf2ed;color:var(--ink);font:14px/1.85 "Microsoft YaHei","Noto Sans CJK SC",sans-serif}
.layout{max-width:1330px;margin:auto;display:grid;grid-template-columns:170px minmax(0,1fr);gap:32px;padding:40px 24px}aside{align-self:start;position:sticky;top:30px}
.brand{font-weight:700;font-size:16px;color:var(--accent);margin-bottom:22px}nav a{display:block;padding:10px 0;text-decoration:none;color:var(--muted);border-bottom:1px solid var(--rule)}
main{min-width:0;padding:50px 54px;background:#fff;border:1px solid var(--rule);box-shadow:0 8px 36px #24443808}h1{font-size:38px;line-height:1.3;margin:0 0 14px;font-weight:600}h1 small{display:block;font-size:24px;margin-top:10px}
.subtitle,.meta{color:var(--muted)}.meta{font-size:11px;line-height:1.7}.intro{font-size:16px;margin:26px 0}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;border-block:1px solid var(--rule);padding:20px 0}.metric small{display:block;color:var(--muted);font-size:11px}.metric strong{display:block;font-size:25px;color:var(--accent)}
section{margin-top:34px;padding-top:30px;border-top:1px solid var(--rule);scroll-margin-top:24px}h2{font-size:23px;line-height:1.5;font-weight:600;margin:0 0 20px}h2 span{font:28px Georgia,serif;color:#9eb5a5;margin-right:15px}h3{font-size:17px;margin:24px 0 10px}
p{margin:0 0 15px}figure{margin:22px 0 28px}figure img{display:block;width:100%;height:auto;border:1px solid #edf1ed}figcaption{color:var(--muted);font-size:11px;line-height:1.7;margin-top:8px}table{width:100%;border-collapse:collapse;font-size:12px;margin:20px 0}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--rule)}th{background:#f1f6f2;font-weight:600}.table-scroll{overflow-x:auto}
.node-nav{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}.node-nav a{color:var(--accent);padding:4px 12px;background:#f0f6f1;text-decoration:none}.node{margin-top:28px;border-top:1px solid var(--rule);padding-top:12px;scroll-margin-top:20px}.cause{border-left:3px solid #aec7b5;padding:10px 16px;background:#f6f9f5}.cause h4{margin:0 0 8px;color:var(--accent);font-size:13px}.conclusions li{padding:10px 0;border-bottom:1px solid var(--rule)}footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--rule);font-size:11px;color:var(--muted)}button{border:1px solid var(--rule);background:#f4f8f4;color:var(--accent);padding:8px 16px;cursor:pointer;margin:12px 0}
@media(max-width:1000px){.layout{grid-template-columns:1fr;padding:16px}aside{position:static}nav{display:flex;flex-wrap:wrap;gap:12px}nav a{padding:5px}.brand{margin-bottom:8px}main{padding:30px 24px}.metrics{grid-template-columns:repeat(2,1fr)}}
@media(max-width:480px){h1{font-size:29px}h1 small{font-size:20px}.layout{padding:8px}main{padding:25px 16px}.metric strong{font-size:21px}}
@media print{body{background:white}.layout{display:block;max-width:none;padding:0}aside,button{display:none}main{border:0;box-shadow:none;padding:0}figure,.node,.metrics{break-inside:avoid}section{break-before:auto}a{color:inherit;text-decoration:none}}
'''


def render(materials,narrative,facts,media,path,generated_at):
    esc=lambda value:html.escape(str(value),quote=True)
    def prose(p):return esc(expand(p,facts))
    def paragraphs(items):return ''.join(f'<p>{prose(p)}</p>' for p in items)
    figure_number=0
    def figure(key):
        nonlocal figure_number
        if key not in media:return ''
        figure_number+=1;m=media[key];p=Path(m['path'])
        mime={'.png':'image/png','.gif':'image/gif','.jpg':'image/jpeg','.jpeg':'image/jpeg','.svg':'image/svg+xml'}.get(p.suffix.lower(),'image/png')
        src='data:'+mime+';base64,'+base64.b64encode(p.read_bytes()).decode('ascii')
        replay='<button type="button" id="replay">重新播放</button>' if key=='animation' else ''
        ident=' id="surface-animation"' if key=='animation' else ''
        return f'<figure>{replay}<img{ident} src="{src}" alt="{esc(m["caption"])}"><figcaption>图{figure_number}　{esc(m["caption"])}</figcaption></figure>'
    def table(headers,rows):
        return '<div class="table-scroll"><table><thead><tr>'+''.join(f'<th>{esc(h)}</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{esc(c)}</td>' for c in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    scene=materials['scene'];rain=materials['rain'];surface=materials['surface'];overflow=materials['overflow']
    nav=''.join(f'<a href="#{key}">{i+1:02d}　{title}</a>' for i,(key,title) in enumerate(zip(SECTIONS,TITLES)))
    metrics=[('累计降雨量',f"{rain['total_mm']:.1f}",'毫米'),('冒溢节点',str(overflow['node_count']),'个'),
             ('最大地表水深',f"{surface['max_saved_depth_m']*100:.1f}",'厘米'),
             ('≥15厘米最大同时面积',f"{surface['threshold_metrics']['0.15']['max_area_m2']:,.0f}",'平方米')]
    header=f'<header><div class="meta">MULTI-AGENT URBAN FLOOD DIAGNOSIS</div><h1>SWMM-Agentic<small>多智能体协同城市内涝诊断报告</small></h1><div class="subtitle">{esc(scene["model_display_name"])} · {esc(scene["scenario_display_name"])} · {esc(rain["shape"])}</div><div class="meta">模拟时段 {esc(scene["simulation_start"])}—{esc(scene["simulation_end"])}<br>运行日期 {esc(scene["run_date"][:10])} · 报告生成时间 {esc(generated_at)}</div><p class="intro">{prose(narrative["intro"])}</p><div class="metrics">'+''.join(f'<div class="metric"><small>{label}</small><strong>{value}</strong><small>{unit}</small></div>' for label,value,unit in metrics)+'</div></header>'
    sections=[];by_event={e['event_id']:e for e in narrative['events']}
    for i,(key,title) in enumerate(zip(SECTIONS,TITLES)):
        body=paragraphs(narrative['sections'][key]) if key!='conclusions' else '<ol class="conclusions">'+''.join(f'<li>{prose(p)}</li>' for p in narrative['sections'][key])+'</ol>'
        if key=='rain':body+=figure('rainfall')
        elif key=='surface':
            sources='；'.join(f'<a href="{esc(row["url"])}" target="_blank" rel="noopener noreferrer">{esc(row["title"])}</a>' for row in SURFACE_BASIS['sources'])
            body=paragraphs(narrative['sections'][key][:1])+f'<p class="meta">分级依据：{sources}</p>'+paragraphs(narrative['sections'][key][1:])
            body+=figure('surface_maps')
            wet=surface['threshold_metrics']['0.01'];attention=surface['threshold_metrics']['0.15']
            labels=[('road','道路'),('green','绿地')] if surface['classification_available'] else [('green','地表')]
            body+=table(['地表类型','≥1厘米最大同时面积','出现时刻','≥15厘米最大同时面积','结束时≥1厘米面积'],
                 [[label,f"{wet['categories'][land]['peak_area_m2']:,.0f}平方米",wet['categories'][land]['peak_time'] or '—',
                   f"{attention['categories'][land]['peak_area_m2']:,.0f}平方米",f"{wet['categories'][land]['final_area_m2']:,.0f}平方米"] for land,label in labels])
            body+=figure('surface_process')+figure('surface_stages')+figure('animation')
        elif key=='overflow':
            body+=table(['节点','冒溢时段','持续时间','冒溢量','峰值流量'],[[e['node_id'],
                f"{e['event']['start']}—{e['event']['end']}",f"{e['event']['duration_minutes']:g}分钟",
                f"{e['event']['estimated_volume_m3']:.2f}立方米",f"{e['event']['peak_flooding_Ls']:.2f}升/秒"] for e in materials['events']])
            body+=figure('overflow_volumes')
        elif key=='causes':
            body+='<h3>逐节点发生过程与成因</h3><div class="node-nav">'+''.join(f'<a href="#event-{j}">{esc(e["node_id"])}'+(f' · 第{e["event"]["occurrence"]}次' if sum(x['node_id']==e['node_id'] for x in materials['events'])>1 else '')+'</a>' for j,e in enumerate(materials['events']))+'</div>' if materials['events'] else ''
            for j,event in enumerate(materials['events']):
                text=by_event[event['event_id']]
                title=text.get('title') or event['node_id']
                body+=f'<article class="node" id="event-{j}" data-node="{esc(event["node_id"])}"><h3>{prose(title)}</h3><h4>发生过程</h4>'+paragraphs(text['process'])+figure(f'event_{j}')
                if text['supported_causes'] or text['candidate_influences']:body+='<div class="cause"><h4>成因判断</h4>'+paragraphs(text['supported_causes'])+paragraphs(text['candidate_influences'])+'</div>'
                body+='</article>'
        sections.append(f'<section id="{key}"><h2><span>{i+1:02d}</span>{title}</h2>{body}</section>')
    document='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SWMM-Agentic｜城市内涝诊断报告</title><style>'+CSS+'</style></head><body><div class="layout"><aside><div class="brand">SWMM-Agentic</div><nav>'+nav+'</nav></aside><main>'+header+''.join(sections)+f'<footer>{esc(scene["model_display_name"])} · {esc(scene["scenario_display_name"])} · 报告生成时间 {esc(generated_at)}</footer>' + '<button onclick="window.print()">打印 / 保存静态版</button></main></div><script>const replay=document.getElementById("replay");if(replay)replay.addEventListener("click",()=>{const img=document.getElementById("surface-animation");const src=img.src;img.src="";requestAnimationFrame(()=>{img.src=src})});</script></body></html>'
    path.write_text(document,encoding='utf-8')
    return {'section_count':len(sections),'event_count':len(materials['events']),'embedded_images':figure_number}
