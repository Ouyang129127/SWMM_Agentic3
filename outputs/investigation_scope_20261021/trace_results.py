"""Retrospective text/reference trace, without rerunning or changing diagnosis."""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
PROJECT = OUT.parents[1]
RUN = PROJECT / 'models/Example1/runs/diagnostic_single_4h_20mm_5min__baseline__20261020_223309_3641fc9e'
TASK = RUN / 'diagnosis_tasks/4eab2228f2a54ef180355bcb8f31bbaf'
load = lambda p: json.loads(p.read_text(encoding='utf-8'))
diagnosis = load(TASK / 'diagnosis_r1.json')
snapshot = load(TASK / 'evidence_snapshot.json')
rows = {r['evidence_id']: r for r in snapshot}
claims = {c['claim_id']: c for c in diagnosis['claims']}

# Manually reviewed, literal object dependencies in the delivered reasoning.
# These are NOT inferred from the containing row's coverage or private model computation.
definitions = [
 ('D1:C1', '起溢时刻', ['10'], [], '节点10冒溢流量；事件开始时间、保存步长与截断状态',
  '01:36:35、01:36:40', '无额外全网过程依赖'),
 ('D1:C2', '冒溢位置及地表关联缺失', ['10'], [], '事件目录、全场冒溢节点列表、surface_association状态',
  '本次全场', '全网14节点的冒溢存在性核对；不需要14节点全部水力时序'),
 ('D1:C3', '冒溢体积与峰值', ['10'], [], '事件积分体积、峰值流量与峰值时刻',
  '01:36:40—01:53:40；峰值01:40:10', '事件事实由程序计算；该结论未引用过程时序包'),
 ('D1:C4', '持续时间及单次连续冒溢', ['10'], [], '事件起止、持续时间、截断标志；全场事件目录',
  '01:36:40—01:53:40', '引用全场事件目录，不等于逐节点过程分析'),
 ('D1:C5', '两路来水集中', ['10'], ['1'], '节点10侧向入流、总入流与冒溢；管1进入流量与体积；降雨峰时',
  '01:35降雨峰；01:36:35—01:40:10增长；事件积分时窗', '管1连接节点9，但本条未明确使用节点9自身时序'),
 ('D1:C6', '直接出口管6的排出约束', ['10'], ['6'], '节点10总入流、冒溢和水头；管6流量、水深、断面及连接',
  '事件时窗，尤其01:40:10', '节点21作为管6的结构端点出现；本条未单独比较节点21时序'),
 ('D1:C7', '节点10达到冒溢阈值及位置解释', ['9','10','21'], ['6'],
  '节点10井底、最大深度、水头、深度；节点9和21深度/最大深度；管6连接与断面',
  '事件时窗', '另引用全网冒溢次数统计；该统计本身不提供相邻节点的水深'),
 ('D1:C8', '设施、初始状态及储量缺证', [], [], '节点类型、initial_depth、ALLOW_PONDING、设施清单、储量提取状态',
  '本次运行输入及首轮证据状态', '使用已核实范围14节点/13管段的静态清单；无逐对象完整时序需求'),
]
mechanism_defs = [
 ('M1','来水集中',['10'],['1'],'直接来水组成、节点10过程、降雨及事件事实','同C5'),
 ('M2','局部输水约束',['10'],['6'],'节点10总入流/冒溢；管6流量、水深、断面及连接','同C6；21为结构端点'),
 ('M3','下游顶托待补证',['10','21'],['6'],'节点10与21水头；管6有符号流量；FLOW_ROUTING及模型上下文',
  '另引用全网13管段流向变化计数汇总；没有逐一分析这些管段的过程'),
 ('M4','冒溢阈值及位置',['9','10','21'],['6'],'同C7','另引用全网冒溢次数统计'),
 ('M5','设施运行不适用',[],[],'设施清单、控制规则与结构','全范围静态清单；无具体远端对象时序分析'),
 ('M6','动态储水待补证',['10'],['6'],'节点10深度变化、管6状态、初始条件与储量缺证状态',
  '结合全范围静态类型清单；未获得储量过程即保留未知'),
]

def coverage(row):
    v = row.get('value', {})
    nodes, links = set(), set()
    if isinstance(v, dict):
        if row['metric_name'] == 'local_structure':
            nodes.update(v.get('nodes', {})); links.update(v.get('links', {}))
        for sample in v.get('series', []):
            nodes.update(sample.get('nodes', {})); links.update(sample.get('links', {}))
    return {'nodes': sorted(nodes,key=int), 'links': sorted(links,key=int)}

records = []
for cid,title,ns,ls,fields,time,note in definitions:
    c = claims[cid]
    records.append({'id':cid,'title':title,'text':c['claim_text'],
                    'explicit_process_nodes':ns,'explicit_process_links':ls,'data_fields':fields,
                    'time':time,'note':note,'evidence_ids':c['evidence_ids']})
for mid,title,ns,ls,fields,note in mechanism_defs:
    m = next(m for m in diagnosis['mechanism_assessments'] if m['mechanism_id']==mid)
    records.append({'id':mid,'title':title,'text':m['reason'],'status':m['status'],
                    'explicit_process_nodes':ns,'explicit_process_links':ls,'data_fields':fields,
                    'time':'首轮事件窗口；所引全网统计为其独立源时窗','note':note,'evidence_ids':m['evidence_ids']})
event = diagnosis['event_assessments'][0]
for rid,title,text,ids,ns,ls,note in [
 ('EVENT_PROCESS','事件过程解释',event['process_explanation'],event['evidence_ids'],['10'],['1','6'],
  '主要复述C1/C3/C5/C6及静态初始条件；未逐一解释远端节点管段'),
 ('JOINT','综合机制结论',event['joint_assessment']['conclusion']+'\n'+event['joint_assessment']['interaction_explanation'],
  event['joint_assessment']['evidence_ids'],['10'],['1','6'],'综合M1/M2/M4；未出现独立的远端支路过程分析'),
 ('OVERVIEW','全场概览',diagnosis['run_overview']['text'],diagnosis['run_overview']['evidence_ids'],['10'],['1','6'],
  '还使用全场事件、降雨及地表最大水深/积水时长汇总；地表统计不是节点来源归因'),
]:
    records.append({'id':rid,'title':title,'text':text,'explicit_process_nodes':ns,'explicit_process_links':ls,
                    'data_fields':'见原文及引用证据列表','time':'见原文；各统计保留自身源时窗','note':note,'evidence_ids':ids})
for q in diagnosis['question_assessments']:
    cid={'Q1':'D1:C1','Q2':'D1:C2','Q3':'D1:C3','Q4':'D1:C4'}[q['question_id']]
    records.append({'id':q['question_id'],'title':'四问回答状态','text':q['reason'],
                    'status':q['status'],'explicit_process_nodes':['10'],'explicit_process_links':[],
                    'data_fields':'对应'+cid,'time':'同对应claim','note':'该字段无独立evidence_ids；这里按内容映射，不伪装为其显式引用',
                    'evidence_ids':[]})
for item in records:
    item['cited_rows']=[{'evidence_id':eid,'metric':rows[eid]['metric_name'],
                         'containing_row_object_coverage':coverage(rows[eid])} for eid in item['evidence_ids']]

attempts=[]
for p in TASK.glob('diagnosis_attempt_*.json'):
    a=load(p)
    attempts.append({k:a.get(k) for k in ['attempt_id','status','phase','started_at','finish_reason','usage','request_characters']})
attempts.sort(key=lambda x:x['started_at'])
audit={'run_id':RUN.name,'revision':1,'diagnosis_source':str(TASK/'diagnosis_r1.json'),
       'trace_basis':'人工逐条核对已交付文本与引用ID；不是LLM内部访问日志，不能证明未提及数据从未影响模型。',
       'validation_limit':'本表追踪声明的依赖，不认证原结论的数值、推理或因果正确性。',
       'key_limitation':'EvidenceID覆盖整包local_process_series/structure，尚无模型声明的对象-字段-样本级引用。',
       'explicit_process_object_union':{'nodes':['9','10','21'],'links':['1','6']},
       'records':records,'attempts':attempts}
(OUT/'claim-evidence-trace.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')

lines=['# 本次诊断逐条证据使用对照','',f'运行：`{RUN.name}`；有效诊断版本：r1。','',audit['trace_basis'],'',audit['validation_limit'],'',
       '“明确使用”指正文明确陈述的数据关系；对象出现在同一证据包中，不等于该对象被明确用于结论。','',
       '## 八条可追溯结论','', '| 结论 | 内容 | 明确涉及的节点过程 | 明确涉及的管段过程 | 数据及范围说明 |',
       '|---|---|---|---|---|']
for r in records[:8]:
    lines.append('| '+ ' | '.join([r['id'],r['title'],'、'.join(r['explicit_process_nodes']) or '无单独时序需求',
                               '、'.join(r['explicit_process_links']) or '无单独时序需求',r['data_fields']+'；'+r['note']])+' |')
lines+=['','## 六机制与综合结论','', '| 项目 | 明确节点过程 | 明确管段过程 | 说明 |','|---|---|---|---|']
for r in records[8:17]:
    lines.append('| '+' | '.join([r['id']+' '+r['title'],'、'.join(r['explicit_process_nodes']) or '静态清单',
                                '、'.join(r['explicit_process_links']) or '静态清单',r['note']])+' |')
lines+=['','## 每项原文与精确引用','']
for r in records:
    lines += ['### '+r['id']+' '+r['title'],'',r['text'],'','- 数据：'+r['data_fields'],
              '- 时窗：'+r['time'],'- 范围说明：'+r['note'],'- 原文引用ID：'+(', '.join(r['evidence_ids']) or '该字段没有独立ID列表'),'']
    for cited in r['cited_rows']:
        cov=cited['containing_row_object_coverage']
        lines.append('  - `'+cited['evidence_id']+'`：'+cited['metric']+
                     ('；整条记录覆盖节点'+','.join(cov['nodes'])+'，管段'+','.join(cov['links']) if cov['nodes'] or cov['links'] else ''))
    lines.append('')
lines+=['## 补证后调用状态','', '| 开始时间（UTC） | 状态 | finish_reason | 输入token | 输出token |','|---|---|---|---|---|']
for a in attempts:
    u=a.get('usage') or {}
    lines.append(f"| {a['started_at']} | {a['status']} / {a['phase']} | {a['finish_reason']} | {u.get('prompt_tokens','')} | {u.get('completion_tokens','')} |")
lines+=['','首轮调用成功；随后四次带补证的调用均因length未形成可解析诊断。不能把补证提取成功计为新的机制结论。','',
        '## 对基础诊断单元的影响','',
        '当前topology_scope.py第66—72行会追溯下游沿程侧支；第76—78行还会纳入所有沿程节点的直接交换管段和端点。若目标是只保留目标节点所在主通道，需要同时界定这两条规则。',
        '即使取消这两类侧向纳入，下游主通道仍按“到分流/出流口停止”延伸；在本模型中仍到出流口18。','']
(OUT/'claim-evidence-trace.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps({'records':len(records),'successful_diagnoses':sum(a['status']=='completed' for a in attempts),
                  'failed_attempts':sum(a['status']=='failed' for a in attempts),'output':str(OUT/'claim-evidence-trace.md')}))
