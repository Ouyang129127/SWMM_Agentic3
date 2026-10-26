# Example1 首轮诊断输入与输出：文件导读

本目录于 2026-10-22 从用户指定运行的历史文件离线整理。没有运行模拟，没有调用 LLM，没有改动原运行文件。

范围只包括首次诊断 R1；补证后 R2 和展示报告 Report Agent 的调用另计。

## 先回答三个问题

1. DiagnosisAgent 不直接打开这些本地文件。Python 程序读取文件、构建证据并选取记录。
2. 选取单位是有对象、事件、指标和来源的结构化 JSON 证据记录；本链路没有语句切块、向量化或向量库检索。evidence_snapshot.json 是本地冻结快照，内存中的 rows 是列表。
3. 首次诊断将系统提示词与一份包含 22 条可见证据及任务上下文的 JSON 文本一起交给一次诊断模型调用。没有把全部原始文件或整个证据包原样上传。

## 真实材料链条

- 原始结果在 `E:\SWMM_Agentic\SWMM-Agentic3\models\Example1\runs\diagnostic_single_4h_20mm_5min__baseline__20261021_230830_fcffdca6` 中：summary.json、swmm/model_with_event.inp、swmm/model.out、swmm/nodes.tsv、swmm/links.tsv、ca2d/surface_depth.tsv、swmm/rainfall_for_simulation.txt。
- EvidenceBuilder 程序生成 evidence/evidence_package.json，包含 28,552 条证据。
- 任务准备程序生成 evidence_snapshot.json：保留原证据并添加 14 条全场标量汇总、1 条冒溢事件概览，共 28,567 条。
- task.json 记录任务与选取状态。首轮 visible_ids 应看首轮 diagnosis_attempt 的记录；现在的 task.json 已推进到 R2。
- R1 可见 22 条：6 条事件证据、1 条降雨背景、14 条全场标量汇总、1 条事件概览。
- local_process_series 是一条证据，但内部包含 207 个时间样本，覆盖 01:36:35—01:53:45、节点 9/10/21 和管段 1/6。22 条记录不等于 22 句话。

## 建议按这个顺序打开

- [节点10输入节选](06_node10_input_excerpt.json)：事件、结构、峰值样本、来源组成和储量可用情况。节选只为阅读，不是模型的全部输入。
- [22条可见证据目录](05_visible_evidence_index.csv)：每条记录的ID、指标、长度和过程样本数。
- [完整用户载荷重建版](01_user_payload_reconstructed.json)：展开 visible_evidence 即可看到22条完整记录。
- [系统提示词](02_system_prompt_current_source.txt)：从当前 prompts.py 原样提取的字符串。
- [两条API消息重建版](04_api_messages_reconstructed.json)：system和user一起构成一次诊断请求。
- [历史模型响应](07_recorded_model_response.json)：从R1调用日志直接复制的parsed_response，尚未加程序的D1:C编号。
- [重建核对记录](09_reconstruction_manifest.json)：输入规模、token用量和来源哈希。

## 首轮调用规模

- 模型：deepseek-flash；日志时间：2026-10-21T15:13:09.979037+00:00。
- 系统提示词 10,205 字符；用户消息 204,343 字符；合计 214,548 字符，与日志一致。
- 接口记载 prompt_tokens=81,570，completion_tokens=27,837。token与字符不是同一计量。
- 首轮 previous_diagnosis=null、verification_feedback=null。

## 输入重建的准确性边界

历史诊断日志保存了可见ID、证据绑定、输入字符数和模型原始响应，没有保存完整请求正文及当时系统提示词哈希。因此01—04是依据当前源码和历史首轮状态重建；合计字符数与历史日志相符，不称为当时API请求的原始抓包。07—08直接来自历史响应。

## 对照源代码

- `E:\SWMM_Agentic\SWMM-Agentic3\workflow_agents\evidence\evidence_builder.py`：原始结果到证据包。
- `E:\SWMM_Agentic\SWMM-Agentic3\workflow_agents\evidence\event_packages.py`：按事件、拓扑和时窗抽取完整局部过程。
- `E:\SWMM_Agentic\SWMM-Agentic3\workflow_agents\evidence\evidence_selection.py`：按任务选记录、生成全场汇总。
- `E:\SWMM_Agentic\SWMM-Agentic3\workflow_agents\orchestration\tasks.py`：创建快照及初始状态。
- `E:\SWMM_Agentic\SWMM-Agentic3\workflow_agents\diagnosis\service.py`：构建载荷、序列化、调用模型、解析返回。
- `E:\SWMM_Agentic\SWMM-Agentic3\workflow_agents\diagnosis\prompts.py`：DiagnosisAgent系统提示词。

展示用HTML报告另有Report Agent调用。其report_prompt.json是报告写作输入，不能当作本次DiagnosisAgent的输入。
