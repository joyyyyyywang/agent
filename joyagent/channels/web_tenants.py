"""Isolated per-browser runtimes for the anonymous shared Web channel."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections import OrderedDict
from copy import deepcopy
import inspect
import os
from pathlib import Path
import re
import stat
import threading
import uuid
from dataclasses import asdict
from typing import Any

from .common import validate_input
from .web_history import project_history
from ..config import Config
from ..decisions import DecisionClient, decision_settings
from ..execution import execution_settings, validate_shared_mcp
from ..memory import MemoryStore
from ..native_cleanup import cleanup_tasks
from ..runtime import Runtime, build_runtime
from ..routing import validate_routing
from ..steering import SteeringConflictError, validate_identifier
from ..types import AgentResult


class TenantBusyError(ValueError):
    """The caller must retry later; no additional turn has been queued."""


def _owned_directory(path: Path) -> None:
    """Create an owner-only directory without accepting a substituted symlink."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("共享 Web 的用户目录不能是符号链接或普通文件")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("共享 Web 的用户目录必须属于服务用户，且权限必须为 700")


class SharedRuntimeLoop:
    """Own one event loop and an idle-evicted cache of isolated visitor runtimes.

    All SQLite/tool operations are scheduled on the same loop. Limits are checked
    before the first await so concurrent sessions cannot accumulate unbounded
    waiting turns, including requests queued for the same native Agent process.
    The filesystem boundary assumes the service's OS account is trusted; Web
    users can only modify files inside their individual workspace.
    """

    def __init__(self, config: Config, *, max_runtimes: int = 32,
                 max_concurrent_turns: int = 8,
                 max_concurrent_turns_per_user: int = 4) -> None:
        decision = decision_settings(config)
        if decision:
            DecisionClient(decision).credential_source()
        validate_routing(config)
        config.select_model_choice()
        config.select_backend()
        for value, maximum in ((max_runtimes, 32), (max_concurrent_turns, 8),
                               (max_concurrent_turns_per_user, 8)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"共享 Web 资源限制必须为 1–{maximum} 的整数")
        execution_settings(config)
        validate_shared_mcp(config)

        self.config = config
        self.max_runtimes = max_runtimes
        self.max_concurrent_turns = max_concurrent_turns
        self.max_concurrent_turns_per_user = min(max_concurrent_turns_per_user, max_concurrent_turns)
        value = config.get("agent", "data_dir", "data")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("共享 Web data_dir 必须是非空目录")
        configured = Path(os.path.expandvars(value)).expanduser()
        if not configured.is_absolute():
            configured = config.root / configured
        # Resolve trusted ancestors, but inspect the configured state directory
        # itself before following a possible symlink.
        self.data_root = configured.parent.resolve() / configured.name
        if self.data_root.resolve().is_relative_to(config.workspace):
            raise ValueError("共享 Web 数据目录不能放在文件工具的 workspace 内")
        if not self.data_root.parent.is_dir():
            raise ValueError("共享 Web data_dir 的父目录必须由管理员预先创建")
        _owned_directory(self.data_root)
        self.users_root = self.data_root / "users"
        _owned_directory(self.users_root)

        self._runtimes: OrderedDict[str, Runtime] = OrderedDict()
        self._leases: dict[str, int] = {}
        self._active_turns: set[tuple[str, str]] = set()
        self._active_users: dict[str, int] = {}
        self._runtime_lock = asyncio.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True,
                                       name="joyagent-shared-web")
        self.thread.start()

    def submit(self, coroutine: Any) -> concurrent.futures.Future:
        if threading.current_thread() is self.thread:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise RuntimeError("不能在共享 Web 事件循环中同步等待自身")
        with self._lifecycle_lock:
            if self._closing or self._closed:
                if inspect.iscoroutine(coroutine):
                    coroutine.close()
                raise RuntimeError("共享 Web 正在停止")
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            except BaseException:
                if inspect.iscoroutine(coroutine):
                    coroutine.close()
                raise
        return future

    def call(self, coroutine: Any) -> Any:
        return self.submit(coroutine).result()

    def _validate(self, user_id: str, session: str, text: str) -> tuple[str, str]:
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("共享 Web 请求必须通过 call 在所属事件循环中执行")
        if self._closing:
            raise RuntimeError("共享 Web 正在停止")
        if not isinstance(user_id, str) or not re.fullmatch(r"[0-9a-f]{32}", user_id):
            raise ValueError("无效的共享 Web 用户标识")
        text, session = validate_input(text, session)
        if len(session) > 124:
            raise ValueError("共享 Web 会话名不能超过 124 个字符")
        return text, f"web:{session}"

    def _tenant_config(self, user_id: str) -> Config:
        # Recheck the entire private subtree on each request, including cached
        # runtimes. User-controlled workspaces must never become state parents.
        _owned_directory(self.data_root)
        _owned_directory(self.users_root)
        tenant = self.users_root / user_id
        _owned_directory(tenant)
        data_dir, workspace = tenant / "data", tenant / "workspace"
        _owned_directory(data_dir)
        _owned_directory(workspace)
        for entry in data_dir.iterdir():
            if entry.name == "native_sessions":
                _owned_directory(entry)
                continue
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError("共享 Web 用户状态只能包含服务用户拥有的普通文件")
        data = deepcopy(self.config.data)
        data.setdefault("agent", {}).update({
            "data_dir": str(data_dir), "workspace": str(workspace),
            "soul": str(self.config.soul_path), "policy": str(self.config.policy_path),
            "skills_dir": str(self.config.skills_dir),
        })
        data["model"] = self.config.model_settings
        # Direct callers of this manager must receive shared-mode error
        # redaction even when their base configuration omitted the Web table.
        data.setdefault("web", {})["mode"] = "shared"
        # Only explicitly shared sandboxed servers get a fresh tenant process.
        data["mcp"] = {"servers": {name: deepcopy(settings) for name, settings
                                  in self.config.data.get("mcp", {}).get("servers", {}).items()
                                  if settings.get("enabled", False) and settings.get("shared") is True
                                  and settings.get("sandbox") is True}}
        return Config(self.config.path, data)

    async def _acquire(self, user_id: str) -> Runtime:
        async with self._runtime_lock:
            config = self._tenant_config(user_id)
            if user_id in self._runtimes:
                runtime = self._runtimes[user_id]
                self._runtimes.move_to_end(user_id)
            else:
                if len(self._runtimes) >= self.max_runtimes:
                    victim = next((key for key in self._runtimes if not self._leases.get(key)), None)
                    if victim is None:
                        raise TenantBusyError("所有用户运行时正在使用，请稍后重试")
                    previous = self._runtimes.pop(victim)
                    await previous.close()
                runtime = await build_runtime(config)
                self._runtimes[user_id] = runtime
            self._leases[user_id] = self._leases.get(user_id, 0) + 1
            return runtime

    def _release(self, user_id: str) -> None:
        count = self._leases[user_id] - 1
        if count:
            self._leases[user_id] = count
        else:
            del self._leases[user_id]

    async def history(self, user_id: str, session: str) -> list[dict[str, Any]]:
        return (await self.conversation_history(user_id, session))["messages"]

    async def conversation_history(self, user_id: str, session: str) -> dict[str, Any]:
        _, session_id = self._validate(user_id, session, "history")
        runtime = await self._acquire(user_id)
        try:
            runtime.check_conversation_not_deleted(session_id)
            return {"messages": project_history(runtime.memory.history(session_id, limit=80),
                                                 model_choices=self.config.model_choices),
                    "conversation": runtime.memory.get_conversation(session_id)}
        finally:
            self._release(user_id)

    async def list_conversations(self, user_id: str, archived: bool = False) -> list[dict[str, Any]]:
        self._validate(user_id, "default", "conversations")
        if type(archived) is not bool:
            raise ValueError("archived 必须是布尔值")
        runtime = await self._acquire(user_id)
        try:
            return runtime.memory.list_conversations(prefix="web:", archived=archived, limit=1000)
        finally:
            self._release(user_id)

    async def create_conversation(self, user_id: str) -> dict[str, Any]:
        self._validate(user_id, "default", "conversations")
        runtime = await self._acquire(user_id)
        try:
            return runtime.memory.create_conversation("web:chat-" + uuid.uuid4().hex)
        finally:
            self._release(user_id)

    async def rename_conversation(self, user_id: str, session: str, title: str) -> dict[str, Any]:
        _, session_id = self._validate(user_id, session, "rename")
        runtime = await self._acquire(user_id)
        try:
            return runtime.rename_conversation(session_id, title)
        finally:
            self._release(user_id)

    async def set_conversation_archived(self, user_id: str, session: str,
                                        archived: bool) -> dict[str, Any]:
        _, session_id = self._validate(user_id, session, "archive")
        if type(archived) is not bool:
            raise ValueError("archived 必须是布尔值")
        runtime = await self._acquire(user_id)
        try:
            return runtime.set_conversation_archived(session_id, archived)
        finally:
            self._release(user_id)

    async def delete_conversation(self, user_id: str, session: str) -> dict[str, Any]:
        _, session_id = self._validate(user_id, session, "delete")
        runtime = await self._acquire(user_id)
        try:
            return await runtime.delete_conversation_async(session_id)
        finally:
            self._release(user_id)

    async def tool_catalog(self, user_id: str) -> list[dict[str, Any]]:
        self._validate(user_id, "default", "tools")
        runtime = await self._acquire(user_id)
        try:
            return [asdict(spec) for spec in runtime.agent.tools.specs()]
        finally:
            self._release(user_id)

    async def backend_catalog(self, user_id: str) -> list[dict[str, Any]]:
        self._validate(user_id, "default", "backends")
        runtime = await self._acquire(user_id)
        try:
            return runtime.backend_catalog()
        finally:
            self._release(user_id)

    async def agent_status(self, user_id: str) -> dict[str, Any]:
        """Observe this visitor without warming or extending a runtime's LRU.

        A dashboard poll must not launch an MCP/CLI, create state directories,
        evict another visitor or wait for the active turn's backend lock.
        This coroutine has no await, so an existing runtime cannot be evicted
        between lookup and its synchronous snapshot on this event loop.
        """
        from ..runtime import agent_status_snapshot

        self._validate(user_id, "default", "agent-status")
        runtime = self._runtimes.get(user_id)
        tenant = self.users_root / user_id
        for path in (self.data_root, self.users_root, tenant, tenant / "data", tenant / "workspace"):
            try:
                info = path.lstat()
            except FileNotFoundError:
                if runtime is not None or path in (self.data_root, self.users_root):
                    raise ValueError("共享 Web 用户状态目录不可用") from None
                # Unused visitors have no directory or database yet.
                continue
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError("共享 Web 用户状态目录必须为服务用户拥有的 0700 普通目录")
        if runtime is not None:
            return runtime.agent_status()
        for name in ("memory.sqlite3-wal", "memory.sqlite3-shm"):
            try:
                info = (tenant / "data" / name).lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise ValueError("共享 Web 数据库附属文件必须为服务用户拥有的普通文件")
        stats = MemoryStore.read_conversation_stats(tenant / "data", prefix="web:")
        return agent_status_snapshot(self.config, stats)

    async def steer(self, user_id: str, session: str, text: str, *,
                    turn_id: str, request_id: str) -> dict[str, Any]:
        text, session_id = self._validate(user_id, session, text)
        validate_identifier(turn_id)
        validate_identifier(request_id, "request_id")
        # A supplement belongs only to an existing active visitor turn. It must
        # not create a runtime, acquire another slot, or refresh cache/LRU state.
        runtime = self._runtimes.get(user_id)
        if (user_id, session_id) not in self._active_turns or runtime is None:
            raise SteeringConflictError("当前任务已结束或不存在")
        return runtime.steer(text, session_id, turn_id=turn_id, request_id=request_id)

    async def chat(self, user_id: str, session: str, text: str,
                   model: str | None = None, backend: str | None = None,
                   turn_id: str | None = None) -> AgentResult:
        text, session_id = self._validate(user_id, session, text)
        turn_options = {"turn_id": validate_identifier(turn_id)} if turn_id is not None else {}
        self.config.select_model_choice(model)
        self.config.select_backend(backend)
        turn = (user_id, session_id)
        if turn in self._active_turns:
            raise TenantBusyError("当前会话已有请求正在执行，请等待本轮完成")
        count = self._active_users.get(user_id, 0)
        if count >= self.max_concurrent_turns_per_user:
            raise TenantBusyError(f"当前访客已有 {self.max_concurrent_turns_per_user} 个会话正在处理，请等待其中一个完成")
        if len(self._active_turns) >= self.max_concurrent_turns:
            raise TenantBusyError("共享 Web 当前请求较多，请稍后重试")
        self._active_turns.add(turn)
        self._active_users[user_id] = count + 1
        try:
            runtime = await self._acquire(user_id)
            try:
                return await runtime.chat(text, session_id=session_id, model=model, backend=backend, **turn_options)
            finally:
                self._release(user_id)
        finally:
            self._active_turns.remove(turn)
            remaining = self._active_users[user_id] - 1
            if remaining:
                self._active_users[user_id] = remaining
            else:
                del self._active_users[user_id]

    def close(self) -> None:
        if threading.current_thread() is self.thread:
            raise RuntimeError("请在共享 Web 事件循环外关闭服务")
        with self._lifecycle_lock:
            if self._closed or self._closing:
                return
            self._closing = True

        async def shutdown() -> None:
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            protected = cleanup_tasks()
            for task in pending:
                if task not in protected:
                    task.cancel()
            # Cleanup may belong to a worker already removed from its pool.
            # Await it with normal requests so every visitor is drained before
            # closing runtime state and stopping this event loop.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            failures = []
            while self._runtimes:
                _, runtime = self._runtimes.popitem(last=False)
                try:
                    await runtime.close()
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise failures[0]

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            self.loop.close()
            with self._lifecycle_lock:
                self._closed = True
