import json
import re
from pathlib import Path
from typing import Any

def _final_message_content(messages):
    for message in reversed(messages):
        content = getattr(message, "content", "")
        text = _stringify_content(content).strip()
        if text:
            return text
    return ""

def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_stringify_content(item) for item in content)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content)

def _is_unrelated_reply(text: str) -> bool:
    unrelated_markers = [
        "leetcode",
        "两数之和",
        "哈希表",
        "二叉搜索树",
        "监控二叉树",
        "rust的hashmap",
        "TreeNode",
        "class Solution",
        "vector<int>",
    ]
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in unrelated_markers)

def _parse_json_from_evidence(evidence: str) -> Any | None:
    try:
        return json.loads(evidence)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", evidence, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

def _normalize_evidence_block(evidence: str) -> tuple[str, str]:
    parsed = _parse_json_from_evidence(evidence)
    if parsed is not None:
        return json.dumps(parsed, ensure_ascii=False, indent=2), "json"
    return evidence.strip(), "text"

def _fallback_validation_explanation(evidence: str) -> str:
    parsed = _parse_json_from_evidence(evidence)
    if isinstance(parsed, list):
        return f"本次查询返回 {len(parsed)} 条记录；完整内容见下方原始结果。"
    if not isinstance(parsed, dict):
        return "本次结果来自固定验证器。由于解释层暂时不可用，下面保留原始验证结果作为正式依据。"

    validator = parsed.get("validator", "fixed validator")
    schema_name = parsed.get("schema_name", "SWMM-2D project layout")
    schema_version = parsed.get("schema_version", "unknown")
    if "projects" in parsed:
        ready_count = sum(1 for item in parsed["projects"] if item.get("ready"))
        return (
            f"本次检查使用固定验证器 `{validator}`，依据 `{schema_name}` "
            f"({schema_version}) 结构规范完成。共发现 {parsed.get('project_count', len(parsed['projects']))} "
            f"个模型项目，其中 {ready_count} 个项目处于 ready 状态。"
        )
    if "model_name" in parsed:
        status = "完整，未发现缺失项" if parsed.get("ready") else "存在问题"
        return (
            f"本次检查使用固定验证器 `{validator}`，依据 `{schema_name}` "
            f"({schema_version}) 结构规范完成。模型 `{parsed['model_name']}` 的检查结论为：{status}。"
        )
    return (
        f"本次结果来自固定验证器 `{validator}`，依据 `{schema_name}` "
        f"({schema_version}) 结构规范生成。"
    )

def _fallback_simulation_explanation(evidence: str) -> str:
    parsed = _parse_json_from_evidence(evidence)
    if not isinstance(parsed, dict):
        return "本次结果来自固定模拟工具。由于解释层暂时不可用，下面保留原始工具结果作为正式依据。"

    run_id = parsed.get("run_id", "")
    model_name = parsed.get("model_name", "")
    swmm = parsed.get("swmm", {}) or {}
    ca2d = parsed.get("ca2d", {}) or {}
    swmm_steps = swmm.get("total_steps", "未知")
    saved_steps = swmm.get("saved_steps", "未知")
    max_depth = ca2d.get("max_depth_m", "未知")
    run_root = parsed.get("run_root") or str(Path(parsed.get("swmm_output_dir", "")).parent)
    return (
        "本次执行已走 tool-first 固定模拟管线：降雨事件写入 SWMM，随后通过 PySWMM 导出节点溢流，"
        "再交给 CA2D 计算并保存到 runs 目录。"
        f"模型 `{model_name}` 的运行编号为 `{run_id}`，SWMM 总步数为 {swmm_steps}，"
        f"保存步数为 {saved_steps}，CA2D 最大水深为 {max_depth} m。"
        f"输出位置为 `{run_root}`。"
    )

def _contains_any(text: str, markers: list[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)

def _extract_agent_reply(messages) -> str:
    for message in reversed(messages):
        content = getattr(message, "content", "")
        text = _stringify_content(content).strip()
        if "Final Answer:" in text and not _is_unrelated_reply(text):
            return text.split("Final Answer:", 1)[1].strip()
    for message in reversed(messages):
        content = getattr(message, "content", "")
        text = _stringify_content(content).strip()
        if text and not _is_unrelated_reply(text):
            return text
    text = _final_message_content(messages)
    if text and not _is_unrelated_reply(text):
        return text
    return (
        "本轮没有得到可靠的 SWMM-2D 相关回复。请重新确认要执行的模型、降雨事件或下一步。"
    )
