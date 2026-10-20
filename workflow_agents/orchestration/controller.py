from workflow_agents.simulation.service import run_simulation_from_scenario_request
import asyncio
import json
import re
import uuid
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Any
from workflow_agents.orchestration.assistant import explain_validation_evidence, run_web_orchestrator_agent_turn
from workflow_agents.scenario.validation import check_all_swmm_2d_projects, check_ca2d_model, check_swmm_2d_project, list_rainfall_events, list_swmm_2d_models
from workflow_agents.orchestration.run_preparation import run_workflow_stage
from workflow_agents import load_scenario_request, prepare_scenario_request
from workflow_agents.common.schemas import EVIDENCE_READY, NO_SURFACE_INFLOW, RUN_READY, SCENARIO_READY
from workflow_agents.common.state import load_or_initialize_state
from workflow_agents.orchestration.investigation import advance_investigation
from workflow_agents.orchestration.investigation_flow import load_active_task
from workflow_agents.orchestration.session_state import WebWorkflow, ACTION_LABELS, TASK_ACTIONS
from workflow_agents.interfaces.presentation import details, error_summary, is_context_limit_error, chat_history_summary
from workflow_agents.common.paths import MODELS_DIR, RUNTIME_DIR
WEB_LOG_DIR = RUNTIME_DIR / 'web_sessions'
run_locks: dict[tuple[str, str], asyncio.Lock] = {}

async def run_orchestrator_turn(transcript: list[dict[str, str]], flow: WebWorkflow | None = None,
                                checkpoint=None) -> str:
    """Only the controller can execute a stage, and only on a fresh user approval."""
    flow = flow if flow is not None else WebWorkflow()
    latest = transcript[-1]['content']
    compact = latest.strip(' ，。!?！？').lower()
    if compact in {'暂停', '停止', '取消', 'pause', 'stop', 'cancel'}:
        flow.status = 'paused'
        flow.version += 1
        return '流程已暂停，本轮未执行。回复“继续”时会先展示待执行步骤，重新等待确认。'
    if compact in {'状态', '当前状态', '进度', '下一步我该做什么', '好的，下一步我该做什么',
                   '现在下一步是什么', '下一步是什么', 'status'}:
        if not flow.run_id and flow.status == 'idle':
            adopt_legacy_binding(flow, transcript)
        detail = ''
        if flow.task_id:
            state = await advance_investigation(flow.model_name, flow.run_id, 'status', flow.task_id)
            detail = format_investigation_result(state, include_next=False) + '\n\n'
        return detail + workflow_status_text(flow)
    # A quoted run/model inventory is not a new execution request.
    new_plan = contains_any(latest, ['设计', '流程', '一步一步', '逐步', '重新开始', '新任务',
                                    '执行模拟', '运行模拟', '模拟计算', '开始模拟', '耦合模拟',
                                    'run simulation', 'run model', 'simulate', 'workflow'])
    if new_plan or (extract_rainfall_file(latest) and flow.status in {'idle', 'completed', 'paused'}):
        flow.reset()
        # Only an explicit user model choice supplies the model, never assistant inventories.
        flow.model_name = latest_user_model(transcript)
        flow.rainfall_file = extract_rainfall_file(latest)
        flow.scenario_name = extract_scenario_name(latest) or 'baseline'
        explicit_run = extract_workflow_run_id(latest)
        if explicit_run:
            bind_web_run(flow, flow.model_name, explicit_run)
            refresh_web_workflow(flow)
            flow.wait_for(flow.next_action)
            return workflow_status_text(flow)
        set_scenario_plan(flow)
        return workflow_plan_text(flow)
    selected_model = extract_model_name(latest)
    if flow.run_id and selected_model and selected_model != flow.model_name:
        flow.reset()
        flow.model_name = selected_model
        flow.rainfall_file = extract_rainfall_file(latest)
        set_scenario_plan(flow)
        return '已切换模型，旧运行与诊断任务不再用于本流程。\n\n' + workflow_plan_text(flow)
    if flow.status in {'awaiting_inputs', 'awaiting_confirmation'} and flow.next_action == 'scenario':
        model = extract_model_name(latest)
        rainfall = extract_rainfall_file(latest)
        scenario = extract_scenario_name(latest)
        if model or rainfall or scenario:
            if model and model != flow.model_name:
                flow.rainfall_file = ''
            flow.model_name = model or flow.model_name
            flow.rainfall_file = rainfall or flow.rainfall_file
            flow.scenario_name = scenario or flow.scenario_name
            set_scenario_plan(flow)
            return workflow_plan_text(flow)
    if is_continue_confirmation(latest) or compact == 'a':
        if flow.status == 'idle':
            adopt_legacy_binding(flow, transcript)
        if flow.status in {'paused', 'interrupted'}:
            if is_context_limit_error(flow.last_error):
                flow.status, flow.next_action = 'context_blocked', ''
                return workflow_status_text(flow)
            if flow.run_id:
                refresh_web_workflow(flow)
            flow.wait_for(flow.next_action)
            return '已恢复当前状态，本轮未执行。请重新确认下面的步骤。\n\n' + workflow_status_text(flow)
        if flow.status == 'awaiting_inputs':
            return workflow_plan_text(flow)
        if flow.status != 'awaiting_confirmation' or not flow.next_action:
            return workflow_status_text(flow)
        return await execute_web_step(flow, checkpoint)
    # Bind a saved run only when the user names it, or migrate an existing chat once.
    explicit_run = extract_workflow_run_id(latest)
    if explicit_run:
        flow.reset()
        bind_web_run(flow, extract_model_name(latest), explicit_run)
        refresh_web_workflow(flow)
        flow.wait_for(flow.next_action)
        return '已绑定指定运行，本轮未执行。\n\n' + workflow_status_text(flow)
    if flow.status == 'idle' and contains_any(latest, ['诊断', '核查', '报告', '证据', '原因', '为什么']):
        adopt_legacy_binding(flow, transcript)
    if flow.run_id and contains_any(latest, ['诊断', '主因', '原因', '为什么', '分析', '调查']):
        if flow.task_id:
            # A changed question is a new task proposal, never an implicit diagnosis.
            flow.question = latest
            flow.task_id = ''
            flow.state_token = web_state_token(flow)
            flow.wait_for('prepare_diagnosis')
            return '已记录新的调查问题；确认后只准备新诊断任务。\n\n' + workflow_status_text(flow)
        return workflow_status_text(flow)
    if flow.run_id and contains_any(latest, ['核查', '报告', '证据']):
        return '请按当前状态依次推进。\n\n' + workflow_status_text(flow)
    if not is_planning_only_request(latest) and not extract_rainfall_file(latest):
        evidence = scenario_agent_evidence(latest)
        if evidence is not None:
            explanation = await explain_validation_evidence(latest, evidence)
            return '本轮只查询模型／降雨资料。\n\n' + explanation
    # No mutation tools are supplied to the model, including vague or unexpected requests.
    return await run_web_orchestrator_agent_turn(build_task_prompt(transcript, flow), planning_only=True)

def latest_user_model(transcript):
    for item in reversed(transcript):
        if item['role'] == 'user':
            model = extract_model_name(item['content'])
            if model:
                return model
    return ''

def set_scenario_plan(flow):
    flow.next_action = 'scenario'
    flow.status = 'awaiting_confirmation' if flow.model_name and flow.rainfall_file else 'awaiting_inputs'
    flow.version += 1

def workflow_plan_text(flow):
    text = ('本轮只制定计划，尚未执行。每次回复“继续”只推进一个动作：\n\n'
            '1. 准备情景请求。\n2. 执行 SWMM-2D 模拟。\n3. 构建统一证据。\n'
            '4. 准备诊断任务。\n5. 执行诊断。\n6. 核查引用。\n7. 生成并交付报告。\n\n')
    if not flow.model_name:
        return text + '请明确选择 models/ 下的模型项目；系统不会自动选择模型。'
    if not flow.rainfall_file:
        events = MODELS_DIR / flow.model_name / 'events'
        files = [p.relative_to(MODELS_DIR / flow.model_name).as_posix() for p in sorted(events.rglob('*'))
                 if p.is_file() and p.suffix.lower() in {'.txt', '.csv', '.dat', '.rain', '.prcp', '.precip', '.ts'}]
        return text + f'当前模型：`{flow.model_name}`。请明确选择降雨文件：\n' + '\n'.join(f'- `{p}`' for p in files)
    return text + workflow_status_text(flow)

def workflow_status_text(flow):
    labels = {'idle': '未开始', 'awaiting_inputs': '等待选择输入', 'awaiting_confirmation': '等待确认',
              'running': '执行中', 'paused': '已暂停', 'interrupted': '步骤中断', 'completed': '已完成',
              'context_blocked': '输入超限，等待缩小调查范围'}
    lines = []
    metadata = [f'流程状态：{labels.get(flow.status, flow.status)}。']
    if flow.model_name:
        metadata.append(f'模型：`{flow.model_name}`；情景：`{flow.scenario_name}`。')
    if flow.rainfall_file:
        metadata.append(f'降雨文件：`{flow.rainfall_file}`。')
    if flow.run_id:
        metadata.append(f'run_id: `{flow.run_id}`。')
    if flow.task_id:
        metadata.append(f'task_id: `{flow.task_id}`。')
    if flow.status == 'context_blocked':
        lines.append('输入证据超限，已停止重试。请指定节点／事件，建立范围更小的调查任务。')
    elif flow.next_action:
        lines.append('下一步：' + ACTION_LABELS[flow.next_action].split('：', 1)[-1] + '；回复“继续”确认。')
    elif flow.status == 'completed':
        lines.append('当前流程已完成；再次“继续”不会重跑诊断或模拟。')
    else:
        lines.append('当前没有待执行步骤，请先明确任务、模型与降雨文件，或指定已有 run_id。')
    lines.append(details('运行信息', '\n'.join(metadata)))
    return '\n\n'.join(lines)

def bind_web_run(flow, model_name, run_id, task_id=''):
    from workflow_agents.common.state import resolve_run_root
    root = resolve_run_root(model_name or '__unspecified__', run_id)
    if model_name and root.parents[1].name != model_name:
        raise ValueError('指定模型与 run_id 不一致；未切换到其他模型。')
    flow.model_name, flow.run_id, flow.task_id = root.parents[1].name, root.name, task_id

def adopt_legacy_binding(flow, transcript):
    binding = investigation_binding(transcript)
    if binding:
        bind_web_run(flow, *binding)
        refresh_web_workflow(flow)
        if flow.task_id and flow.next_action == 'diagnose':
            state = load_active_task(flow.model_name, flow.run_id, flow.task_id)
            attempt = state.get('last_attempt', {})
            if attempt.get('status') == 'failed' and attempt.get('http_status') == 400:
                # Older audit files did not save the provider error message.
                for item in reversed(transcript):
                    if item['role'] == 'assistant' and flow.task_id in item['content']:
                        if is_context_limit_error(item['content']):
                            flow.last_error = next((line for line in item['content'].splitlines() if is_context_limit_error(line)), '')
                            flow.status, flow.next_action = 'context_blocked', ''
                        break
        flow.wait_for(flow.next_action)

def web_state_token(flow):
    root = MODELS_DIR / flow.model_name / 'runs' / flow.run_id
    paths = [root / 'workflow_state.json', root / 'diagnosis_tasks' / 'active_task.json']
    if flow.task_id:
        paths.append(root / 'diagnosis_tasks' / flow.task_id / 'task.json')
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode('utf-8'))
        digest.update(path.read_bytes() if path.exists() else b'absent')
    return digest.hexdigest()

def refresh_web_workflow(flow):
    root, run = load_or_initialize_state(flow.model_name, flow.run_id)
    if root.parents[1].name != flow.model_name:
        raise ValueError('当前绑定的模型／运行不一致。')
    if run['state'] == EVIDENCE_READY:
        state = load_active_task(flow.model_name, flow.run_id, flow.task_id)
        if state:
            flow.task_id = state['task_id']
            flow.next_action = TASK_ACTIONS.get(state['state'], '')
            if state['state'] in {'preliminary_delivered', 'awaiting_evidence_confirmation'}:
                display = state.get('display_report', {})
                if display.get('status') == 'failed':
                    flow.next_action = 'display_report'
            attempt = state.get('last_attempt', {})
            if attempt.get('status') == 'failed' and is_context_limit_error(attempt.get('error_message', '')):
                flow.last_error = attempt['error_message']
                flow.status, flow.next_action = 'context_blocked', ''
        else:
            flow.next_action = 'prepare_diagnosis'
    else:
        flow.next_action = {SCENARIO_READY: 'simulation', RUN_READY: 'evidence_building',
                            NO_SURFACE_INFLOW: 'evidence_building'}.get(run['state'], '')
        if not flow.next_action:
            raise ValueError('当前运行未就绪，不能推进；请检查工作流错误或创建新情景。')
    flow.state_token = web_state_token(flow)

async def execute_web_step(flow, checkpoint=None):
    # A second browser/session cannot spend an approval against a changed run.
    key = (flow.model_name, flow.run_id) if flow.run_id else (flow.model_name, flow.rainfall_file)
    lock = run_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _execute_web_step(flow, checkpoint)

async def _execute_web_step(flow, checkpoint=None):
    action = flow.next_action
    if flow.run_id and web_state_token(flow) != flow.state_token:
        refresh_web_workflow(flow)
        flow.wait_for(flow.next_action)
        return '保存状态已变化，本轮未执行；已重新读取进度，请确认当前下一步。\n\n' + workflow_status_text(flow)
    flow.status = 'running'
    if checkpoint:
        checkpoint()
    try:
        if action == 'scenario':
            result = prepare_scenario_request(model_name=flow.model_name, rainfall_file=flow.rainfall_file,
                scenario_name=flow.scenario_name,
                run_id=f"{Path(flow.rainfall_file).stem}__{flow.scenario_name}__{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}")
            flow.run_id = result['scenario_request']['run_id']
            detail = '情景请求已准备，尚未执行模拟。'
        elif action == 'simulation':
            root, state = load_or_initialize_state(flow.model_name, flow.run_id)
            if state['state'] != SCENARIO_READY:
                raise ValueError('模拟不是当前允许的步骤。')
            detail = run_simulation_from_scenario_request(load_scenario_request(flow.model_name, flow.run_id))
            _, after = load_or_initialize_state(flow.model_name, flow.run_id)
            if after['state'] not in {RUN_READY, NO_SURFACE_INFLOW}:
                raise ValueError('模拟未成功完成；请检查执行结果：' + detail)
            summary = json.loads((root / 'summary.json').read_text(encoding='utf-8')) if (root / 'summary.json').exists() else {}
            ca2d = summary.get('ca2d', {})
            brief = '模拟已完成。'
            if ca2d.get('max_depth_m') is not None:
                brief += f"CA2D 最大水深：{ca2d['max_depth_m']} m。"
            if after['state'] == NO_SURFACE_INFLOW:
                brief += '本次无正节点溢流，二维边界入流为零。'
            detail = brief + '\n\n' + details('完整模拟结果', detail)
        elif action == 'evidence_building':
            detail = run_workflow_stage(model_name=flow.model_name, run_id=flow.run_id, target_stage='evidence_building')
            root, after = load_or_initialize_state(flow.model_name, flow.run_id)
            if after['state'] != EVIDENCE_READY:
                raise ValueError('统一证据构建未成功：' + detail)
            from workflow_agents.evidence.evidence_package import load_package
            overview = load_package(root)['overview']
            brief = (f"统一证据已构建：{overview.get('event_count', '未知')} 个冒溢事件，"
                     f"{overview.get('overflow_node_count', '未知')} 个冒溢节点；尚未执行诊断。")
            detail = brief + '\n\n' + details('完整证据构建结果', detail)
        elif action == 'prepare_diagnosis':
            result = await advance_investigation(flow.model_name, flow.run_id, 'prepare', question=flow.question)
            flow.task_id = result['task_id']
            detail = '诊断任务与可见证据已准备，尚未调用诊断模型。'
        elif action == 'diagnose':
            from workflow_agents.orchestration.investigation_flow import diagnose_with_repair
            result = await diagnose_with_repair(flow.model_name, flow.run_id, flow.task_id)
            detail = format_investigation_result(result, include_next=False)
        elif action in {'verify', 'report', 'evidence', 'display_report'}:
            result = await advance_investigation(flow.model_name, flow.run_id, action, flow.task_id)
            detail = format_investigation_result(result, include_next=False)
        else:
            raise ValueError('没有合法的待执行步骤。')
        refresh_web_workflow(flow)
        flow.last_error = result.get('execution_error', '') if action == 'diagnose' else ''
        if action == 'prepare_diagnosis':
            # A follow-up preparation replaces the prior task binding explicitly.
            flow.question = ''
        flow.wait_for(flow.next_action)
        if is_context_limit_error(flow.last_error):
            flow.status = 'context_blocked'
            flow.next_action = ''
        return f"本轮只执行：{ACTION_LABELS[action].split('：', 1)[-1]}。\n\n{detail}\n\n" + workflow_status_text(flow)
    except Exception as exc:
        # A failed/rejected stage never grants permission for the next stage.
        flow.last_error = str(exc)
        flow.status = 'awaiting_confirmation'
        flow.version += 1
        if is_context_limit_error(exc):
            flow.status, flow.next_action = 'context_blocked', ''
        return '本轮步骤未完成：' + error_summary(exc) + '\n\n未执行后续步骤。\n\n' + workflow_status_text(flow) + '\n\n' + details('完整错误信息', str(exc))
    except BaseException:
        flow.status = 'interrupted'
        flow.version += 1
        raise

def investigation_binding(transcript: list[dict[str, str]]) -> tuple[str, str, str] | None:
    # Work backwards by message, not by regex-pattern priority. A durable run
    # binding remains usable even when the conversation exceeds ten turns.
    run_id = ''
    task_id = ''
    for item in reversed(transcript):
        text = item['content']
        if not task_id:
            ids = re.findall(r'task_id[\s`\"\'=:：()]*([0-9a-f]{32})', text, re.IGNORECASE)
            task_id = ids[-1] if ids else ''
        run_id = extract_workflow_run_id(text)
        if run_id:
            break
    if not run_id:
        return None
    context = '\n'.join(item['content'] for item in transcript)
    model_name = extract_model_name(context)
    # resolve_run_root checks uniqueness across models for stale/absent model names.
    from workflow_agents.common.state import resolve_run_root
    root = resolve_run_root(model_name or '__unspecified__', run_id)
    return root.parents[1].name, root.name, task_id

def format_investigation_result(state: dict[str, Any], include_next: bool = True) -> str:
    current = state['state']
    labels = {'ready_for_diagnosis': '诊断证据已就绪', 'revision_requested': '引用核查未通过，等待修正诊断',
              'ready_for_verification': '诊断已完成，尚未核查', 'ready_for_report': '引用核查通过，尚未生成报告',
              'awaiting_evidence_confirmation': '初步诊断已交付，等待确认补证', 'preliminary_delivered': '初步诊断流程已完成'}
    lines = []
    extra = []
    attempt = state.get('last_attempt', {})
    failure = state.get('execution_error') or (attempt.get('error_message') if attempt.get('status') == 'failed' else '')
    if failure:
        lines.append('本次诊断未完成：' + error_summary(failure))
        extra.append(details('完整错误信息', failure))
    else:
        lines.append('结果：' + labels.get(current, current) + '。')
        overview = state.get('evidence_overview', {})
        if 'event_count' in overview:
            lines.append(f"范围：{overview['event_count']} 个冒溢事件，{overview.get('overflow_node_count', '未知')} 个冒溢节点。")
        # Never show a previous revision's conclusion as the current result.
        diagnosis = state.get('diagnosis', {})
        if diagnosis.get('revision') == state.get('revision') and current in {'ready_for_verification', 'ready_for_report', 'awaiting_evidence_confirmation', 'preliminary_delivered'}:
            finding = diagnosis.get('run_overview', {}).get('text')
            if finding:
                if len(finding) > 220:
                    sentences = re.split(r'(?<=[。！？])', finding)
                    preview = ''.join(sentences[:2])
                    if len(preview) > 220:
                        preview = preview[:220] + '…'
                    lines.append('要点摘要：' + preview)
                    extra.append(details('完整诊断要点', finding))
                else:
                    lines.append('要点：' + finding)
    if state.get('evidence_plan'):
        labels = {'available': '可补充', 'already_visible': '已经读取', 'no_match': '请求对象／时段无匹配项', 'unavailable': '当前保存数据或提取方法不能满足'}
        counts = {status: sum(item['status'] == status for item in state['evidence_plan']) for status in labels}
        lines.append('补证：' + '；'.join(f'{labels[s]} {n} 项' for s, n in counts.items() if n) + '。')
        requests = []
        for item in state['evidence_plan']:
            req = item['request']
            requests.append(f"- {labels[item['status']]}：{req.get('reason', '')}")
            if item.get('execution') == 'saved_result_extract':
                resolved = item.get('resolved_request', {})
                requests.append('方法：本次保存结果提取／计算；' + resolved.get('scope_note', ''))
                for scope in resolved.get('scopes', []):
                    objects = scope['nodes'] + scope['links'] + scope['cells']
                    requests.append(f"{scope['event_id'] or '无事件限定'}：{', '.join(objects)}；{scope['time_start'] or '保存起点'} 至 {scope['time_end'] or '保存终点'}。")
            if item.get('reason'):
                requests.append('能力边界：' + item['reason'])
        extra.append(details('补证目的、对象与时段（确认前可展开核对）', '\n\n'.join(requests)))
    if state.get('report_revision') == state.get('revision') and state.get('report_file'):
        label = '查看此前交付的文字报告' if current in {'ready_for_diagnosis', 'revision_requested'} else '查看完整文字报告'
        lines.append(model_file_link(state['report_file'], label))
    display = state.get('display_report', {})
    if display.get('status') == 'completed' and display.get('revision') == state.get('revision'):
        label = '查看此前交付的 HTML 报告' if current in {'ready_for_diagnosis', 'revision_requested'} else '打开 HTML 诊断报告'
        lines.append(model_file_link(display['html_file'], label))
    elif display.get('status') == 'failed':
        lines.append('HTML 报告未生成，已保留完整文字报告。')
        extra.append(details('报告生成错误', display.get('error', '请检查生成记录后重试。')))
    if include_next and not is_context_limit_error(failure):
        action = TASK_ACTIONS.get(current)
        if action:
            lines.append('下一步：' + ACTION_LABELS[action].split('：', 1)[-1] + '；回复“继续”确认。')
    extra.append(details('诊断任务信息', f"task_id: `{state['task_id']}`；状态：`{current}`；版本：{state.get('revision', 0)}。"))
    return '\n\n'.join(lines + extra)

def model_file_link(filename: str, label: str) -> str:
    from urllib.parse import quote
    path = Path(filename).resolve()
    try:
        relative = path.relative_to(MODELS_DIR.resolve()).as_posix()
        return f'[{label}](/models/{quote(relative, safe="/")})'
    except ValueError:
        return f'{label}：`{path}`'


def scenario_agent_evidence(message: str) -> str | None:
    """Route model/project/event organization to the workflow ScenarioAgent."""
    model_name = extract_model_name(message)
    rainfall_file = extract_rainfall_file(message)
    run_id = extract_run_id(message)
    scenario_name = extract_scenario_name(message) or "baseline"
    wants_prepare = contains_any(message, ["准备", "创建", "生成", "scenario_request", "情景请求", "场景请求", "prepare scenario"])
    if wants_prepare and rainfall_file:
        result = prepare_scenario_request(
            model_name=model_name,
            rainfall_file=rainfall_file,
            event_name=Path(rainfall_file.replace("\\", "/")).stem,
            scenario_name=scenario_name,
            run_id=run_id,
        )
        return "ScenarioAgent evidence:\n" + json.dumps(result, ensure_ascii=False, indent=2)

    wants_models = contains_any(message, ["models", "模型项目", "项目", "模型"])
    wants_list = contains_any(message, ["列出", "list", "available"])
    wants_check = contains_any(message, ["检查", "验证", "完整", "check", "validate"])
    wants_ca2d = contains_any(message, ["ca2d", "静态模型", "static"])
    wants_rainfall = contains_any(message, ["降雨事件", "降雨文件", "rainfall event", "list rainfall"])
    wants_simulation_or_analysis = contains_any(
        message,
        ["模拟", "运行", "run", "simulate", "plot", "绘图", "图", "报告", "分析"],
    )

    if wants_rainfall:
        return "ScenarioAgent evidence:\n" + list_rainfall_events(model_name)

    if wants_ca2d and wants_check:
        target = f"models/{model_name}" if model_name else ""
        return "ScenarioAgent evidence:\n" + check_ca2d_model(target)

    if wants_ca2d and not wants_simulation_or_analysis:
        target = f"models/{model_name}" if model_name else ""
        return "ScenarioAgent evidence:\n" + check_ca2d_model(target)

    if wants_models and wants_list and wants_check:
        if model_name:
            return "ScenarioAgent evidence:\n" + check_swmm_2d_project(model_name)
        return "ScenarioAgent evidence:\n" + check_all_swmm_2d_projects()

    if wants_models and wants_list:
        return "ScenarioAgent evidence:\n" + list_swmm_2d_models()

    if wants_models and wants_check:
        if model_name:
            return "ScenarioAgent evidence:\n" + check_swmm_2d_project(model_name)
        return "ScenarioAgent evidence:\n" + check_all_swmm_2d_projects()

    if "models" in message.lower() and not wants_simulation_or_analysis:
        return "ScenarioAgent evidence:\n" + check_all_swmm_2d_projects()

    if model_name and not wants_simulation_or_analysis:
        return "ScenarioAgent evidence:\n" + check_swmm_2d_project(model_name)

    return None


def is_continue_confirmation(message: str) -> bool:
    compact = message.strip().lower()
    for token in [" ", "\t", "\r", "\n", "，", "。", ",", ".", "!", "！", "?", "？"]:
        compact = compact.replace(token, "")
    return compact in {
        "继续",
        "继续补证",
        "同意补证",
        "开始补证",
        "开始",
        "下一步",
        "执行",
        "运行",
        "确认",
        "好的",
        "好",
        "是的",
        "可以",
        "按此计划执行",
        "按计划执行",
        "continue",
        "go",
        "yes",
        "ok",
    }

def extract_rainfall_file(text: str) -> str:
    normalized = text.replace("\\", "/")
    patterns = [
        r"(models/[A-Za-z0-9_.-]+/events/[^\s，。；;\"'<>]+?\.(?:txt|csv|dat|rain|prcp|precip|ts))",
        r"(events/[^\s，。；;\"'<>]+?\.(?:txt|csv|dat|rain|prcp|precip|ts))",
        r"([A-Za-z0-9_.-]*rain[A-Za-z0-9_.\-/]*\.(?:txt|csv|dat|rain|prcp|precip|ts))",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        if value.lower().startswith("models/"):
            parts = value.split("/", 2)
            if len(parts) == 3:
                return parts[2]
        if "/" not in value:
            return f"events/{value}"
        return value
    return ""

def extract_run_id(text: str) -> str:
    patterns = [
        r"(?:run_id|运行ID|运行编号|输出编号)\s*[:=：]\s*([A-Za-z0-9_.-]+)",
        r"(?:保存为|命名为|编号为)\s*([A-Za-z0-9_.-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""

def extract_workflow_run_id(text: str) -> str:
    patterns = [
        r"[`'\"]?run_id[`'\"]?\s*[:=：]\s*[`'\"]?([A-Za-z0-9_.-]+)",
        r"(?:运行ID|运行编号|输出编号)\s*[:=：]\s*[`'\"]?([A-Za-z0-9_.-]+)",
        r"\b([A-Za-z0-9_]+__[A-Za-z0-9_.-]+__\d{8}_\d{6})\b",
        r"\b(agentic2_[A-Za-z0-9_.-]+)\b",
        r"\b(my_[A-Za-z0-9_.-]+)\b",
    ]
    matches = [(match.end(), -priority, match.group(1)) for priority, pattern in enumerate(patterns)
               for match in re.finditer(pattern, text, flags=re.IGNORECASE)]
    return max(matches)[2] if matches else extract_run_id(text)

def extract_scenario_name(text: str) -> str:
    patterns = [
        r"(?:scenario|情景|场景)\s*[:=：]\s*([A-Za-z0-9_.-]+)",
        r"scenarios/([A-Za-z0-9_.-]+)/model\.inp",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    if "baseline" in text.lower():
        return "baseline"
    return ""

def contains_any(text: str, markers: list[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)

def extract_model_name(text: str) -> str:
    normalized = text.replace("\\", "/")
    path_match = re_search_model_path(normalized)
    if path_match and (MODELS_DIR / path_match).is_dir():
        return path_match
    model_roots = sorted(path for path in MODELS_DIR.iterdir() if path.is_dir()) if MODELS_DIR.exists() else []
    # Only an exact identifier token is accepted. For example,
    # "suanliguanw_swmm_2d" must never match "Gsuanliguanw_swmm_2d".
    matches = [
        model_root.name
        for model_root in model_roots
        if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(model_root.name)}(?![A-Za-z0-9_.-])", text, flags=re.IGNORECASE)
    ]
    return matches[0] if len(matches) == 1 else ""

def re_search_model_path(text: str) -> str:
    import re

    match = re.search(r"models/([A-Za-z0-9_.-]+)", text)
    if match and match.group(1).lower() != "models":
        return match.group(1)
    return ""

def build_task_prompt(transcript: list[dict[str, str]], flow: WebWorkflow | None = None) -> str:
    conversation = "\n".join(
        f"{item['role']}: {chat_history_summary(item['content']) if item['role'] == 'assistant' else item['content']}" for item in transcript[-12:]
    )
    latest = transcript[-1]["content"]
    model_inventory = build_model_inventory_context()
    live_context = ''
    try:
        binding = ((flow.model_name, flow.run_id, flow.task_id) if flow.run_id else None) if flow is not None else investigation_binding(transcript)
        if binding:
            model_name, run_id, task_id = binding
            state = load_active_task(model_name, run_id, task_id)
            if state:
                fields = ('task_id', 'model_name', 'run_id', 'state', 'revision', 'report_revision',
                          'report_file', 'display_report_file', 'pending_evidence_requests', 'evidence_closure')
                live_context = 'Persisted active task state:\n' + json.dumps({k: state[k] for k in fields if k in state}, ensure_ascii=False)
    except (ValueError, FileNotFoundError) as exc:
        live_context = 'Persisted task binding could not be resolved: ' + str(exc)
    approval_context = ('Application approval state (authoritative):\n' + json.dumps(flow.to_dict(), ensure_ascii=False)) if flow is not None else ''
    return (
        "You are continuing a human-in-the-loop SWMM-Agentic3 workflow-stage web chat.\n"
        "The active architecture is StatefulOrchestrator plus workflow-stage agents: "
        "ScenarioAgent, SimulationAgent, EvidenceBuilderAgent, DiagnosisAgent, VerificationAgent, and ReportAgent.\n"
        "At service startup and before handling user work, treat the project-root models/ directory as the source of truth.\n"
        "Never assume data/example.inp exists or use it as a default model.\n"
        f"{model_inventory}\n\n"
        f"{live_context}\n\n"
        f"{approval_context}\n\n"
        "Use the transcript to preserve the current plan and next-step state.\n"
        "If the latest user message is a confirmation, continue with exactly the next step.\n"
        "If there is no clear next step in the transcript, ask the user to confirm the next step instead of guessing.\n"
        "After evidence construction call DiagnosisAgent(model_name, run_id) without a question to deliver the default whole-run preliminary overflow diagnosis.\n"
        "User follow-up questions prepare scoped tasks. Preserve task_id for subsequent diagnosis, evidence retrieval, verification and reports.\n"
        "Deliver the preliminary report before retrieving pending evidence. There is no legacy run-level diagnosis fallback.\n"
        "Never say a tool or simulation succeeded unless the current turn actually observed that result.\n"
        "If it is a new request, give a concise plan and ask whether to continue.\n\n"
        f"Transcript:\n{conversation}\n\n"
        f"Latest user message:\n{latest}"
    )

def build_model_inventory_context() -> str:
    if not MODELS_DIR.exists():
        return f"Current model inventory: no project-root models directory exists at {MODELS_DIR}."

    model_lines = []
    for model_root in sorted(path for path in MODELS_DIR.iterdir() if path.is_dir()):
        baseline = model_root / "swmm" / "scenarios" / "baseline" / "model.inp"
        events_dir = model_root / "events"
        event_files = []
        if events_dir.exists():
            event_files = [
                str(path.relative_to(model_root))
                for path in sorted(events_dir.rglob("*"))
                if path.is_file() and path.suffix.lower() in {".txt", ".csv", ".dat", ".rain", ".prcp", ".precip", ".ts"}
            ]
        model_lines.append(
            "- "
            f"{model_root.name}: model_yaml={(model_root / 'model.yaml').exists()}, "
            f"baseline_inp={baseline.exists()}, "
            f"events={event_files or 'none'}"
        )

    if not model_lines:
        return f"Current model inventory: project-root models directory is empty at {MODELS_DIR}."
    return "Current model inventory from project-root models/:\n" + "\n".join(model_lines)

def is_planning_only_request(message: str) -> bool:
    normalized = message.lower()
    planning_markers = [
        "\u5148\u7ed9\u8ba1\u5212",
        "\u4e0d\u8981\u7acb\u5373\u6267\u884c",
        "\u4e0d\u8981\u6267\u884c",
        "\u522b\u6267\u884c",
        "\u53ea\u7ed9\u8ba1\u5212",
        "\u4ec5\u7ed9\u8ba1\u5212",
        "plan only",
        "do not execute",
        "don't execute",
    ]
    return any(marker in normalized for marker in planning_markers)

def repair_mojibake(text: str) -> str:
    if not looks_like_mojibake(text):
        return text
    try:
        repaired = text.encode("gbk", errors="strict").decode("utf-8", errors="strict")
    except UnicodeError:
        return text
    if chinese_score(repaired) > chinese_score(text):
        return repaired
    return text

def looks_like_mojibake(text: str) -> bool:
    if any("\ue000" <= char <= "\uf8ff" for char in text):
        return True
    markers = [
        "\u7487",
        "\u9359",
        "\u93b4",
        "\u6d93",
        "\u6b22",
        "\u95c6",
        "\u6a3f",
        "\u951b",
        "\u6d93",
        "\u6f36",
        "\u935a",
        "\u5d41",
        "\u5d46",
        "\u20ac",
        "\ufffd",
    ]
    return any(marker in text for marker in markers)

def chinese_score(text: str) -> int:
    common = set(
        "\u7684\u4e00\u662f\u5728\u4e86\u8bf7\u68c0\u67e5\u9879\u76ee\u662f\u5426\u5b8c\u6574"
        "\u5148\u7ed9\u8ba1\u5212\u4e0d\u8981\u7acb\u5373\u6267\u884c\u7ee7\u7eed\u6a21\u62df"
    )
    return sum(1 for char in text if char in common)

def is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "ratelimiterror" in exc.__class__.__name__.lower()
        or "rate limit" in text
        or "rate limiting" in text
        or "system is too busy" in text
        or "error code: 429" in text
        or "'code': 50609" in text
    )

def is_llm_timeout_error(exc: Exception) -> bool:
    text = str(exc).lower()
    class_name = exc.__class__.__name__.lower()
    return (
        "apitimeouterror" in class_name
        or "readtimeout" in class_name
        or "request timed out" in text
        or "readtimeout" in text
        or "httpx.readtimeout" in text
        or "httpcore.readtimeout" in text
    )
