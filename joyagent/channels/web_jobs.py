"""Bounded in-memory receipts for Web turns running on the runtime event loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


class JobConflictError(ValueError):
    pass


class JobBusyError(ValueError):
    pass


def chat_error(exc: BaseException) -> tuple[int, str]:
    """Only fixed public errors enter job receipts, including local hosting."""
    from ..runtime import (
        ConversationArchivedError, ConversationBusyError, ConversationDeletedError,
        ConversationDeletionError, ConversationNotFoundError,
    )
    from ..steering import SteeringConflictError, SteeringLimitError
    from .web_tenants import TenantBusyError

    for kind, status, message in (
        (ConversationDeletedError, 410, "对话已永久删除，请新建对话"),
        (ConversationArchivedError, 409, "会话已归档，请先恢复后再发送消息"),
        (ConversationBusyError, 409, "会话正在执行，请等待本轮完成后再操作"),
        (ConversationNotFoundError, 404, "会话不存在，请刷新列表"),
        (ConversationDeletionError, 503, "对话删除清理未完成，请重试"),
        (SteeringConflictError, 409, "当前任务已结束或补充请求不匹配；请保留输入并重新发送"),
        (SteeringLimitError, 429, "本轮补充指令已达到数量或长度上限，请等待任务结束后继续"),
        (TenantBusyError, 429, "请求过于频繁或服务繁忙，请稍后重试"),
        (concurrent.futures.CancelledError, 503, "Agent 正在停止"),
        (asyncio.CancelledError, 503, "Agent 正在停止"),
        (ValueError, 400, "请求参数无效"),
    ):
        if isinstance(exc, kind):
            return status, message
    return 500, "Agent 执行失败，请稍后重试"


@dataclass
class _Job:
    fingerprint: str
    future: concurrent.futures.Future | None = None
    outcome: dict[str, Any] | None = None
    finished: float | None = None


class ChatJobs:
    """Do not own workers: futures use the bridge's already managed event loop."""

    def __init__(self, *, max_running: int = 8, per_owner: int = 4,
                 max_completed: int = 256, ttl: float = 600,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_running = max_running
        self.per_owner = per_owner
        self.max_completed = max_completed
        self.ttl = ttl
        self.clock = clock
        self._jobs: dict[tuple[str, str, str], _Job] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _prune(self) -> None:
        completed = sorted((job.finished, key) for key, job in self._jobs.items()
                           if job.finished is not None)
        now = self.clock()
        for index, (finished, key) in enumerate(completed):
            if now - finished >= self.ttl or index < len(completed) - self.max_completed:
                del self._jobs[key]

    def start(self, owner: str, session: str, turn: str, payload: dict[str, Any],
              submit: Callable[[], concurrent.futures.Future]) -> dict[str, Any]:
        key = owner, session, turn
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
        with self._lock:
            if self._closed:
                raise concurrent.futures.CancelledError()
            self._prune()
            old = self._jobs.get(key)
            if old is not None:
                if old.fingerprint != fingerprint:
                    raise JobConflictError("本轮编号已用于其他请求，请重新发送")
                return self._receipt(session, turn, old)
            active = [entry for entry, job in self._jobs.items() if job.outcome is None]
            if any(entry[:2] == key[:2] for entry in active):
                from ..runtime import ConversationBusyError
                raise ConversationBusyError("会话正在执行")
            if len(active) >= self.max_running or sum(entry[0] == owner for entry in active) >= self.per_owner:
                raise JobBusyError("请求过于频繁或服务繁忙，请稍后重试")
            job = _Job(fingerprint)
            self._jobs[key] = job
            try:
                job.future = submit()
            except BaseException:
                del self._jobs[key]
                raise
            # An already finished Future can invoke the callback immediately.
            job.future.add_done_callback(lambda future: self._finished(key, job, future))
            return self._receipt(session, turn, job)

    @staticmethod
    def _receipt(session: str, turn: str, job: _Job) -> dict[str, Any]:
        return {"session_id": session, "turn_id": turn,
                "status": job.outcome["status"] if job.outcome else "running"}

    def _finished(self, key: tuple[str, str, str], job: _Job,
                  future: concurrent.futures.Future) -> None:
        try:
            outcome = {"status": "completed", "result": future.result()}
        except BaseException as exc:
            status, message = chat_error(exc)
            outcome = {"status": "failed", "error": message, "status_code": status}
        with self._lock:
            if self._jobs.get(key) is job:
                job.outcome, job.finished = outcome, self.clock()
                # Do not retain callbacks/coroutines or their input after completion.
                job.future = None
                self._prune()

    def get(self, owner: str, session: str, turn: str) -> dict[str, Any] | None:
        with self._lock:
            self._prune()
            job = self._jobs.get((owner, session, turn))
            return dict(job.outcome or {"status": "running"}) if job is not None else None

    def active(self, owner: str, session: str) -> bool:
        with self._lock:
            return any(key[:2] == (owner, session) and job.outcome is None
                       for key, job in self._jobs.items())

    def forget(self, owner: str, session: str) -> None:
        with self._lock:
            for key in list(self._jobs):
                if key[:2] == (owner, session) and self._jobs[key].outcome is not None:
                    del self._jobs[key]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            futures = [job.future for job in self._jobs.values() if job.future is not None]
            self._jobs.clear()
        for future in futures:
            future.cancel()
