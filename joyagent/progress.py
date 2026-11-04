"""Ephemeral, provider-neutral execution milestones, never model reasoning."""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import threading


_MESSAGES = {
    "accepted": "请求已接收，等待开始处理",
    "preparing": "正在准备会话和上下文",
    "routing": "正在选择适合本轮任务的模型",
    "agent": "正在准备 Agent，繁忙时需等待前序任务",
    "model": "正在请求模型，等待响应",
    "tools": "正在执行工具",
    "tool_done": "一项工具执行完成",
    "tool_failed": "一项工具未完成，正在处理结果",
    "organizing": "正在整理结果",
    "completed": "本轮处理完成",
    "stopped": "本轮已暂停，请查看回复中的原因",
    "failed": "本轮执行已结束，请核对结果或错误提示",
}
_current: ContextVar = ContextVar("joyagent_progress", default=None)


class TurnProgress:
    """Callable fixed-vocabulary sink; safe to capture across worker threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events = deque(maxlen=5)
        self._sequence = 0
        self._status = "running"
        self("accepted")

    def __call__(self, phase: str) -> None:
        if type(phase) is not str or phase not in _MESSAGES:
            return
        with self._lock:
            if self._status != "running":
                return
            self._sequence += 1
            self._events.append({"sequence": self._sequence, "phase": phase, "text": _MESSAGES[phase]})
            if phase in {"completed", "stopped", "failed"}:
                self._status = phase

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": self._status, "events": [dict(event) for event in self._events]}


def capture_progress():
    return _current.get()


def report_progress(phase: str) -> None:
    sink = _current.get()
    if sink is not None:
        sink(phase)


@contextmanager
def progress_scope(sink):
    token = _current.set(sink)
    try:
        yield
    finally:
        _current.reset(token)
