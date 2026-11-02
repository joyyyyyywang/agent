"""Browser history projection: visible conversation and safe tool outcomes."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from ..types import Message


_ERRORS = {
    "permission_denied": "工具未获允许，请选择当前可用的工具。",
    "unknown_tool": "工具不存在，请刷新工具列表。",
    "invalid_arguments": "工具参数无效，请检查参数后重试。",
    "timeout": "工具执行超时，外部操作结果需要核实。",
    "cancelled": "工具执行已中止，外部操作结果需要核实。",
    "superseded": "已收到补充指令，此工具尚未执行。",
    "budget_exceeded": "已达到本轮工具调用上限。",
    "tool_error": "工具执行失败，请检查参数或稍后重试。",
    "execution_failed": "命令或脚本未成功完成，请查看退出状态和输出。",
}


def project_routing(value: Any, model_choices: Iterable[str]) -> dict[str, Any]:
    """Expose only known routing outcomes; never reflect stored service details."""
    if (not isinstance(value, dict) or value.get("mode") != "auto"
            or value.get("category") not in ("simple", "complex")
            or value.get("reason") not in ("classified", "low_confidence", "decision_failed")
            or not isinstance(value.get("model"), str) or value["model"] not in model_choices
            or value.get("decision_model") != "decision-model-preview"):
        return {}
    result = {key: value[key] for key in ("mode", "category", "model", "reason", "decision_model")}
    probability = value.get("simple_probability")
    if type(probability) in (int, float) and 0 <= probability <= 1 and math.isfinite(probability):
        result["simple_probability"] = probability
    return result


def _backend_events(message: Message) -> list[dict[str, Any]]:
    """Project only the neutral, bounded CLI outcomes saved on the final reply."""
    # Retired OpenCode replies remain readable without any executable adapter.
    if message.backend not in {"codex", "opencode", "pi", "deepseek"}:
        return []
    candidates = []

    def encoded(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    for event in message.tool_events[:64]:
        if not isinstance(event, dict):
            continue
        name, ok, output = event.get("name"), event.get("ok"), event.get("result")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name)
                or not name.startswith(message.backend + "_") or type(ok) is not bool
                or not isinstance(output, dict) or output.get("ok") is not ok):
            continue
        safe = {"name": name, "ok": ok, "result": {"ok": ok, "result": output.get("result")}}
        try:
            serialized = encoded(safe)
        except (TypeError, ValueError, RecursionError):
            continue
        preview = {"name": name, "ok": ok, "result": {
            "ok": ok, "result": {"text": "", "truncated": True}}}
        minimum = min(len(serialized), len(encoded(preview)))
        candidates.append((safe, serialized, preview, minimum))

    # Reserve each later event a fair share before spending space on an earlier
    # large result. Small records remain whole; all 64 maximum-length names and
    # statuses fit. Include the JSON array's own delimiters in the budget.
    remaining = 10000 - 2 - max(0, len(candidates) - 1)
    share = remaining // max(1, len(candidates))
    reservations = [min(len(serialized), max(minimum, share))
                    for _, serialized, _, minimum in candidates]
    reserved = sum(reservations)
    projected = []
    for (safe, serialized, preview, _), reservation in zip(candidates, reservations):
        reserved -= reservation
        available = remaining - reserved
        if len(serialized) > available:
            text = encoded(safe["result"]["result"])
            low, high = 0, min(len(text), available)
            while low < high:
                middle = (low + high + 1) // 2
                preview["result"]["result"]["text"] = text[:middle]
                if len(encoded(preview)) <= available:
                    low = middle
                else:
                    high = middle - 1
            preview["result"]["result"]["text"] = text[:low]
            safe, serialized = preview, encoded(preview)
        remaining -= len(serialized)
        projected.append(safe)
    return projected


def _event(message: Message, name: str) -> dict[str, Any]:
    """Never replay stored exception text, third-party errors or protocol data."""
    try:
        output = json.loads(message.content)
    except (TypeError, ValueError):
        output = None
    if isinstance(output, dict) and output.get("ok") is True:
        result = {"ok": True, "result": output.get("result")}
    else:
        code = output.get("code") if isinstance(output, dict) else None
        if not isinstance(code, str) or code not in _ERRORS:
            code = "tool_error"
        result = {"ok": False, "code": code, "error": _ERRORS[code]}
        if code == "execution_failed" and isinstance(output.get("result"), dict):
            details = output["result"]
            safe: dict[str, Any] = {}
            for key in ("stdout", "stderr", "cwd"):
                if isinstance(details.get(key), str):
                    safe[key] = details[key]
            for key in ("timed_out", "truncated"):
                if type(details.get(key)) is bool:
                    safe[key] = details[key]
            if "exit_code" in details and (details["exit_code"] is None or type(details["exit_code"]) is int):
                safe["exit_code"] = details["exit_code"]
            result["result"] = safe
    # Durations are not persisted, so do not manufacture them on refresh.
    return {"name": name, "ok": result["ok"], "result": result}


def project_history(messages: Iterable[Message], *, model_choices: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Attach each completed tool result to its turn's visible final reply."""
    visible: list[dict[str, Any]] = []
    calls: dict[str, str] = {}
    events: list[dict[str, Any]] = []

    def flush() -> None:
        if events:
            visible.append({"role": "assistant", "content": "工具执行记录", "tool_events": list(events)})
            events.clear()
        calls.clear()

    for message in messages:
        if message.role == "user":
            flush()
            if message.content:
                visible.append({"role": "user", "content": message.content})
        elif message.role == "assistant":
            if message.tool_calls:
                for call in message.tool_calls:
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", call.name):
                        calls[call.id] = call.name
                continue
            if message.content:
                row: dict[str, Any] = {"role": "assistant", "content": message.content}
                routing = project_routing(getattr(message, "routing", None), model_choices)
                if routing:
                    row["routing"] = routing
                if isinstance(message.backend, str) and message.backend in {"codex", "opencode", "pi", "deepseek"}:
                    row["backend"] = message.backend
                    events.extend(_backend_events(message))
                if events:
                    row["tool_events"] = list(events)
                    events.clear()
                visible.append(row)
                calls.clear()
        elif message.role == "tool":
            name = calls.pop(message.tool_call_id, None)
            # A truncated or malformed history window must not invent a call.
            if name:
                events.append(_event(message, name))
    flush()
    return visible
