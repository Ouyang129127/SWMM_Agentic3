# SWMM-Agentic3

按论文中的六个工作环节组织的最小程序版本。保留 SWMM→CA2D 正式降雨模拟、证据构建、诊断、补证、引用核查和报告生成，以及网页和命令行入口。

本目录不附带具体模型、降雨、历史运行结果、聊天记录、旧虚拟环境或 API 密钥。`models/` 目前只有放置说明。加入模型前可以检查导入、网页启动、空模型提示和离线工作流测试；完整模拟验证需要后续放入模型。

## 安装与启动

已验证的 Python 版本：3.11.5。以下命令在本目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

需要调用模型时，将 `.env.example` 复制为 `.env` 并配置自己的 API 密钥。确定性的模型检查、证据处理和离线测试不需要真实密钥。API 模型名沿用原项目配置，可以通过 `DEEPSEEK_MODEL` 修改。

```powershell
.\run_web.ps1
```

浏览器访问 `http://127.0.0.1:8001`。也可以运行 `run_web.bat`。网页负责展示和提交操作，后端调度负责确认、任务绑定、合法状态转换和各环节执行。运行日志按需写入 `runtime/web_sessions/`。

命令行交互入口：

```powershell
.venv\Scripts\python.exe main.py "列出可用模型"
```

命令行逐动作执行入口：

```powershell
.venv\Scripts\python.exe run_workflow.py --model MODEL --run-id RUN --stage evidence_building
.venv\Scripts\python.exe run_workflow.py --model MODEL --run-id RUN --investigation-action prepare
.venv\Scripts\python.exe run_workflow.py --model MODEL --run-id RUN --investigation-action diagnose --task-id TASK
.venv\Scripts\python.exe run_workflow.py --model MODEL --run-id RUN --investigation-action verify --task-id TASK
.venv\Scripts\python.exe run_workflow.py --model MODEL --run-id RUN --investigation-action report --task-id TASK
```

`MODEL`、`RUN`、`TASK` 是占位符，替换为实际名称。`initial` 和 `continue` 是保留的组合调度动作；网页继续采用每次确认执行一步的流程。

## 论文环节与代码对应

| 环节 | 入口 | 主要实现 |
|---|---|---|
| ScenarioAgent | `workflow_agents/scenario/agent.py` | `requests.py`：情景请求；`validation.py`：模型、静态资源和降雨目录检查 |
| SimulationAgent | `workflow_agents/simulation/agent.py` | `service.py`：共享运行与状态保存；`runner.py`：降雨管线；`ca2d.py`、`timing.py`：求解与输出 |
| EvidenceBuilderAgent | `workflow_agents/evidence/agent.py` | `evidence_builder.py`、事件/拓扑/精度模块；`requests.py`、`supplement.py`：补证规划与执行 |
| DiagnosisAgent | `workflow_agents/diagnosis/agent.py` | `service.py`：模型调用与输出校验；`prompts.py`、`contract.py`、`task_scope.py` |
| VerificationAgent | `workflow_agents/verification/agent.py` | `service.py`、`reference_checks.py`：引用、对象、事件及证据绑定检查 |
| ReportAgent | `workflow_agents/reporting/agent.py` | `delivery.py`、`internal_report.py`：任务报告交付；`pipeline.py`：材料、写作、绘图和 HTML |

支持模块：

- `orchestration/`：跨环节调度、任务生命周期、会话控制、重试和状态恢复。`investigation.py` 调用各环节服务，`controller.py` 管理交互确认。
- `common/`：项目路径、环境配置、LLM 客户端、JSON、哈希、运行状态和共享输入处理。
- `interfaces/`：命令行适配、网页数据结构和展示格式。根目录 `web_app.py` 仅保留 HTTP 路由和静态内容适配。
- `web/index.html`：网页展示与操作。网页与命令行共用 `simulation/service.py`，不再各自保存模拟完成状态。

核查环节当前实现的是可追溯性检查，尚未实现独立的水力解释或因果结论认证。未来相关能力应在 `verification/` 扩展；本次重构不将其标为已实现。

## 数据、资源和路径

项目根目录统一由 `common/paths.py` 定位，不依赖启动时的工作目录。默认模型目录为 `models/`，会话运行目录为 `runtime/`。可以在环境变量或本项目 `.env` 中设置 `SWMM_MODELS_DIR`、`SWMM_RUNTIME_DIR`，建议使用绝对路径，修改后重启进程。

模型布局见 `models/README.md`。模拟结果继续保存在模型的 `runs/<run_id>/` 下；程序不会自动寻找或借用旧项目的模型、密钥或结果。旧结果中的绝对路径属于结果元数据，移动旧结果时仍需重新绑定；回归脚本在临时副本内完成这一处理。

`reporting/prompts/` 中的固定文字模板、地表口径和四个 `event_example*.json` 都是运行资源。教学示例会进入报告写作提示，并参与生成依赖哈希；它们不是当前案例的证据。本次保持这些教学内容与原项目一致，包括旧 P7 案例示例和场景展示别名；其通用化、因果表述审查应单独评估。未被读取的 `event_rewrite_v1.md` 已排除。

报告缓存会检查代码/提示资源、证据源和诊断修订版本；迁移后的首次报告需要重新生成，不承诺复用旧目录缓存。

## 验证与回归

### 首轮调查范围（2026-10-21更新）

首轮详细证据采用 **上游一段带节点、下游一支带边界**：

- 上游保留冒溢节点的每条直接接入管段及其另一端节点，到这里停止；不再追溯这些节点的上游或其他接管。
- 下游沿连续通道，遇到第一个其他**汇流节点**、设备管段（泵、闸、堰、孔口等）、调蓄/特殊节点、末端排口或终点停止。保留边界对象及其保存过程；设备管段保留本体和两端节点，不继续越过设备。
- 沿程或边界的侧支、额外交换管段及外端节点不自动纳入。目标自身作为调查起点，不因其是汇流节点或调蓄节点而原地停止。
- 下游遇到纯分流时停止，记录 `downstream_branch_unresolved`、`status=partial` 和 `unresolved_branches`，需要补证确定路径；不随机选一路或自动追遍所有分支。目标本身多出口时保留直接交换管段和端点用于来水核对，不继续展开。
- 环路和缺失定义明确记录停止原因。方向来自输入连接，不等于实际流向或致因方向。

Example1：N10的首轮范围为节点9、10、21与管段1、6；以N21为目标时，为节点10、20、21、22、16与管段5、6、7、8。前者在N21停止，后者在N16停止。

该范围用于首轮事件结构和详细时序，不删除本地全场证据库、事件目录或全场标量摘要，不截断选中对象的保存样本。补证仍按明确的对象、变量、时窗执行，可越过首轮边界：节点过程请求保留其直接连接管段及两端节点；不会自动恢复旧的沿程侧支追溯。

证据包升级为 **1.2**，拓扑策略为 `upstream_one_segment_downstream_confluence_v1`。旧1.1证据包、旧任务快照保留，但不能继续充当新规则的输入。对已有模拟重新执行 `evidence_building`，再创建新诊断任务即可，无须重跑模拟。程序会使旧证据对应的运行回到待构建证据状态；重启服务加载新代码后执行即可。

用已有结果验证此次范围调整（原运行只读，完整证据和新诊断输入在临时副本构建，不调用LLM）：

```powershell
.venv\Scripts\python.exe scripts/check_scope_upgrade.py --run "models\Example1\runs\RUN" --inspect-node 21 --output "outputs\scope-upgrade-validation\result.json"
```

该检查确认保留样本、时刻、结构参数、事件事实、直接来水组成及全场指标保持一致，只允许预期的拓扑范围变化；同时检查原运行全部文件哈希未变化。`--inspect-node`只检查指定节点的拓扑，不为其伪造冒溢事件。

安装测试依赖并执行通用测试：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

保留的测试使用临时数据、模拟的模型响应及 HTTP 内存传输，覆盖事件与证据规则、诊断输出约束、补证循环、引用绑定、报告生成、会话恢复和逐步确认。区域准备、批量案例和论文绘图脚本未带入。

使用已有运行结果进行回归，不重新模拟，也不调用 LLM：

```powershell
.venv\Scripts\python.exe scripts/check_saved_results.py --run "D:\models\Example1\runs\RUN" --temp-root "D:\temporary"
```

可额外指定 `--baseline-code "D:\SWMM-Agentic2"`，在同一临时结果副本上分别调用旧、新证据构建程序。脚本比较完整证据包，仅忽略降雨上下文的提取时间 `created_at`；不忽略模拟时刻、指标、事件或证据内容。它还检查原运行目录的全部文件哈希未变化。测试副本在结束时清理。这是严格一致性检查；与旧拓扑策略比较会报告预期差异，此次有意变更范围应使用上面的 `check_scope_upgrade.py`。

具体测试结果与当前验证边界见 `VALIDATION.md`。地表 GeoTIFF 导出为可选能力，需要另装 `rasterio`；缺少它时仍保留原程序的其他输出。

## 本次精简范围

未复制模型、降雨、历史结果、任务/聊天记录、开发分析记录、论文与演示材料、缓存、虚拟环境和 `.env`。未带入旧日志转 Markdown、演示模型生成、旧冒溢文件直接启动管线，以及已无正式入口的旧网页执行路由。正式降雨管线保留。

重构时保留了模拟公式、事件判据、证据精度规则和原诊断方法。2026-10-21另按上节约定调整了首轮拓扑范围及相应诊断提示，模拟公式、事件判据和证据精度不变。报告写作提示此前根据实际失败记录增强了机制状态与空数组约束，重试反馈会给出期望值和实际值，一致性校验规则保持不变。另修复了目录迁移相关导入与缓存路径，统一两种入口的模拟状态保存与降雨哈希传递，允许无密钥时查询固定结果，并让报告图片 MIME 类型不依赖 Windows 注册表。
