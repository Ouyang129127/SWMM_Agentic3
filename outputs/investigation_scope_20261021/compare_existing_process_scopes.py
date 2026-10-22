"""Offline scope comparisons; no model calls and no original files modified."""
import copy
import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
RUN = OUT.parents[1] / 'models/Example1/runs/diagnostic_single_4h_20mm_5min__baseline__20261020_223309_3641fc9e'
path = RUN / 'evidence/evidence_package.json'
package = json.loads(path.read_text(encoding='utf-8'))
source = next(r for r in package['evidence_rows'] if r['metric_name'] == 'local_process_series')
compact = lambda x: json.dumps(x, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
base_characters = len(compact(source))
variants = [
    ('current', '当前全范围', ['9','10','13','14','15','16','17','18','19','20','21','22','23','24'],
     ['1','4','5','6','7','8','10','11','12','13','14','15','16']),
    ('one_segment_with_endpoint_series', '上下游各一段，含端点时序', ['9','10','21'], ['1','6']),
    ('main_branch_with_endpoint_series', '上下游主支，含端点时序', ['9','10','21','22','16','24','17','18'],
     ['1','6','7','8','15','16','10']),
    ('main_branch_without_endpoint_series', '上下游主支，不取最外端N9/N18的节点时序、保留末段管道',
     ['10','21','22','16','24','17'], ['1','6','7','8','15','16','10']),
    ('main_branch_without_endpoint_and_terminal_links', '上下游主支，去掉最外端N9/N18及其末段管道',
     ['10','21','22','16','24','17'], ['6','7','8','15','16']),
    ('main_branch_with_side_interfaces', '主支加沿程侧向接口及其端点、不追溯完整侧支',
     ['9','10','21','22','16','24','17','18','20','15','23'], ['1','6','7','8','15','16','10','5','13','14']),
]
results=[]
for key,label,nodes,links in variants:
    node_set,link_set=set(nodes),set(links)
    value=copy.deepcopy(source['value'])
    for sample in value['series']:
        sample['nodes']={k:v for k,v in sample['nodes'].items() if k in node_set}
        sample['links']={k:v for k,v in sample['links'].items() if k in link_set}
        # Target direct-source values are kept only when that pipe remains.
        sample['signed_towards_node_Ls']={k:v for k,v in sample['signed_towards_node_Ls'].items()
                                        if k=='aggregate_lateral' or k in link_set}
        if not {'1','6'} <= link_set:
            sample.pop('positive_source_sum_Ls',None)
            sample.pop('source_sum_minus_total_Ls',None)
    candidate=copy.deepcopy(source)
    candidate['value']=value
    size=len(compact(candidate))
    entry={'id':key,'label':label,'node_series':sorted(node_set,key=int),'link_series':sorted(link_set,key=int),
           'samples':len(value['series']),'comparable_process_row_characters':size,
           'character_reduction_percent':round(100*(1-size/base_characters),2),
           'directly_retains_all_explicit_r1_process_objects':{'9','10','21'}<=node_set and {'1','6'}<=link_set,
           'missing_explicit_node_series':sorted({'9','10','21'}-node_set,key=int),
           'missing_explicit_link_series':sorted({'1','6'}-link_set,key=int)}
    results.append(entry)
    if key=='one_segment_with_endpoint_series':
        extract={'artifact_type':'offline_process_subset_not_a_native_evidence_package',
                 'source_evidence_id':source['evidence_id'],
                 'source_package_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                 'run_id':RUN.name,'nodes':entry['node_series'],'links':entry['link_series'],
                 'note':'Exact selected source values, all 207 times retained. Original IDs not reissued as modified native evidence. No model call performed.',
                 'value':value}
        (OUT/'n10-direct-process-extract.json').write_text(json.dumps(extract,ensure_ascii=False,indent=2),encoding='utf-8')

series=source['value']['series']
checks=[]
for subject,values in [('L1',[ (s['time'],s['links']['1']['signed_model_flow_Ls']) for s in series]),
                       ('N10_lateral',[ (s['time'],s['nodes']['10']['lateral_inflow_Ls']) for s in series]),
                       ('N10_overflow',[ (s['time'],s['nodes']['10']['flooding_Ls']) for s in series])]:
    maximum=max(v for _,v in values)
    checks.append({'subject':subject,'maximum':maximum,'maximum_times':[t for t,v in values if v==maximum],
                   'value_at_overflow_peak':dict(values)['2024-01-01 01:40:10']})
report={'run_id':RUN.name,'original_process_row_characters':base_characters,
        'comparison_basis':'Only detailed process samples are compared. Run/event facts, rainfall, global scalar summaries, static structure/facility inventory must be separately retained for the corresponding broad claims.',
        'limitation':'Retaining explicitly cited objects is not a model rerun, a proof of identical outputs, or certification of the original mechanisms. Removing standalone endpoint series may retain endpoint-derived head differences in links.',
        'variants':results,'numerical_checks':checks,
        'finding':'C5 and M1 confuse source values at the overflow-peak time with the sources own maxima: the two sources do not reach their own saved maxima at the same time.'}
(OUT/'process-scope-comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# N10已有首轮结论的过程证据删减检查','',
       '未重新调用模型，未修改原始证据。比较详细时序范围；全场事件目录、降雨、全场标量摘要和静态设施清单另行保留。','',
       '| 方案 | 节点时序数 | 管段时序数 | 过程记录字符数 | 相对当前减少 | 原结论明确写出的过程对象有无缺项 |',
       '|---|---|---|---|---|---|']
for r in results:
    missing=[]
    if r['missing_explicit_node_series']:missing.append('节点'+','.join(r['missing_explicit_node_series']))
    if r['missing_explicit_link_series']:missing.append('管段'+','.join(r['missing_explicit_link_series']))
    lines.append(f"| {r['label']} | {len(r['node_series'])} | {len(r['link_series'])} | {r['comparable_process_row_characters']:,} | {r['character_reduction_percent']}% | {'；'.join(missing) if missing else '无'} |")
lines += ['','节点9缺失会失去C7/M4中相邻上游节点水深的直接样本；管段1缺失还会失去C5/M1中的上游直接来水时序。若保留由端点计算的水头差，可能以额外计算恢复部分端点状态，不能声称删除了所有端点信息。','',
          '## 实际数值核对','', '| 对象 | 自身保存样本最大值 | 自身峰时 | 在冒溢峰时01:40:10的值 |','|---|---|---|---|']
for c in checks:
    lines.append(f"| {c['subject']} | {c['maximum']} L/s | {', '.join(c['maximum_times'])} | {c['value_at_overflow_peak']} L/s |")
lines+=['','已有C5/M1声称两路来水在01:40:10达到各自峰值。原时序显示两路自身峰时分别为01:40:20和01:40:05。direct_source_composition的peak_entering_Ls是冒溢峰时的来源取值，并非每一路自身最大值。','',
        '这项检查说明：小范围过程仍可核查当前主要论据；原全范围输入并未保证诊断表述准确。不能将“保留原结论所用数据”直接等同于“原结论已获证明”。','']
(OUT/'process-scope-comparison.md').write_text('\n'.join(lines),encoding='utf-8')
print(compact({'variants':results,'numerical_checks':checks}))
