import asyncio
import json
import uuid
from pathlib import Path
from fastapi import HTTPException
from workflow_agents.interfaces.contracts import ChatRequest, ChatResponse
from workflow_agents.orchestration.session_state import WebWorkflow
from workflow_agents.orchestration.controller import run_orchestrator_turn, repair_mojibake, is_rate_limit_error, is_llm_timeout_error
from workflow_agents.common.paths import RUNTIME_DIR
WEB_LOG_DIR = RUNTIME_DIR / 'web_sessions'
CHAT_TIMEOUT_SECONDS = 900
class ChatSession:
    def __init__(self) -> None:
        self.transcript: list[dict[str, str]] = []
        self.lock = asyncio.Lock()
        self.workflow = WebWorkflow()
sessions: dict[str, ChatSession] = {}

async def chat(request: ChatRequest) -> ChatResponse:
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        restored = ChatSession()
        path = session_log_path(session_id)
        if path.exists():
            restored.transcript = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        flow_path = session_log_path(session_id).with_suffix('.state.json')
        if flow_path.exists():
            restored.workflow = WebWorkflow.from_dict(json.loads(flow_path.read_text(encoding='utf-8')))
        sessions[session_id] = restored
    session = sessions[session_id]
    user_message = repair_mojibake(request.message)

    async with session.lock:
        if request.flow_version is not None and request.flow_version != session.workflow.version:
            raise HTTPException(status_code=409, detail='流程状态已更新，本次未执行。请刷新页面后确认当前下一步。')
        session.transcript.append({"role": "user", "content": user_message})
        append_session_log(session_id, "user", user_message)
        try:
            reply = await asyncio.wait_for(
                run_orchestrator_turn(session.transcript, session.workflow,
                                      lambda: save_session_workflow(session_id, session.workflow)),
                timeout=CHAT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            reply = (
                "This request did not return within 15 minutes, so the web chat stopped waiting. "
                "If you started a long simulation, check the latest models/<model_name>/runs/ output folder. "
                "If this was only a normal question, the model service or a tool call likely hung; you can retry now."
            )
        except Exception as exc:
            if is_rate_limit_error(exc):
                reply = (
                    "模型服务当前繁忙，刚才这一轮没有成功执行，也没有产生可靠结果。"
                    "请稍等几十秒后直接重试同一句，或点击“新会话”后再试。"
                )
                session.transcript.append({"role": "assistant", "content": reply})
                append_session_log(session_id, "assistant", reply)
                save_session_workflow(session_id, session.workflow)
                return ChatResponse(session_id=session_id, reply=reply, transcript=session.transcript, workflow=session.workflow.to_dict())
            if is_llm_timeout_error(exc):
                reply = (
                    "模型服务本轮响应超时，刚才这一轮没有成功执行，也没有产生可靠结果。"
                    "这通常是 LLM 服务商响应慢、网络连接不稳定，或当前请求过长导致的。"
                    "请稍后重试；如果连续出现，可以换更快的模型或把任务拆小。"
                )
                session.transcript.append({"role": "assistant", "content": reply})
                append_session_log(session_id, "assistant", reply)
                save_session_workflow(session_id, session.workflow)
                return ChatResponse(session_id=session_id, reply=reply, transcript=session.transcript, workflow=session.workflow.to_dict())
            session.transcript.append({"role": "assistant", "content": f"Execution failed: {exc}"})
            append_session_log(session_id, "assistant", f"Execution failed: {exc}")
            save_session_workflow(session_id, session.workflow)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        session.transcript.append({"role": "assistant", "content": reply})
        append_session_log(session_id, "assistant", reply)
        save_session_workflow(session_id, session.workflow)
        return ChatResponse(session_id=session_id, reply=reply, transcript=session.transcript, workflow=session.workflow.to_dict())

async def get_session(session_id: str) -> ChatResponse:
    session = sessions.get(session_id)
    if session is not None:
        return ChatResponse(session_id=session_id, reply="", transcript=session.transcript, workflow=session.workflow.to_dict())

    log_path = session_log_path(session_id)
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="Session not found.")
    transcript = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        transcript.append({"role": record["role"], "content": record["content"]})
    path = log_path.with_suffix('.state.json')
    flow = WebWorkflow.from_dict(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else WebWorkflow()
    return ChatResponse(session_id=session_id, reply="", transcript=transcript, workflow=flow.to_dict())

async def delete_session(session_id: str) -> dict[str, bool]:
    sessions.pop(session_id, None)
    return {"ok": True}

def session_log_path(session_id: str) -> Path:
    safe_id = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")[:80]
    WEB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return WEB_LOG_DIR / f"{safe_id}.jsonl"

def save_session_workflow(session_id: str, flow: WebWorkflow) -> None:
    path = session_log_path(session_id).with_suffix('.state.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(flow.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)

def append_session_log(session_id: str, role: str, content: str) -> None:
    record = {"role": role, "content": content}
    with session_log_path(session_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
