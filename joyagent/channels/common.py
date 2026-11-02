"""Small, shared channel boundaries and durable cursors."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

MAX_MESSAGE = 12_000
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def validate_input(text: Any, session_id: Any = "default") -> tuple[str, str]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("消息不能为空")
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"消息不能超过 {MAX_MESSAGE} 个字符")
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        raise ValueError("会话名须为 1–128 位字母、数字、点、冒号、下划线或连字符")
    return text, session_id


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"通道游标文件不可读：{path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"通道游标格式不正确：{path}")
    return value


def write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cursor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def wait_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
