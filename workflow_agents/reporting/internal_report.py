"""Render the internal task report from an existing diagnosis."""
from workflow_agents.diagnosis.contract import MECHANISMS, STATUS_LABELS

def render_report(state, diagnosis, visible_count, snapshot_count):
    lines = ['# 冒溢初步诊断' if state.get('task_kind') == 'initial_overflow' else '# 诊断调查结果', '',
             state['task_context'].get('objective') or state['task_context']['original_question'], '',
             '本报告为初步诊断。引用核查只确认可追溯性，不代表数值、工程解释或因果已独立验证。', '',
             f'证据阅读范围：已提供 {visible_count} / {snapshot_count} 条快照证据；未提供部分不视为已分析。', '']
    if state.get('task_kind') == 'initial_overflow':
        overview = state['overflow_overview']['value']
        lines += ['## 全场概览', '',
                  f"识别 {overview['overflow_node_count']} 个冒溢节点、{overview['event_count']} 次事件；所识别事件积分合计 {overview['identified_event_volume_m3']:.6f} m³。",
                  '节点分布：' + ('、'.join(overview['overflow_nodes']) or '未识别冒溢节点') + '。',
                  f"事件时间范围：{overview['first_start'] or '无'} 至 {overview['last_end'] or '无'}。",
                  f"依据：{state['overflow_overview']['evidence_id']}。", '',
                  diagnosis['run_overview']['text'], '引用：' + ', '.join(diagnosis['run_overview']['evidence_ids']), '',
                  '## 逐节点／事件诊断', '']
        manifest = {e['event_id']: e for e in state['event_manifest']}
        assessments = {e['event_id']: e for e in diagnosis['event_assessments']}
        for event_id, event in manifest.items():
            facts, item = event['facts'], assessments[event_id]
            lines += [f"### {event['node_id']} · {event_id}", '',
                      f"发生事实：起溢 {facts['start']}；{'末次保存时刻（结束未观测）' if facts.get('right_censored') else '结束'} {facts['end']}；最后正冒溢样本 {facts['last_positive_time']}；{'已观测持续' if facts.get('left_censored') or facts.get('right_censored') else '持续'} {facts['duration_minutes']:g} min；积分冒溢量 {facts['estimated_volume_m3']:.6f} m³。",
                      f"峰值：{facts['peak_time']}，{facts['peak_flooding_Ls']:.6f} L/s；保存步长 {facts.get('saved_step_seconds')} s。",
                      f"采样边界：左截断={facts.get('left_censored')}，右截断={facts.get('right_censored')}；积分方法={facts.get('integration_method')}。",
                      f"事实来源：{event['fact_evidence_id']}。", '',
                      '发生过程：' + item['process_explanation'], '引用：' + ', '.join(item['evidence_ids']), '', '可能机制：', '']
            for m in item['mechanism_assessments']:
                lines.append(f"- {MECHANISMS[m['mechanism_id']]}（{STATUS_LABELS[m['status']]}）：{m['reason']}" +
                             (f"（{', '.join(m['evidence_ids'])}）" if m.get('evidence_ids') else ''))
            joint = item.get('joint_assessment')
            if joint:
                names = '、'.join(MECHANISMS[k] for k in joint['mechanism_ids']) or '尚不能确定'
                lines += ['', f"综合机制判断（{STATUS_LABELS[joint['status']]}）：{joint['conclusion']}",
                          '共同作用机制：' + names + '。', '作用关系：' + joint['interaction_explanation'],
                          '依据：' + ', '.join(joint['evidence_ids']), '判断边界：' + joint['limitations']]
            lines += ['', '竞争解释／共同作用：' + item['alternatives'], '', '证据缺口：', '']
            lines.extend('- ' + gap for gap in item['evidence_gaps'])
            if not item['evidence_gaps']:
                lines.append('本轮未提出具体缺口；不代表所有机制已核实。')
            lines.append('')
        if not manifest:
            lines += ['本场保存结果中未识别冒溢；这不代表所有时段、模型边界或地表风险已被排除。', '']
    if diagnosis['claims']:
        lines += ['## 可追溯结论', '']
    for claim in diagnosis['claims']:
        lines += [f"- [{claim['claim_id']}] [{claim['claim_kind']}] {claim['claim_text']}（{', '.join(claim['evidence_ids'])}）",
                  f"  工程依据：{claim['engineering_reason']}；相关解释：{claim['alternatives']}；适用范围：{claim['scope']}"]
    lines += ['', '## 待补证事项', '']
    for req in diagnosis['evidence_requests']:
        target = ' / '.join(str(req[k]) for k in ('event_ids', 'event_id', 'object_ids', 'object_id', 'metric_name') if req.get(k))
        plan = next((p for p in state.get('evidence_plan', []) if p['request'] == req), {})
        detail = ('（从本次保存结果提取／计算，返回可追溯证据）' if plan.get('execution') == 'saved_result_extract'
                  or req['tool'] == 'saved_result_extract' else
                  '（需明确保存数据能力以外的调查方法）' if req['tool'] == 'unavailable' else '（查询冻结快照中的既有记录）')
        if plan.get('reason'):
            detail += ' ' + plan['reason']
        lines.append(f"- {target or req['tool']}：{req['reason']}" + detail)
    if not diagnosis['evidence_requests']:
        lines.append('本轮没有待执行的补证查询；逐事件缺口仍以正文为准。')
    lines += ['', '本轮可回答范围与限制：' + diagnosis['stop_reason']]
    return '\n'.join(lines)
