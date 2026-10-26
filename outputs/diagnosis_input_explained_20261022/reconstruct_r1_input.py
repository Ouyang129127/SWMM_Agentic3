"""Reconstruct R1 inputs for inspection, without importing agents or calling models.

Original run artifacts are read only. This export is a reconstruction using
current source and recorded R1 visibility, not a historical request capture.
"""
import ast
import copy
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
RUN = BASE / 'models/Example1/runs/diagnostic_single_4h_20mm_5min__baseline__20261021_230830_fcffdca6'
TASK = RUN / 'diagnosis_tasks/6c808c679a2b4930b769cc22a6f80e54'
ATTEMPT = TASK / 'diagnosis_attempt_67979c9703234a8fb21c14d50a5db68d.json'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def main():
    package_path = RUN / 'evidence/evidence_package.json'
    snapshot_path = TASK / 'evidence_snapshot.json'
    state_path = TASK / 'task.json'
    prompt_path = BASE / 'workflow_agents/diagnosis/prompts.py'
    service_path = BASE / 'workflow_agents/diagnosis/service.py'
    tracked = [package_path, snapshot_path, state_path, ATTEMPT, prompt_path, service_path]
    before = {str(p): sha(p) for p in tracked}
    package, rows, state, audit = map(read, [package_path, snapshot_path, state_path, ATTEMPT])
    assert sha(snapshot_path) == audit['snapshot_hash']
    assert sha(package_path) == state['source_hash']
    assert state['observations'][0]['selected_count'] == len(audit['visible_ids']) == 22

    # Load only a literal and two pure functions. No module imports or setup code.
    tree = ast.parse(prompt_path.read_text(encoding='utf-8'))
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'SYSTEM_PROMPT' for t in n.targets))
    system = ast.literal_eval(assignment.value)
    service_tree = ast.parse(service_path.read_text(encoding='utf-8'))
    functions = [n for n in service_tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'serialize_prompt', 'build_diagnosis_prompt'}]
    env = {'json': json, 'copy': copy, 'Counter': Counter,
           'evidence_binding': lambda _: copy.deepcopy(audit['evidence_binding'])}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(service_path), 'exec'), env)
    initial_state = copy.deepcopy(state)
    initial_state.update(visible_ids=audit['visible_ids'], observations=state['observations'][:1],
                         supplements=[], revision=0, state='ready_for_diagnosis')
    payload = env['build_diagnosis_prompt'](initial_state, rows, previous=None, feedback=None)
    content = env['serialize_prompt'](payload)
    char_count = len(system) + len(content)
    # Do not present mismatching current-code reconstruction as the R1 input.
    assert char_count == audit['request_characters'], (char_count, audit['request_characters'])
    visible = payload['visible_evidence']
    assert len(visible) == 22
    write('01_user_payload_reconstructed.json', payload)
    (OUT / '02_system_prompt_current_source.txt').write_text(system, encoding='utf-8')
    (OUT / '03_user_message_compact_reconstructed.txt').write_text(content, encoding='utf-8')
    write('04_api_messages_reconstructed.json', {'messages': [
        {'role': 'system', 'content': system}, {'role': 'user', 'content': content}],
        'json_output': True})
    with (OUT / '05_visible_evidence_index.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['index', 'evidence_id', 'metric_name', 'object_id',
                                             'event_id', 'compact_characters', 'series_samples'])
        writer.writeheader()
        for i, r in enumerate(visible, 1):
            writer.writerow(dict(index=i, evidence_id=r['evidence_id'], metric_name=r['metric_name'],
                                 object_id=r['object_id'], event_id=r.get('event_id', ''),
                                 compact_characters=len(compact(r)),
                                 series_samples=len(r.get('value', {}).get('series', []))))
    by_metric = {r['metric_name']: r for r in visible}
    process = by_metric['local_process_series']
    peak = next(r for r in process['value']['series'] if r['time'] == '2024-01-01 01:40:10')
    write('06_node10_input_excerpt.json', {
        'notice': 'Reading excerpt only: R1 received all 207 local-process samples, not this one sample.',
        'event_context': by_metric['event_context'],
        'local_structure': by_metric['local_structure'],
        'process_evidence_id': process['evidence_id'], 'one_peak_sample': peak,
        'direct_source_composition': by_metric['direct_source_composition'],
        'facilities_and_storage': by_metric['facilities_and_storage']})
    write('07_recorded_model_response.json', audit['parsed_response'])
    (OUT / '08_recorded_raw_response.txt').write_text(audit['raw_response'], encoding='utf-8')
    assert before == {str(p): sha(p) for p in tracked}
    stats = {
        'kind': 'current_source_reconstruction_not_historical_request_capture',
        'run_root': str(RUN), 'task_root': str(TASK), 'audit_file': str(ATTEMPT),
        'model': audit['model'], 'started_at': audit['started_at'],
        'package_evidence_rows': len(package['evidence_rows']), 'snapshot_evidence_rows': len(rows),
        'visible_evidence_rows': len(visible), 'derived_global_scalar_summaries': 14,
        'local_process_samples': len(process['value']['series']),
        'system_characters': len(system), 'user_message_characters': len(content),
        'combined_characters': char_count, 'recorded_request_characters': audit['request_characters'],
        'character_count_matches_audit': True, 'recorded_usage': audit['usage'],
        'previous_diagnosis': payload['previous_diagnosis'],
        'verification_feedback': payload['verification_feedback'],
        'source_hashes_before_and_after_match': True, 'source_hashes': before,
        'reconstruction_note': 'Historical audit stores visible IDs, binding, character count and response, but not complete input messages or a system-prompt hash. Equal length supports consistency; it is not proof of byte-identical historical input.',
    }
    write('09_reconstruction_manifest.json', stats)
    lines = [
        '# Example1 首轮诊断输入与输出：文件导读', '',
        '本目录于 2026-10-22 从用户指定运行的历史文件离线整理。没有运行模拟，没有调用 LLM，没有改动原运行文件。', '',
        '范围只包括首次诊断 R1；补证后 R2 和展示报告 Report Agent 的调用另计。', '',
        '## 先回答三个问题', '',
        '1. DiagnosisAgent 不直接打开这些本地文件。Python 程序读取文件、构建证据并选取记录。',
        '2. 选取单位是有对象、事件、指标和来源的结构化 JSON 证据记录；本链路没有语句切块、向量化或向量库检索。evidence_snapshot.json 是本地冻结快照，内存中的 rows 是列表。',
        '3. 首次诊断将系统提示词与一份包含 22 条可见证据及任务上下文的 JSON 文本一起交给一次诊断模型调用。没有把全部原始文件或整个证据包原样上传。', '',
        '## 真实材料链条', '',
        f'- 原始结果在 `{RUN}` 中：summary.json、swmm/model_with_event.inp、swmm/model.out、swmm/nodes.tsv、swmm/links.tsv、ca2d/surface_depth.tsv、swmm/rainfall_for_simulation.txt。',
        '- EvidenceBuilder 程序生成 evidence/evidence_package.json，包含 28,552 条证据。',
        '- 任务准备程序生成 evidence_snapshot.json：保留原证据并添加 14 条全场标量汇总、1 条冒溢事件概览，共 28,567 条。',
        '- task.json 记录任务与选取状态。首轮 visible_ids 应看首轮 diagnosis_attempt 的记录；现在的 task.json 已推进到 R2。',
        '- R1 可见 22 条：6 条事件证据、1 条降雨背景、14 条全场标量汇总、1 条事件概览。',
        '- local_process_series 是一条证据，但内部包含 207 个时间样本，覆盖 01:36:35—01:53:45、节点 9/10/21 和管段 1/6。22 条记录不等于 22 句话。', '',
        '## 建议按这个顺序打开', '',
        '- [节点10输入节选](06_node10_input_excerpt.json)：事件、结构、峰值样本、来源组成和储量可用情况。节选只为阅读，不是模型的全部输入。',
        '- [22条可见证据目录](05_visible_evidence_index.csv)：每条记录的ID、指标、长度和过程样本数。',
        '- [完整用户载荷重建版](01_user_payload_reconstructed.json)：展开 visible_evidence 即可看到22条完整记录。',
        '- [系统提示词](02_system_prompt_current_source.txt)：从当前 prompts.py 原样提取的字符串。',
        '- [两条API消息重建版](04_api_messages_reconstructed.json)：system和user一起构成一次诊断请求。',
        '- [历史模型响应](07_recorded_model_response.json)：从R1调用日志直接复制的parsed_response，尚未加程序的D1:C编号。',
        '- [重建核对记录](09_reconstruction_manifest.json)：输入规模、token用量和来源哈希。', '',
        '## 首轮调用规模', '',
        f'- 模型：{audit["model"]}；日志时间：{audit["started_at"]}。',
        f'- 系统提示词 {len(system):,} 字符；用户消息 {len(content):,} 字符；合计 {char_count:,} 字符，与日志一致。',
        f'- 接口记载 prompt_tokens={audit["usage"]["prompt_tokens"]:,}，completion_tokens={audit["usage"]["completion_tokens"]:,}。token与字符不是同一计量。',
        '- 首轮 previous_diagnosis=null、verification_feedback=null。', '',
        '## 输入重建的准确性边界', '',
        '历史诊断日志保存了可见ID、证据绑定、输入字符数和模型原始响应，没有保存完整请求正文及当时系统提示词哈希。因此01—04是依据当前源码和历史首轮状态重建；合计字符数与历史日志相符，不称为当时API请求的原始抓包。07—08直接来自历史响应。', '',
        '## 对照源代码', '',
        f'- `{BASE / "workflow_agents/evidence/evidence_builder.py"}`：原始结果到证据包。',
        f'- `{BASE / "workflow_agents/evidence/event_packages.py"}`：按事件、拓扑和时窗抽取完整局部过程。',
        f'- `{BASE / "workflow_agents/evidence/evidence_selection.py"}`：按任务选记录、生成全场汇总。',
        f'- `{BASE / "workflow_agents/orchestration/tasks.py"}`：创建快照及初始状态。',
        f'- `{BASE / "workflow_agents/diagnosis/service.py"}`：构建载荷、序列化、调用模型、解析返回。',
        f'- `{BASE / "workflow_agents/diagnosis/prompts.py"}`：DiagnosisAgent系统提示词。', '',
        '展示用HTML报告另有Report Agent调用。其report_prompt.json是报告写作输入，不能当作本次DiagnosisAgent的输入。', '',
    ]
    (OUT / 'README_中文导读.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({k: v for k, v in stats.items() if k != 'source_hashes'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
