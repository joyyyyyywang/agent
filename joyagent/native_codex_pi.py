"""Persistent Codex app-server and pi RPC sessions over an owned transport.

The transport owns the process, deadlines and bounded JSONL reads. These adapters
own one native session, not a process: successful turns retain native context;
the host serializes adapters sharing a process and discards that process after a
failed or cancelled turn. pi session files live only in the owned private cache.

No provider credentials, filesystem paths supplied by visitors, reasoning, or
stderr are part of this interface. The surrounding namespace is the sandbox.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from collections import deque
from copy import deepcopy
from typing import Callable

try:
    from .cli_events import CLIEvents
except ImportError:  # The isolated host mounts these two modules as siblings.
    from cli_events import CLIEvents


def _identifier(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 256
            and not any(ord(char) < 32 for char in value))


def _model(value: object) -> None:
    if not _identifier(value) or not value.strip():
        raise ValueError("Agent 模型名称无效")


class _Adapter:
    backend: str

    def __init__(self, rpc, publish: Callable[[dict], None], output_limit: int = 8000):
        self.rpc = rpc
        self.publish = publish
        self.output_limit = output_limit
        self.parser = CLIEvents(self.backend, output_limit)
        self.native_session_id = ""
        self._active = False
        self._cancelled = False
        self._broken = False
        self._done: asyncio.Future | None = None
        self._published = 0

    def _activate(self) -> None:
        # The host serializes start/turn/drop for the entire backend process.
        self.rpc.on_event = self._event

    def _begin(self, prompt: str, model: str) -> None:
        _model(model)
        if not isinstance(prompt, str):
            raise ValueError("Agent 输入必须是文本")
        if not self.native_session_id or self._active or self._broken:
            raise RuntimeError("Agent 原生会话不可用")
        self._activate()
        self.parser = CLIEvents(self.backend, self.output_limit)
        self._published = 0
        self._cancelled = False
        self._active = True
        self._done = asyncio.get_running_loop().create_future()

    def _fail(self) -> None:
        self._broken = True
        if self.backend == "pi":
            self.parser.feed({"type": "agent_settled", "aborted": True})
        else:
            self.parser.feed({"type": "turn.failed"})
        self._finish()

    def _finish(self) -> None:
        if self._done is not None and not self._done.done():
            self._done.set_result(None)

    def _feed(self, record: dict) -> None:
        self.parser.feed(record)
        while self._published < len(self.parser.events):
            event = self.parser.events[self._published]
            self._published += 1
            try:
                self.publish(deepcopy(event))
            except Exception:
                self._fail()
                raise RuntimeError("Agent 工具事件传输失败") from None
        if self.parser.failed:
            self._fail()

    def _result(self) -> dict:
        stopped = self._cancelled or self.parser.failed or not self.parser.completed
        self._broken = self._broken or stopped
        self._active = False
        text = ("Agent 本轮执行中断，未收到最终结果；已执行的操作见工具记录，请核实后继续。"
                if stopped else self.parser.text)
        return {"text": text, "tool_events": deepcopy(self.parser.events),
                "stopped": stopped, "native_session_id": self.native_session_id}


class _CodexLogs:
    """Disable the pinned server's independent SQLite TRACE sink.

    0.142.5 ignores RUST_LOG for this sink and does not purge it on thread/delete.
    These triggers affect only our generated CODEX_HOME. Install them before any
    thread is created; later drift is a fatal error, never silently repaired.
    """

    _COLUMNS = {"id", "ts", "ts_nanos", "level", "target", "feedback_log_body",
                "thread_id", "process_uuid", "module_path", "file", "line", "estimated_bytes"}
    _TRIGGERS = {
        "joyagent_logs_no_insert": "CREATE TRIGGER joyagent_logs_no_insert BEFORE INSERT ON logs BEGIN SELECT RAISE(IGNORE); END",
        "joyagent_logs_no_update": "CREATE TRIGGER joyagent_logs_no_update BEFORE UPDATE ON logs BEGIN SELECT RAISE(IGNORE); END",
    }

    def __init__(self, root):
        self.root = Path(root).absolute()
        self.identity = None
        self.databases = {}

    def _path(self, filename="logs_2.sqlite") -> Path:
        if ".." in self.root.parts or filename not in {"logs_2.sqlite", "state_5.sqlite"}:
            raise ValueError("Codex 日志目录无效")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.root.anchor, flags)
        try:
            for part in self.root.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            directory = os.fstat(descriptor)
            if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
                raise ValueError("Codex 日志目录不是私有目录")
            database = None
            for name in (filename, filename + "-wal", filename + "-shm"):
                try:
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    if name == filename:
                        raise
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or info.st_mode & 0o022):
                    raise ValueError("Codex 日志文件无效")
                if name == filename:
                    database = (info.st_dev, info.st_ino)
            identity = (directory.st_dev, directory.st_ino)
            if (self.identity is not None and self.identity != identity
                    or filename in self.databases and self.databases[filename] != database):
                raise ValueError("Codex 日志目录已替换")
            self.identity = identity
            self.databases[filename] = database
            return self.root / filename
        finally:
            os.close(descriptor)

    def check(self, *, install=False) -> None:
        path = self._path()
        self._path("state_5.sqlite")
        # Do not create a replacement database if native initialization failed.
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2,
                                     isolation_level=None)
        try:
            self._path()
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("BEGIN IMMEDIATE")
            table = connection.execute("SELECT type FROM sqlite_master WHERE name='logs'").fetchone()
            columns = {row[1] for row in connection.execute("PRAGMA table_info(logs)")}
            if table != ("table",) or columns != self._COLUMNS:
                raise ValueError("Codex 日志结构不兼容")
            triggers = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
            if install:
                if any(name not in self._TRIGGERS or sql != self._TRIGGERS[name]
                       for name, sql in triggers.items()):
                    raise ValueError("Codex 日志策略无效")
                for name, sql in self._TRIGGERS.items():
                    if name not in triggers:
                        connection.execute(sql)
                connection.execute("DELETE FROM logs")
            elif triggers != self._TRIGGERS:
                raise ValueError("Codex 日志策略已改变")
            # Match the native writer: execute() must succeed without a row.
            connection.execute("INSERT INTO logs (ts,ts_nanos,level,target,feedback_log_body,estimated_bytes) "
                               "VALUES (0,0,'INFO','joyagent','joyagent-log-policy-probe',0)")
            if connection.execute("SELECT 1 FROM logs LIMIT 1").fetchone() is not None:
                raise ValueError("Codex 日志内容未禁用")
            connection.execute("COMMIT")
            if install:
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or checkpoint[0] != 0:
                    raise ValueError("Codex 日志镜像清理未完成")
            self._path()
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

    def purge_deleted_thread(self, identity: str) -> None:
        """Remove deleted metadata bodies from our native SQLite pages/WAL."""
        path = self._path("state_5.sqlite")
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2,
                                     isolation_level=None)
        try:
            self._path("state_5.sqlite")
            table = connection.execute("SELECT type FROM sqlite_master WHERE name='threads'").fetchone()
            columns = {row[1]: row for row in connection.execute("PRAGMA table_info(threads)")}
            if (table != ("table",) or "id" not in columns
                    or columns["id"][2] != "TEXT" or columns["id"][5] != 1):
                raise ValueError("Codex 会话索引结构不兼容")
            if connection.execute("SELECT 1 FROM threads WHERE id=?", (identity,)).fetchone() is not None:
                raise ValueError("Codex 会话索引尚未删除")
            for statement in ("PRAGMA wal_checkpoint(TRUNCATE)", "VACUUM", "PRAGMA wal_checkpoint(TRUNCATE)"):
                row = connection.execute(statement).fetchone()
                if statement.startswith("PRAGMA") and (row is None or row[0] != 0):
                    raise ValueError("Codex 会话索引镜像清理未完成")
            self._path("state_5.sqlite")
        finally:
            connection.close()


class CodexAdapter(_Adapter):
    """Codex 0.142.5 app-server JSON-RPC (without a ``jsonrpc`` field)."""

    backend = "codex"

    def __init__(self, rpc, publish: Callable[[dict], None], output_limit: int = 8000,
                 *, state_root: str | Path | None = None):
        super().__init__(rpc, publish, output_limit)
        self._turn_id: str | None = None
        self._retired_turns: deque[str] = deque(maxlen=64)
        # Real hosts must pass their generated CODEX_HOME. None permits a pure
        # fake RPC transport without filesystem state in adapter unit tests.
        self._logs = _CodexLogs(state_root) if state_root is not None else None

    async def start(self, model: str) -> str:
        _model(model)
        if self.native_session_id or self._broken:
            raise RuntimeError("Codex 原生会话已经初始化")
        try:
            self._activate()
            if not getattr(self.rpc, "_joyagent_codex_initialized", False):
                await self.rpc.request("initialize", {
                    "clientInfo": {"name": "joyagent", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                })
                await self.rpc.send({"method": "initialized", "params": {}})
                self.rpc._joyagent_codex_initialized = True
            if self._logs is not None:
                install = not getattr(self.rpc, "_joyagent_codex_logs_installed", False)
                self._logs.check(install=install)
                self.rpc._joyagent_codex_logs_installed = True
            result = await self.rpc.request("thread/start", {
                "model": model, "modelProvider": "joyagent", "cwd": "/workspace",
                "approvalPolicy": "never", "sandbox": "danger-full-access",
                # The pinned server rejects thread/delete for ephemeral
                # threads. Persist only within the disposable private HOME.
                "ephemeral": False,
            })
            identity = result.get("thread", {}).get("id")
            if not _identifier(identity) or self._broken:
                raise ValueError("invalid native thread")
            self.native_session_id = identity
            return identity
        except asyncio.CancelledError:
            self._broken = True
            raise
        except Exception:
            self._broken = True
            raise RuntimeError("Codex 原生会话初始化失败") from None

    def _accept_turn(self, identity: object) -> bool:
        if not _identifier(identity):
            self._fail()
            return False
        if identity in self._retired_turns:
            return False
        if self._turn_id is None:
            self._turn_id = identity
        return self._turn_id == identity

    async def turn(self, prompt: str, model: str) -> dict:
        self._begin(prompt, model)
        self._turn_id = None
        try:
            if self._logs is not None:
                self._logs.check()
            result = await self.rpc.request("turn/start", {
                "threadId": self.native_session_id, "model": model,
                "input": [{"type": "text", "text": prompt}],
            })
            if not self._accept_turn(result.get("turn", {}).get("id")):
                self._fail()
            await self._done
            if self._turn_id:
                self._retired_turns.append(self._turn_id)
            if self._logs is not None:
                self._logs.check()
            return self._result()
        except asyncio.CancelledError:
            # Keep the turn id available for the host's subsequent cancel().
            self._broken = True
            raise
        except Exception:
            self._fail()
            raise RuntimeError("Codex 原生会话执行失败") from None

    async def cancel(self) -> None:
        if not self._active:
            return
        self._cancelled = True
        self._fail()
        if self._turn_id:
            await self.rpc.request("turn/interrupt", {
                "threadId": self.native_session_id, "turnId": self._turn_id,
            })

    async def drop(self) -> None:
        if self._active:
            raise RuntimeError("Codex 会话正在执行")
        if not self.native_session_id:
            return
        self._activate()
        try:
            if self._logs is not None:
                self._logs.check()
            await self.rpc.request("thread/backgroundTerminals/clean", {
                "threadId": self.native_session_id,
            })
            deleted = await self.rpc.request("thread/delete", {"threadId": self.native_session_id})
            if deleted != {} or self._broken:
                raise ValueError("native thread deletion failed")
            loaded = await self.rpc.request("thread/loaded/list", {"limit": 1000})
            identities = loaded.get("data") if isinstance(loaded, dict) else None
            if (not isinstance(identities, list) or len(identities) > 1000
                    or not all(_identifier(value) for value in identities)
                    or loaded.get("nextCursor") is not None
                    or self.native_session_id in identities):
                raise ValueError("native thread remains loaded")
            if self._logs is not None:
                self._logs.purge_deleted_thread(self.native_session_id)
                self._logs.check()
            # This is a protocol deletion, not delayed unsubscribe. Deletion
            # removes the private rollout and index before acknowledging it.
            self.native_session_id = ""
            self._broken = True
        except asyncio.CancelledError:
            self._broken = True
            raise
        except Exception:
            self._broken = True
            raise RuntimeError("Codex 原生会话删除失败") from None

    async def _event(self, record: dict) -> None:
        if not isinstance(record, dict):
            self._fail()
            return
        method = record.get("method")
        if not isinstance(method, str):
            return
        if "id" in record:
            # Approval, dynamic tools, auth refresh and every other client RPC
            # are disabled. Never grant permissions from a model-originated RPC.
            await self.rpc.send({"id": record["id"], "error": {
                "code": -32601, "message": "Client requests are disabled",
            }})
            self._fail()
            return
        if not self._active:
            return
        params = record.get("params")
        if not isinstance(params, dict):
            if method in {"turn/started", "turn/completed", "item/completed", "error"}:
                self._fail()
            return
        if params.get("threadId") != self.native_session_id:
            return
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn")
            if not isinstance(turn, dict):
                self._fail()
                return
            if not self._accept_turn(turn.get("id")):
                return
            if method == "turn/started":
                return
            items = turn.get("items", [])
            if not isinstance(items, list):
                self._fail()
                return
            # v0.142.5 usually sends itemsView=notLoaded here. Accept a full
            # payload too; CLIEvents deduplicates completed native tool ids.
            for item in items:
                self._item(item)
            if turn.get("status") == "completed" and not turn.get("error"):
                self._feed({"type": "turn.completed"})
            else:
                self._fail()
            self._finish()
        elif method == "item/completed":
            if self._turn_id is not None and params.get("turnId") == self._turn_id:
                self._item(params.get("item"))
        elif method == "error":
            if params.get("turnId") in {None, self._turn_id}:
                self._fail()

    def _item(self, item: object) -> None:
        if not isinstance(item, dict):
            self._fail()
            return
        kind = item.get("type")
        mapping = {"agentMessage": "agent_message", "commandExecution": "command_execution",
                   "fileChange": "file_change", "mcpToolCall": "mcp_tool_call",
                   "dynamicToolCall": "dynamic_tool_call"}
        if kind not in mapping:
            # In particular, never project reasoning or its summaries.
            return
        if not _identifier(item.get("id")):
            self._fail()
            return
        projected = {"id": item["id"], "type": mapping[kind]}
        for key in ("text", "status", "command", "server", "tool", "result",
                    "error", "changes", "success"):
            if key in item:
                projected[key] = item[key]
        for source, target in (("exitCode", "exit_code"),
                               ("aggregatedOutput", "aggregated_output"),
                               ("contentItems", "content_items")):
            if source in item:
                projected[target] = item[source]
        self._feed({"type": "item.completed", "item": projected})


class PiAdapter(_Adapter):
    """pi 1.3.0 RPC; prompt acknowledgement is not run completion."""

    backend = "pi"

    def __init__(self, rpc, publish: Callable[[dict], None], output_limit: int = 8000,
                 *, session_root: str | Path | None = None):
        super().__init__(rpc, publish, output_limit)
        self._prompt_sent = False
        self._session_root = Path(session_root).absolute() if session_root is not None else None
        self._root_identity: tuple[int, int] | None = None
        self._session_file: Path | None = None
        self._has_turn = False

    @contextmanager
    def _root_fd(self):
        root = self._session_root
        if root is None or not root.is_absolute() or ".." in root.parts:
            raise ValueError("pi 会话目录无效")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root.anchor, flags)
        try:
            for part in root.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
                    or self._root_identity is not None and identity != self._root_identity):
                raise ValueError("pi 会话目录无效")
            if self._root_identity is None:
                self._root_identity = identity
            yield descriptor
        finally:
            os.close(descriptor)

    def _path(self, value: object) -> Path:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("pi 会话文件无效")
        path = Path(value)
        # Native paths must be direct children, with no symlink resolution.
        if (not path.is_absolute() or path.parent != self._session_root or path.suffix != ".jsonl"
                or any(ord(char) < 32 for char in value)):
            raise ValueError("pi 会话文件无效")
        return path

    def _stat_file(self, descriptor: int, path: Path, *, required: bool) -> None:
        try:
            info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if required:
                raise ValueError("pi 会话文件丢失") from None
        else:
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or info.st_mode & 0o022):
                raise ValueError("pi 会话文件无效")

    def _file(self, value: object, *, required: bool = False) -> Path:
        path = self._path(value)
        with self._root_fd() as descriptor:
            self._stat_file(descriptor, path, required=required)
        return path

    async def _state(self) -> dict:
        state = (await self._command("get_state")).get("data")
        if (not isinstance(state, dict) or not _identifier(state.get("sessionId"))
                or state.get("isStreaming") is not False
                or state.get("isCompacting") is not False
                or type(state.get("pendingMessageCount")) is not int
                or state["pendingMessageCount"] != 0 or self._broken):
            raise ValueError("invalid native session")
        self._file(state.get("sessionFile"))
        return state

    async def _replace(self, command: str, **fields) -> None:
        response = await self._command(command, **fields)
        if response.get("data", {}).get("cancelled") is not False:
            raise RuntimeError("pi 会话切换失败")

    async def _settings(self) -> None:
        # Session replacement recreates the runtime, so disable automatic calls
        # after every replacement rather than assuming these are process flags.
        await self._command("set_auto_retry", enabled=False)
        await self._command("set_auto_compaction", enabled=False)

    async def _select(self) -> None:
        state = await self._state()
        if state["sessionId"] == self.native_session_id:
            if self._file(state["sessionFile"]) != self._session_file:
                raise ValueError("pi 会话身份不一致")
            self._file(str(self._session_file), required=self._has_turn)
            return
        path = self._file(str(self._session_file))
        if not path.exists() and not self._has_turn:
            # pi writes a fresh file only after its first user message. An
            # unopened empty context has nothing to restore; make a fresh one.
            await self._replace("new_session")
            state = await self._state()
            self.native_session_id = state["sessionId"]
            self._session_file = self._file(state["sessionFile"])
        else:
            self._file(str(path), required=True)
            await self._replace("switch_session", sessionPath=str(path))
            state = await self._state()
            if (state["sessionId"] != self.native_session_id
                    or self._file(state["sessionFile"]) != path):
                raise ValueError("pi 会话身份不一致")
        await self._settings()

    async def _command(self, kind: str, **fields) -> dict:
        response = await self.rpc.exchange({"type": kind, **fields})
        if (not isinstance(response, dict) or response.get("type") != "response"
                or response.get("command") != kind or response.get("success") is not True):
            raise RuntimeError("pi 原生命令失败")
        return response

    async def _set_model(self, model: str) -> None:
        response = await self._command("set_model", provider="joyagent", modelId=model)
        selected = response.get("data")
        if (not isinstance(selected, dict) or selected.get("provider") != "joyagent"
                or selected.get("id") != model):
            raise RuntimeError("pi 原生模型未正确选择")

    async def start(self, model: str) -> str:
        _model(model)
        if self.native_session_id or self._broken:
            raise RuntimeError("pi 原生会话已经初始化")
        try:
            self._activate()
            with self._root_fd():
                pass
            await self._replace("new_session")
            await self._settings()
            await self._set_model(model)
            state = await self._state()
            self.native_session_id = state["sessionId"]
            self._session_file = self._file(state["sessionFile"])
            return self.native_session_id
        except asyncio.CancelledError:
            self._broken = True
            raise
        except Exception:
            self._broken = True
            raise RuntimeError("pi 原生会话初始化失败") from None

    async def turn(self, prompt: str, model: str) -> dict:
        self._begin(prompt, model)
        self._prompt_sent = False
        try:
            await self._select()
            await self._set_model(model)
            self._prompt_sent = True
            response = await self._command("prompt", message=prompt)
            if response.get("data", {}).get("disposition") != "started":
                self._fail()
            await self._done
            self._prompt_sent = False
            self._has_turn = True
            return self._result()
        except asyncio.CancelledError:
            self._broken = True
            raise
        except Exception:
            self._fail()
            raise RuntimeError("pi 原生会话执行失败") from None

    async def drop(self) -> None:
        if self._active:
            raise RuntimeError("pi 会话正在执行")
        if not self.native_session_id:
            return
        self._activate()
        try:
            state = await self._state()
            if state["sessionId"] == self.native_session_id:
                if self._file(state["sessionFile"]) != self._session_file:
                    raise ValueError("pi 会话身份不一致")
                # Detach the old runtime first, so it cannot flush or recreate
                # the removed file. The fresh empty session needs no model call.
                await self._replace("new_session")
                await self._settings()
                replacement = await self._state()
                if (replacement["sessionId"] == self.native_session_id
                        or self._file(replacement["sessionFile"]) == self._session_file):
                    raise ValueError("pi 会话未卸载")
            path = self._path(str(self._session_file))
            with self._root_fd() as descriptor:
                self._stat_file(descriptor, path, required=self._has_turn)
                try:
                    os.unlink(path.name, dir_fd=descriptor)
                except FileNotFoundError:
                    if self._has_turn:
                        raise
            # A renamed/replaced ancestor must not turn deletion into success
            # for a different directory. The caller then resets the whole HOME.
            with self._root_fd():
                pass
            self.native_session_id = ""
            self._session_file = None
            self._broken = True
        except asyncio.CancelledError:
            self._broken = True
            raise
        except Exception:
            self._broken = True
            raise RuntimeError("pi 原生会话删除失败") from None

    async def cancel(self) -> None:
        if not self._active:
            return
        self._cancelled = True
        self._fail()
        # abort by itself may continue queued prompts in pi. Clear first, even
        # though this adapter never intentionally queues or steers a prompt.
        try:
            await self._command("clear_queue")
        finally:
            await self._command("abort")

    async def _event(self, record: dict) -> None:
        if not isinstance(record, dict):
            self._fail()
            return
        kind = record.get("type")
        if kind == "extension_ui_request":
            await self.rpc.send({"type": "extension_ui_response", "id": record.get("id"),
                                 "cancelled": True})
            self._fail()
            return
        if not self._active or not self._prompt_sent:
            return
        if (kind == "response" and record.get("success") is not True
                or kind in {"auto_retry_start", "auto_compaction_start"}):
            self._fail()
            return
        self._feed(record)
        if kind == "agent_settled":
            self._finish()
