"""Bounded user guidance owned by exactly one active conversation turn."""
from __future__ import annotations

import re

from .types import Message


class SteeringConflictError(ValueError):
    pass


class SteeringLimitError(ValueError):
    pass


def validate_identifier(value, name="turn_id"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", value):
        raise ValueError(f"{name} 必须为 16–64 位字母、数字、下划线或连字符")
    return value


class SteeringControl:
    def __init__(self, turn_id, max_chars, delivery):
        self.turn_id, self.max_chars, self.delivery = turn_id, max_chars, delivery
        self.entries = {}
        self.characters = 0
        self.closed = False

    @property
    def pending(self):
        return any(row["status"] == "queued" for row in self.entries.values())

    def add(self, text, *, turn_id, request_id):
        validate_identifier(turn_id)
        validate_identifier(request_id, "request_id")
        if self.closed or turn_id != self.turn_id:
            raise SteeringConflictError("此轮任务已结束或已切换，请重新发送消息")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("补充指令不能为空")
        text = text.strip()
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("补充指令必须为有效的 Unicode 文本") from None
        if len(text) > self.max_chars:
            raise ValueError(f"补充指令最多 {self.max_chars} 个字符")
        previous = self.entries.get(request_id)
        if previous is not None:
            if previous["message"] != text:
                raise SteeringConflictError("此补充指令编号已用于其他内容")
            return {**previous, "status": "queued"}
        if len(self.entries) >= 8 or self.characters + len(text) > self.max_chars:
            raise SteeringLimitError("本轮补充指令已达到条数或总字符上限")
        row = {"turn_id": turn_id, "request_id": request_id, "message": text,
               "status": "queued", "delivery": self.delivery}
        self.entries[request_id] = row
        self.characters += len(text)
        return row.copy()

    def consume(self, memory, session_id):
        rows = [row for row in self.entries.values() if row["status"] == "queued"]
        if rows:
            memory.append_messages(session_id, [Message("user", row["message"]) for row in rows])
            for row in rows:
                row["status"] = "applied"
        return bool(rows)

    def finish(self, memory, session_id):
        self.closed = True
        rows = [row for row in self.entries.values() if row["status"] == "queued"]
        if rows:
            memory.append_messages(session_id, [*[Message("user", row["message"]) for row in rows],
                Message("assistant", "以上补充指令未执行：本轮已停止或达到执行边界，请确认后重新发送。")])
            for row in rows:
                row["status"] = "unapplied"
        return [row.copy() for row in self.entries.values()]
