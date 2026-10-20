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

可额外指定 `--baseline-code "D:\SWMM-Agentic2"`，在同一临时结果副本上分别调用旧、新证据构建程序。脚本比较完整证据包，仅忽略降雨上下文的提取时间 `created_at`；不忽略模拟时刻、指标、事件或证据内容。它还检查原运行目录的全部文件哈希未变化。测试副本在结束时清理。

具体测试结果与当前验证边界见 `VALIDATION.md`。地表 GeoTIFF 导出为可选能力，需要另装 `rasterio`；缺少它时仍保留原程序的其他输出。

## 本次精简范围

未复制模型、降雨、历史结果、任务/聊天记录、开发分析记录、论文与演示材料、缓存、虚拟环境和 `.env`。未带入旧日志转 Markdown、演示模型生成、旧冒溢文件直接启动管线，以及已无正式入口的旧网页执行路由。正式降雨管线保留。

模拟公式、事件判据、证据精度规则和诊断/报告提示内容保持原有方法。另修复了目录迁移相关导入与缓存路径，统一两种入口的模拟状态保存与降雨哈希传递，允许无密钥时查询固定结果，并让报告图片 MIME 类型不依赖 Windows 注册表。
