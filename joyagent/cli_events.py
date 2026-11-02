"""Bounded public projections of external coding-agent JSONL events.

Only protocol stdout enters this parser. Stderr and reasoning events are never
answers. The process supervisor must additionally require exit status zero and a
successful model-broker outcome; a JSON terminal event alone is insufficient.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import math
import re
from typing import Any


_BACKENDS = {"codex", "pi", "deepseek"}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class CLIEvents:
    def __init__(self, backend: str, limit: int = 10000):
        if backend not in _BACKENDS:
            raise ValueError("unsupported CLI event backend")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("CLI event limit must be a positive integer")
        self.backend = backend
        self.limit = limit
        self.text = ""
        self.events: list[dict] = []
        self.completed = False
        self.failed = False
        self._event_chars = 0
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending: dict[str, str] = {}
        self._pi_stop: str | None = None
        self._deepseek_end = False
        self._deepseek_final = False

    def _fail(self) -> None:
        self.failed = True
        self.completed = False

    def _success(self) -> None:
        if not self.failed:
            self.completed = True

    def _once(self, category: str, identity: Any) -> bool:
        if not isinstance(identity, str) or not identity:
            return True
        key = category + ":" + identity[:256]
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > 256:
            self._seen.popitem(last=False)
        return True

    def _answer(self, value: Any, *, append: bool = False) -> None:
        if not isinstance(value, str):
            self._fail()
            return
        if append:
            separator = "\n" if self.text and value else ""
            remaining = self.limit - len(self.text)
            self.text += (separator + value)[:remaining]
        else:
            self.text = value[:self.limit]

    def _public_value(self, value: Any, remaining: list[int], depth: int = 0) -> Any:
        """Bound traversal as well as the final serialization, without references."""
        if remaining[0] <= 0 or depth > 6:
            return "…"
        if value is None or isinstance(value, bool):
            remaining[0] -= 5
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                encoded = _dump(value)
            except (ValueError, OverflowError):
                return "[invalid number]"
            remaining[0] -= len(encoded)
            return value if len(encoded) <= 128 and (not isinstance(value, float) or math.isfinite(value)) else "[large number]"
        if isinstance(value, str):
            answer = value[:max(0, remaining[0])]
            remaining[0] -= len(answer)
            return answer
        if isinstance(value, list):
            result = []
            for item in value[:64]:
                if remaining[0] <= 0:
                    break
                result.append(self._public_value(item, remaining, depth + 1))
                remaining[0] -= 1
            return result
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if remaining[0] <= 0 or len(result) >= 64:
                    break
                if not isinstance(key, str) or key in {"thinking", "reasoning", "reasoning_content"}:
                    continue
                if value.get("type") in {"thinking", "reasoning", "image"}:
                    return {"type": "omitted"}
                key = key[:128]
                remaining[0] -= len(key) + 4
                result[key] = self._public_value(item, remaining, depth + 1)
            return result
        return "[unsupported value]"

    def _tool(self, name: Any, ok: bool, result: Any, *, identity: Any = None) -> None:
        if len(self.events) >= 64 or not self._once("tool", identity):
            return
        remaining = self.limit - self._event_chars
        if remaining <= 0:
            return
        name = name if isinstance(name, str) and name else "tool"
        name = (self.backend + "_" + re.sub(r"[^A-Za-z0-9_]", "_", name))[:64]
        projected = self._public_value(result, [remaining])
        event = {"name": name, "ok": ok, "result": {"ok": ok, "result": projected}}
        encoded = _dump(event)
        if len(encoded) > remaining:
            text = _dump(projected)
            event["result"]["result"] = {"text": "", "truncated": True}
            if len(_dump(event)) > remaining:
                return
            low, high = 0, min(len(text), remaining)
            while low < high:
                middle = (low + high + 1) // 2
                event["result"]["result"]["text"] = text[:middle]
                if len(_dump(event)) <= remaining:
                    low = middle
                else:
                    high = middle - 1
            event["result"]["result"]["text"] = text[:low]
            encoded = _dump(event)
        self._event_chars += len(encoded)
        self.events.append(event)

    def feed(self, event: dict) -> None:
        """Consume one decoded stdout record; malformed known events fail closed."""
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self._fail()
            return
        try:
            getattr(self, "_" + self.backend)(event)
        except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
            self._fail()

    def _codex(self, event: dict) -> None:
        kind = event["type"]
        if kind in {"turn.failed", "error"}:
            self._fail()
        elif kind == "turn.started":
            self.completed = False
        elif kind == "turn.completed":
            self._success()
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                self._fail()
                return
            item_type = item.get("type")
            if item_type == "agent_message":
                self._answer(item.get("text"))
            elif item_type == "error":
                message = item.get("message", "")
                if not (isinstance(message, str) and message.startswith("Model metadata for ")
                        and "not found. Defaulting to fallback metadata" in message):
                    self._fail()
            elif item_type == "command_execution":
                exit_code = item.get("exit_code")
                ok = (item.get("status") == "completed" and type(exit_code) is int and exit_code == 0)
                self._tool("command_execution", ok, {
                    "command": item.get("command", ""), "stdout": item.get("aggregated_output", ""),
                    "exit_code": exit_code,
                }, identity=item.get("id"))
            elif item_type == "mcp_tool_call":
                result = item.get("result")
                ok = (item.get("status") == "completed" and not item.get("error")
                      and not (isinstance(result, dict) and result.get("isError")))
                self._tool("mcp_" + str(item.get("server", ""))[:64] + "_" + str(item.get("tool", "tool"))[:64],
                           ok, result if ok else item.get("error") or result, identity=item.get("id"))
            elif item_type in {"file_change", "web_search", "collab_tool_call", "dynamic_tool_call"}:
                ok = item.get("status") == "completed" and item.get("success") is not False
                result = {key: item[key] for key in ("changes", "query", "result", "error", "content_items")
                          if key in item}
                self._tool(item_type, ok, result, identity=item.get("id"))

    def _pi(self, event: dict) -> None:
        kind = event["type"]
        if kind == "message_end":
            message = event.get("message")
            if not isinstance(message, dict):
                self._fail()
                return
            if message.get("role") != "assistant":
                return
            content = message.get("content")
            if not isinstance(content, list):
                self._fail()
                return
            pieces = []
            remaining = self.limit
            for block in content:
                if not isinstance(block, dict):
                    self._fail()
                    return
                if block.get("type") == "text":
                    text = block.get("text")
                    if not isinstance(text, str):
                        self._fail()
                        return
                    pieces.append(text[:remaining])
                    remaining -= len(pieces[-1])
            self._answer("".join(pieces))
            self._pi_stop = message.get("stopReason")
            if self._pi_stop not in {"stop", "toolUse"}:
                self._fail()
        elif kind == "tool_execution_end":
            ok = event.get("isError") is False
            self._tool(event.get("toolName"), ok, event.get("result"), identity=event.get("toolCallId"))
        elif kind == "agent_settled":
            if event.get("aborted") is False and self._pi_stop == "stop":
                self._success()
            else:
                self._fail()
        elif kind == "agent_start":
            self.completed = False
        elif kind == "auto_retry_end" and event.get("success") is False:
            self._fail()

    def _deepseek(self, event: dict) -> None:
        kind = event["type"]
        if kind == "error":
            self._fail()
        elif kind == "tool_call":
            call_id, name = event.get("callId"), event.get("tool")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                self._fail()
            elif len(call_id) > 256 or len(self._pending) >= 64 or call_id in self._pending:
                self._fail()
            else:
                self._pending[call_id] = name[:128]
        elif kind == "tool_result":
            call_id = event.get("callId")
            name = self._pending.pop(call_id, None)
            if name is None or event.get("status") not in {"completed", "error"}:
                self._fail()
            else:
                self._tool(name, event["status"] == "completed", event.get("result"), identity=call_id)
        elif kind == "status" and event.get("phase") == "turn_end":
            reason = event.get("reason")
            if isinstance(reason, dict) and reason.get("kind") == "completed":
                self._deepseek_end = True
            else:
                self._fail()
        elif kind == "status" and event.get("phase") == "turn_start":
            self.completed = self._deepseek_end = self._deepseek_final = False
        elif kind == "final":
            self._answer(event.get("text"))
            self._deepseek_final = True
        if self._deepseek_end and self._deepseek_final:
            if self._pending:
                self._fail()
            else:
                self._success()
