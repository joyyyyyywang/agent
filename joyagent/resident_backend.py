"""One resident native process per visitor/workspace/backend, with many sessions.

Native protocol state is a disposable cache. SQLite remains authoritative. Normal
turns keep the native process and thread; failures or capacity eviction rebuild
from bounded JoyAgent history, without replaying completed actions automatically.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import tempfile
import uuid
import weakref

from .cli_backend import ExternalProvider, transcript
from .cli_profiles import build_resident_profile
from .cli_host import LoopbackBridge
from .model_broker import ModelBroker, MAX_REPLAY_BYTES, MAX_REPLAY_ITEMS
from .native_cleanup import create_cleanup_task
from .process_guard import ProcessGuardError, watch_process
from .types import ModelReply, shared_provider_message
from .agent_status import native_host_pids


_POOLS = weakref.WeakKeyDictionary()
MAX_NATIVE_SESSIONS = 64
IDLE_RESTART_DELAYS = (1, 5, 15)


@dataclass
class NativeSession:
    native_session_id: str
    seen_count: int = 0
    identity: tuple | None = None
    reasoning: bool = False
    # Absolute ordinal of the first retained SQLite message, not its contents.
    # Native history must be rebuilt when the selected contiguous tail slides.
    context_start: int | None = None


def _session_key(session_id):
    return hashlib.sha256(session_id.encode()).hexdigest()


def resident_limit(config):
    value = config.get("backends", "resident_limit", 8)
    if type(value) is not int or not 1 <= value <= 32:
        raise ValueError("backends.resident_limit 必须为 1–32 的整数")
    return value


async def _finish(awaitable):
    task = create_cleanup_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


def _private(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Agent 会话目录必须为服务所有者的 0700 私有目录")


def _remove(path):
    if path.is_symlink():
        raise ValueError("Agent 会话目录不能是符号链接")
    if path.exists():
        shutil.rmtree(path)


class _Pool:
    def __init__(self, limit):
        self.limit = limit
        self.workers = OrderedDict()
        self.lock = asyncio.Lock()


class ResidentManager:
    def __init__(self, config, workspace, policy, memory):
        self.config, self.workspace, self.policy, self.memory = config, workspace, policy, memory
        self.limit = resident_limit(config)
        self.root = config.data_dir / "native_sessions"
        self.state_lock = None
        self._pool = None
        self.closed = False
        self._turns = set()
        self._backend_locks = {}
        self._recoveries = {}
        self._restart_attempts = {}

    def _registry(self):
        if self._pool is None:
            loop = asyncio.get_running_loop()
            self._pool = _POOLS.setdefault(loop, _Pool(self.limit))
            self._pool.limit = min(self._pool.limit, self.limit)
        return self._pool

    def _state(self):
        if self.state_lock is not None:
            return
        _private(self.root)
        descriptor = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ValueError("Agent 会话锁无效")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # A prior service crash can leave native transcripts. No live owner
            # holds this lock now, so remove only our named disposable caches.
            for child in self.root.iterdir():
                if re.fullmatch(r"[0-9a-f]{64}", child.name):
                    _remove(child)
            self.state_lock = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _session_path(self, session_id):
        # Kept for removing the previous release's per-conversation cache layout.
        return self.root / _session_key(session_id)

    def _process_path(self, backend):
        return self.root / _session_key("worker:" + backend)

    def _backend_lock(self, backend):
        return self._backend_locks.setdefault(backend, asyncio.Lock())

    def provider(self, backend, base_provider, session_id):
        if self.closed:
            raise RuntimeError("Agent 运行时已关闭")
        return ResidentProvider(self, backend, base_provider, session_id)

    def has_session(self, session_id):
        return self._pool is not None and any(
            key[0] is self and session_id in worker.sessions for key, worker in self._pool.workers.items())

    def snapshot(self):
        """Read only this manager's existing workers; do not create/touch LRU."""
        if self._pool is None or self.closed:
            return {}
        rows, requests = {}, {}
        for (owner, backend), worker in self._pool.workers.items():
            if owner is not self:
                continue
            process = worker.process
            alive = process is not None and process.returncode is None
            manager_pid = process.pid if alive and type(process.pid) is int and process.pid > 0 else None
            row = {"status": "starting", "pid": None, "manager_pid": manager_pid,
                   "resident_session_count": 0, "reason": "", "running": alive}
            if (getattr(worker, "_termination_requested", False) or getattr(worker, "_close_task", None) is not None
                    or (process is not None and not alive) or any(task.done() for task in worker.tasks)):
                reason = getattr(worker, "failure_reason", "") or "进程已退出或连接异常。"
                recovering = self._recoveries.get(backend)
                if recovering is not None and not recovering.done():
                    row.update(status="starting", reason=reason + " 正在自动重启。")
                else:
                    exhausted = self._restart_attempts.get(backend, 0) >= len(IDLE_RESTART_DELAYS)
                    note = " 已达自动重启上限；下次请求会重新启动。" if exhausted else " 下次请求会重新启动。"
                    row.update(status="error", reason=reason + note)
            elif alive and worker.native_pid is not None:
                row.update(status="busy" if worker.busy else "idle",
                           resident_session_count=sum(key.startswith("web:") for key in worker.sessions))
                requests[backend] = (manager_pid, worker.native_pid)
            rows[backend] = row
        for backend, pid in native_host_pids(requests).items():
            rows[backend]["pid"] = pid
        return rows

    def worker_failed(self, worker):
        """Recover an already-started idle worker; never retry a user turn."""
        backend = worker.backend
        pending = self._recoveries.get(backend)
        if (self.closed or self._pool is None or worker.busy
                or getattr(worker, "_close_task", None) is not None
                or self._pool.workers.get((self, backend)) is not worker
                or pending is not None and not pending.done()
                or self._restart_attempts.get(backend, 0) >= len(IDLE_RESTART_DELAYS)):
            return
        task = asyncio.create_task(self._recover_idle(worker))
        self._recoveries[backend] = task
        def finished(done):
            if not done.cancelled():
                done.exception()
            if self._recoveries.get(backend) is done:
                self._recoveries.pop(backend, None)
        task.add_done_callback(finished)

    async def _recover_idle(self, worker):
        backend, pool = worker.backend, self._pool
        key = (self, backend)
        while self._restart_attempts.get(backend, 0) < len(IDLE_RESTART_DELAYS):
            attempt = self._restart_attempts.get(backend, 0)
            self._restart_attempts[backend] = attempt + 1
            await asyncio.sleep(IDLE_RESTART_DELAYS[attempt])
            async with self._backend_lock(backend):
                async with pool.lock:
                    if self.closed or pool.workers.get(key) is not worker or worker.busy:
                        return
                    # Reserve its existing pool slot; recovery neither evicts
                    # another visitor nor promotes this worker in the LRU.
                    worker.busy = True
                try:
                    await worker.close()
                except BaseException:
                    worker.busy = False
                    raise
                worker.busy = False
                if self.closed:
                    return
                fresh = _Worker(self, backend, worker.provider)
                fresh.busy = True
                pool.workers[key] = fresh
                worker = fresh
                try:
                    await fresh.start()
                    if not fresh.reusable():
                        raise RuntimeError("Agent 重启后连接已中断")
                except asyncio.CancelledError:
                    await fresh.close()
                    raise
                except Exception:
                    await fresh.close()
                else:
                    # No open/turn, model lease, prompt or tool is issued here.
                    # Native sessions rebuild from SQLite on the next request.
                    return
                finally:
                    fresh.busy = False

    def _check_open(self, session_id):
        if self.closed or self.memory.is_conversation_deleted(session_id):
            raise RuntimeError("Agent 会话已关闭或删除")

    async def _acquire(self, backend, provider, session_id):
        self._check_open(session_id)
        self._state()
        pool = self._registry()
        # This manager is bound to exactly one visitor and one Workspace.
        key = (self, backend)
        async with pool.lock:
            # Closing/deletion can happen while this turn waits for capacity.
            self._check_open(session_id)
            worker = pool.workers.get(key)
            if worker is not None and worker.busy:
                raise RuntimeError("此 Agent 会话正在执行")
            if worker is not None and not worker.reusable():
                pool.workers.pop(key)
                await worker.close()
                worker = None
            if worker is None:
                while len(pool.workers) >= pool.limit:
                    victim = next((item for item in pool.workers.items() if not item[1].busy), None)
                    if victim is None:
                        raise RuntimeError("常驻 Agent 均在执行，请稍后重试")
                    pool.workers.pop(victim[0])
                    await victim[1].close()
                self._check_open(session_id)
                worker = _Worker(self, backend, provider)
                pool.workers[key] = worker
            worker.busy = True
            pool.workers.move_to_end(key)
            return key, worker

    async def complete(self, selected, messages):
        self._check_open(selected.session_id)
        turn = asyncio.current_task()
        self._turns.add(turn)
        try:
            # pi has one active native session; all backends share one model
            # relay. Serialize by backend, without spawning another process.
            async with self._backend_lock(selected.backend):
                self._check_open(selected.session_id)
                row = self.memory.get_conversation(selected.session_id)
                if row is None:
                    raise ValueError("Agent 会话不存在")
                count = row["message_count"]
                # Agent supplies one synthetic system message followed by a
                # contiguous suffix of this conversation's durable messages.
                # Comparing ordinals catches a sliding window even when users
                # repeat exactly the same text, without retaining prompt data.
                context_start = count - (len(messages) - 1)
                key, worker = await self._acquire(selected.backend, selected.provider, selected.session_id)
                keep = False
                try:
                    await worker.start()
                    self._check_open(selected.session_id)
                    state = worker.sessions.get(selected.session_id)
                    identity = (selected.provider.kind, selected.provider.base_url, selected.provider.model)
                    reset = state is not None and (
                        count <= state.seen_count or count - state.seen_count > len(messages) - 1
                        or state.context_start is not None and state.context_start != context_start)
                    if state is not None and (state.reasoning or selected.provider.deepseek_tool_reasoning):
                        items, size = worker.broker.session_usage(_session_key(selected.session_id))
                        reset |= (state.identity != identity or items >= MAX_REPLAY_ITEMS // 2
                                  or size >= MAX_REPLAY_BYTES // 2)
                    if reset:
                        await worker.drop_session(selected.session_id)
                        state = None
                    if state is None:
                        # Bound host continuation caches and native sessions
                        # without allocating another process for a new chat.
                        if len(worker.sessions) >= MAX_NATIVE_SESSIONS:
                            await worker.drop_session(next(iter(worker.sessions)))
                        state = await worker.open_session(selected.session_id, selected.provider)
                    context = (messages if not state.seen_count else
                               [messages[0], *messages[-(count - state.seen_count):]])
                    worker.turn_budget = (selected.remaining_requests, selected.remaining_tool_calls)
                    try:
                        result = await worker.turn(selected.session_id, transcript(context),
                                                   selected.provider, selected.partial_events)
                    finally:
                        # Broker counters include attempted model calls and native
                        # tool requests, not merely the projected public events.
                        broker = getattr(worker, "broker", None)
                        if broker is not None:
                            selected.remaining_requests -= broker.calls
                            selected.remaining_tool_calls -= broker.tool_calls
                    if getattr(worker, "broker", None) is None:
                        # Harmless test workers have no broker. Production always
                        # uses its authoritative counters above.
                        selected.remaining_requests -= result.steps
                        selected.remaining_tool_calls -= len(result.tool_events)
                    self._check_open(selected.session_id)
                    if result.stopped:
                        return result
                    state.seen_count = count + 1
                    state.identity, state.reasoning = identity, selected.provider.deepseek_tool_reasoning
                    state.context_start = context_start
                    worker.sessions.move_to_end(selected.session_id)
                    self._restart_attempts.pop(selected.backend, None)
                    keep = True
                    return result
                finally:
                    if keep:
                        worker.busy = False
                        if not worker.reusable():
                            self.worker_failed(worker)
                    else:
                        self._registry().workers.pop(key, None)
                        await worker.close()
        finally:
            self._turns.discard(turn)

    async def close_session(self, session_id):
        if not self.root.exists() and not self.has_session(session_id):
            return
        self._state()
        pool = self._registry()
        async def cleanup():
            error = None
            for backend in ("codex", "pi", "deepseek"):
                async with self._backend_lock(backend):
                    key = (self, backend)
                    # Reserve the worker against global idle capacity eviction.
                    async with pool.lock:
                        worker = pool.workers.get(key)
                        if worker is None or session_id not in worker.sessions:
                            continue
                        worker.busy = True
                    try:
                        await worker.drop_session(session_id)
                    except Exception:
                        # An uncertain native deletion must not leave records.
                        # Dispose this backend cache; other chats retain SQLite
                        # history and reconstruct native sessions on next use.
                        pool.workers.pop(key, None)
                        try:
                            await worker.close()
                        except Exception as exc:
                            error = error or exc
                    finally:
                        worker.busy = False
            if error is not None:
                raise error
            _remove(self._session_path(session_id))
        await _finish(cleanup())

    async def close(self):
        self.closed = True
        caller = asyncio.current_task()
        async def cleanup():
            error = None
            try:
                # Runtime.close may run without a Web loop first cancelling
                # requests. Let each active turn finish its own worker cleanup
                # before taking the registry lock, including in-flight startup.
                turns = [turn for turn in (*self._turns, *self._recoveries.values())
                         if turn is not caller and not turn.done()]
                for turn in turns:
                    turn.cancel()
                results = await asyncio.gather(*turns, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        error = error or result
                if self._pool is not None:
                    async with self._pool.lock:
                        for key in list(self._pool.workers):
                            if key[0] is self:
                                worker = self._pool.workers.pop(key)
                                try:
                                    await worker.close()
                                except Exception as exc:
                                    error = error or exc
            finally:
                if self.state_lock is not None:
                    os.close(self.state_lock)
                    self.state_lock = None
            if error is not None:
                raise error
        await _finish(cleanup())


class ResidentProvider:
    @property
    def capabilities(self):
        return ExternalProvider.capabilities.fget(self.manager)

    def __init__(self, manager, backend, provider, session_id):
        self.manager, self.backend, self.provider, self.session_id = manager, backend, provider, session_id
        self.partial_events = []
        self.remaining_requests = manager.policy.limits.max_steps
        self.remaining_tool_calls = manager.policy.limits.max_tool_calls

    async def complete(self, messages, tools):
        if self.remaining_requests <= 0:
            return ModelReply(content="已达到本轮模型调用上限，任务已暂停。", backend=self.backend, stopped=True)
        return await self.manager.complete(self, messages)


class _Worker:
    FAILURE_REASONS = {
        **ProcessGuardError.MESSAGES,
        "output_limit": "Agent 输出超过上限，已停止执行",
        "protocol_error": "Agent 协议连接异常，已停止执行",
        "native_exit": "Agent 进程已退出",
    }

    def __init__(self, manager, backend, provider):
        self.manager, self.backend, self.provider = manager, backend, provider
        self.launcher = ExternalProvider(backend, manager.config, provider, manager.workspace, manager.policy)
        self.process = self.broker = self.path = None
        self.bridge = None
        self.socket_dir = None
        self.tasks = []
        self.queue = asyncio.Queue(maxsize=128)
        self.busy = False
        self.sessions = OrderedDict()
        self.native_pid = None
        self.output_bytes = 0
        self._termination_requested = False
        self._termination_error = None
        self._close_task = None
        self.failure_code = None
        self.failure_exit_code = None

    @property
    def failure_reason(self):
        if self.failure_code is None:
            return ""
        reason = self.FAILURE_REASONS[self.failure_code]
        if self.failure_exit_code is not None:
            reason += (f"（信号 {-self.failure_exit_code}）" if self.failure_exit_code < 0
                       else f"（退出码 {self.failure_exit_code}）")
        return reason + "。"

    def _remember_failure(self, code, exit_code=None):
        if self.failure_code is not None or self._close_task is not None:
            return
        self.failure_code = code if isinstance(code, str) and code in self.FAILURE_REASONS else "protocol_error"
        if self.failure_code == "native_exit" and type(exit_code) is int and -255 <= exit_code <= 255:
            self.failure_exit_code = exit_code
        backend = self.backend if self.backend in {"codex", "pi", "deepseek"} else "unknown"
        pid = getattr(self.process, "pid", None)
        pid = pid if type(pid) is int and pid > 0 else None
        logging.getLogger(__name__).warning(
            "Resident Agent stopped: backend=%s manager_pid=%s reason=%s exit_code=%s",
            backend, pid, self.failure_code, self.failure_exit_code)

    def reusable(self):
        if self.process is None or self.process.returncode is not None or self._close_task is not None:
            return False
        if any(task.done() for task in self.tasks):
            return False
        return True

    async def start(self):
        if self.process is not None:
            return
        self.launcher._prepare()
        await self.launcher.executor.check()
        parent = self.manager._process_path(self.backend)
        _private(parent)
        self.path = Path(tempfile.mkdtemp(prefix=self.backend + "-", dir=parent))
        home = self.path / "home"
        _private(home)
        models = self.manager.config.model_choices
        limits = self.manager.policy.limits
        # Unix socket paths are capped near 108 bytes; tenant state paths are
        # deliberately long. Keep only the credential-free socket in short /tmp.
        self.socket_dir = Path(tempfile.mkdtemp(prefix="joyagent-rpc-"))
        self.broker = ModelBroker(self.socket_dir / "model.sock", self.provider, max_requests=limits.max_steps,
            max_tool_calls=limits.max_tool_calls, timeout=limits.turn_timeout_seconds)
        self.broker.lease_required = True
        self.broker.deactivate()
        token = secrets.token_hex(32)
        if self.launcher.executor.settings.get("network", "none") == "host":
            self.bridge = LoopbackBridge(self.broker.path, token, resident=True)
        endpoint = self.bridge.endpoint if self.bridge is not None else "http://127.0.0.1:18765/v1"
        argv, additions = build_resident_profile(self.backend, self.provider.model,
            endpoint, home, self.launcher.root, models, broker_token=token)
        plan = self.path / "plan.json"
        plan.write_text(json.dumps({"argv": argv, "env": additions, "port": 18765,
            "socket": "/run/joyagent/model.sock", "backend": self.backend,
            "model": self.provider.model, "models": models, "output_limit": limits.tool_output_chars,
            "session_limit": MAX_NATIVE_SESSIONS, "external_bridge": self.bridge is not None}))
        plan.chmod(0o600)
        command, env = self.launcher._launch(home, plan, self.broker.path, resident=True)
        self.process = await asyncio.create_subprocess_exec(*command, env=env, cwd="/",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=1024 * 1024 + 1)
        self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._errors()),
                      asyncio.create_task(watch_process(self.process))]
        for task in self.tasks:
            task.add_done_callback(self._failed)
        async with asyncio.timeout(30):
            ready = await self._next()
        if ready.get("type") != "ready" or type(ready.get("native_pid")) is not int:
            raise ValueError("常驻 Agent 未能启动服务")
        self.native_pid = ready["native_pid"]

    async def _send(self, op, session_id, **fields):
        identifier = uuid.uuid4().hex
        raw = json.dumps({"op": op, "id": identifier, "session": _session_key(session_id), **fields},
                         ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(raw) > 1024 * 1024:
            raise ValueError("Agent 输入超过上限")
        self.process.stdin.write(raw)
        await self.process.stdin.drain()
        return identifier

    async def _control(self, op, session_id, **fields):
        self.output_bytes = 0
        async with asyncio.timeout(30):
            identifier = await self._send(op, session_id, **fields)
            record = await self._next()
        if (record.get("id") != identifier or record.get("type") != op
                or record.get("session") != _session_key(session_id)):
            raise ValueError("Agent 会话操作响应无效")
        return record

    async def open_session(self, session_id, provider):
        if session_id in self.sessions:
            return self.sessions[session_id]
        record = await self._control("open", session_id, model=provider.model)
        identity = record.get("native_session_id")
        if (not isinstance(identity, str) or not identity or len(identity) > 256
                or any(state.native_session_id == identity for state in self.sessions.values())):
            raise ValueError("Agent 原生会话标识无效或重复")
        state = NativeSession(identity)
        self.sessions[session_id] = state
        return state

    async def drop_session(self, session_id):
        state = self.sessions.get(session_id)
        if state is None:
            return
        record = await self._control("drop", session_id)
        if record.get("native_session_id") != state.native_session_id:
            raise ValueError("Agent 删除响应不属于当前会话")
        self.broker.forget_session(_session_key(session_id))
        self.sessions.pop(session_id)

    def _failed(self, task):
        # Resource and protocol guards remain active while the Agent is idle.
        # Observe exceptions now and revoke access without waiting for another turn.
        if not task.cancelled():
            error = task.exception()
            self._remember_failure(error.code if isinstance(error, ProcessGuardError)
                                   else "protocol_error" if error is not None else "native_exit")
        self._terminate()
        if self.broker is not None:
            with contextlib.suppress(RuntimeError):
                self.broker.deactivate()
        if self.bridge is not None:
            self.bridge.server.lease = None
        notify = getattr(self.manager, "worker_failed", None)
        if callable(notify):
            notify(self)

    def _terminate(self):
        if self._termination_requested or self.process is None or self.process.returncode is not None:
            return
        # Several reader/guard callbacks can finish together before asyncio
        # observes SIGKILL. Signalling the same dying group again can return
        # EPERM on macOS; only one owner sends its termination request.
        self._termination_requested = True
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # A disappearing process group can reject killpg while the owned
            # subprocess handle can still be signalled/reaped normally.
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            except OSError as error:
                self._termination_error = error
        except OSError as error:
            self._termination_error = error

    async def _read(self):
        while raw := await self.process.stdout.readline():
            self.output_bytes += len(raw)
            if self.output_bytes > 8 * 1024 * 1024:
                self._remember_failure("output_limit")
                raise ValueError("Agent 输出超过上限")
            record = json.loads(raw)
            if not isinstance(record, dict) or record.get("type") == "fatal":
                if isinstance(record, dict):
                    code = record.get("code")
                    self._remember_failure(code if code in ("native_exit", "output_limit", "protocol_error")
                                           else "protocol_error", record.get("exit_code"))
                raise ValueError("常驻 Agent 连接已中断")
            self.queue.put_nowait(record)
        self._remember_failure("native_exit")
        raise RuntimeError("常驻 Agent 已退出")

    async def _errors(self):
        while raw := await self.process.stderr.read(8192):
            self.output_bytes += len(raw)
            if self.output_bytes > 8 * 1024 * 1024:
                self._remember_failure("output_limit")
                raise ValueError("Agent 输出超过上限")
        self._remember_failure("native_exit")
        raise RuntimeError("常驻 Agent 已退出")

    def _preserve_tools(self, identifier, partial, first=None, session=None):
        if identifier is None or partial is None:
            return
        records = [first] if first is not None else []
        while not self.queue.empty():
            records.append(self.queue.get_nowait())
        for record in records:
            if (isinstance(record, dict) and record.get("id") == identifier
                    and record.get("session") == session
                    and record.get("type") == "tool" and isinstance(record.get("event"), dict)
                    and len(partial) < 64):
                partial.append(record["event"])

    async def _next(self, identifier=None, partial=None, session=None):
        receive = asyncio.create_task(self.queue.get())
        waited = False
        try:
            await asyncio.wait((receive, *self.tasks), return_when=asyncio.FIRST_COMPLETED)
            waited = True
        finally:
            receive.cancel()
            try:
                await asyncio.gather(receive, return_exceptions=True)
            finally:
                if not waited or any(task.done() for task in self.tasks):
                    first = receive.result() if receive.done() and not receive.cancelled() else None
                    self._preserve_tools(identifier, partial, first, session)
        # Recheck after waiter cleanup: a guard may have failed at that await.
        # Its failure wins over any queued success, while completed tools above
        # remain available to the public failure/audit record.
        if any(task.done() for task in self.tasks):
            raise RuntimeError("常驻 Agent 已停止或超过资源限制")
        return receive.result()

    async def turn(self, session_id, prompt, provider, partial):
        limits = self.manager.policy.limits
        self.output_bytes = 0
        state = self.sessions[session_id]
        self.broker.select_session(_session_key(session_id))
        requests, tool_calls = getattr(self, "turn_budget", (limits.max_steps, limits.max_tool_calls))
        lease = self.broker.activate(provider, max_requests=max(0, requests),
            max_tool_calls=max(0, tool_calls), timeout=limits.turn_timeout_seconds)
        if self.bridge is not None:
            self.bridge.server.lease = lease
        self.provider = provider
        identifier = await self._send("turn", session_id, prompt=prompt, model=provider.model, lease=lease)
        async with asyncio.timeout(limits.turn_timeout_seconds):
            while True:
                record = await self._next(identifier, partial, _session_key(session_id))
                if record.get("id") != identifier or record.get("session") != _session_key(session_id):
                    raise ValueError("Agent 响应不属于当前轮")
                if record.get("type") == "tool":
                    event = record.get("event")
                    if not isinstance(event, dict) or len(partial) >= 64:
                        raise ValueError("Agent 工具结果无效")
                    partial.append(event)
                    continue
                if (record.get("type") != "result" or type(record.get("stopped")) is not bool
                        or not isinstance(record.get("text"), str)
                        or not isinstance(record.get("tool_events"), list) or len(record["tool_events"]) > 64
                        or record.get("native_session_id") != state.native_session_id):
                    raise ValueError("Agent 返回格式无效")
                stopped = record["stopped"] or bool(self.broker.failure)
                text = (shared_provider_message(self.broker.failure) if self.broker.failure else record["text"])
                self.broker.deactivate()
                if self.bridge is not None:
                    self.bridge.server.lease = None
                return ModelReply(content=text, backend=self.backend, stopped=stopped,
                                  steps=self.broker.calls, tool_events=record["tool_events"])

    async def close(self):
        # Send the kill before awaiting anything; cleanup cannot leave a native
        # namespace or model lease alive after cancellation or permanent delete.
        if self._close_task is None:
            self._terminate()
            errors = []
            if self.bridge is not None:
                try:
                    self.bridge.close()
                except Exception as error:
                    errors.append(error)
                finally:
                    self.bridge = None
            if self.broker is not None:
                try:
                    self.broker.close()
                except Exception as error:
                    errors.append(error)
                finally:
                    self.broker = None

            async def reap():
                if self.process is not None:
                    try:
                        if self._termination_error is not None:
                            # Unexpected OS refusal must not hang service close
                            # or prevent other resources from being disposed.
                            async with asyncio.timeout(5):
                                await self.process.wait()
                        else:
                            await self.process.wait()
                    except Exception as error:
                        errors.append(error)
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                self.sessions.clear()
                for path in (self.path, self.socket_dir):
                    if path is not None:
                        try:
                            _remove(path)
                        except Exception as error:
                            errors.append(error)
                if errors:
                    raise errors[0]

            self._close_task = create_cleanup_task(reap())

        async def wait_cleanup():
            await asyncio.shield(self._close_task)

        await _finish(wait_cleanup())
