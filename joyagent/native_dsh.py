"""DeepSeek Harness 0.2.1-rc.2 ACP adapter for one resident native session.

The parent owns the isolated process, model-broker lease, timeouts and cleanup.
Only committed ACP text and bounded terminal tool results become public output.
The SDK and headless profiles are intentionally not used: ACP supplies native
per-session model selection, cancellation, and prompt completion barriers.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

if __package__:
    from .cli_events import CLIEvents
else:  # The isolated host imports read-only sibling modules under /run/joyagent.
    from cli_events import CLIEvents


class DSHProtocolError(RuntimeError):
    """A safe protocol failure; native diagnostics must stay private."""


class DSHAdapter:
    def __init__(self, rpc, publish, output_limit: int = 8000, *, state_root=None):
        if type(output_limit) is not int or output_limit < 1:
            raise ValueError("output_limit must be a positive integer")
        if not callable(publish):
            raise ValueError("publish must be callable")
        self.rpc, self.publish, self.output_limit = rpc, publish, output_limit
        self.native_session_id: str | None = None
        # Only the host's generated DSH_HOME may be supplied, never a Web field.
        self.state_root = Path(state_root) if state_root is not None else None
        self._state_identity: tuple[int, int] | None = None
        self._started = self._running = self._broken = False
        self._native_closed = self._dropped = False
        self._reset_turn()
        self.rpc.on_event = self._on_event

    def _reset_turn(self) -> None:
        self._text = ""
        self._message_id = None
        self._tools = CLIEvents("deepseek", limit=self.output_limit)
        self._known_tools: set[str] = set()
        self._finished_tools: set[str] = set()
        self._tool_overflow = self._malformed = False

    @staticmethod
    def _model_value(model: str) -> str:
        if (not isinstance(model, str) or not model.strip() or len(model) > 256
                or any(ord(char) < 32 for char in model)):
            raise DSHProtocolError("DeepSeek Harness 模型名称无效")
        return json.dumps(["joyagent", model], ensure_ascii=False, separators=(",", ":"))

    async def _request(self, method: str, params: dict) -> dict:
        try:
            response = await self.rpc.request(method, params)
        except Exception:
            raise DSHProtocolError("DeepSeek Harness 原生会话请求失败") from None
        if not isinstance(response, dict):
            raise DSHProtocolError("DeepSeek Harness 返回了无效协议响应")
        return response

    async def _select_model(self, model: str) -> None:
        value = self._model_value(model)
        result = await self._request("session/set_config_option", {
            "sessionId": self.native_session_id, "configId": "model", "value": value,
        })
        options = result.get("configOptions")
        if not isinstance(options, list) or len(options) > 64:
            raise DSHProtocolError("DeepSeek Harness 未确认模型选择")
        matches = [option for option in options if isinstance(option, dict) and option.get("id") == "model"]
        if len(matches) != 1 or matches[0].get("currentValue") != value:
            raise DSHProtocolError("DeepSeek Harness 未确认模型选择")

    async def start(self, model: str) -> str:
        self._model_value(model)
        if self._broken:
            raise DSHProtocolError("DeepSeek Harness 会话需要重新启动")
        self.rpc.on_event = self._on_event
        if self._started:
            return self.native_session_id
        if self.native_session_id is not None:
            raise DSHProtocolError("DeepSeek Harness 会话需要重新启动")
        try:
            self._bind_state_root()
            # The host serializes operations on one ACP connection, while
            # keeping a separate adapter/native session for each Web chat.
            if not getattr(self.rpc, "_joyagent_dsh_initialized", False):
                initialized = await self._request("initialize", {
                    "protocolVersion": 1, "clientCapabilities": {},
                    "clientInfo": {"name": "joyagent", "version": "1"},
                })
                info = initialized.get("agentInfo")
                if (type(initialized.get("protocolVersion")) is not int or initialized["protocolVersion"] != 1
                        or not isinstance(info, dict) or info.get("name") != "deepseek-harness-acp"):
                    raise DSHProtocolError("DeepSeek Harness ACP 协议不兼容")
                self.rpc._joyagent_dsh_initialized = True
            created = await self._request("session/new", {"cwd": "/workspace", "mcpServers": []})
            identity = created.get("sessionId")
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,128}", identity):
                raise DSHProtocolError("DeepSeek Harness 返回了无效会话标识")
            self.native_session_id = identity
            await self._select_model(model)
            self._started = True
            return identity
        except BaseException:
            self._broken = True
            raise

    async def turn(self, prompt: str, model: str) -> dict:
        if not self._started or self._broken:
            raise DSHProtocolError("DeepSeek Harness 会话尚未就绪")
        if self._running:
            raise DSHProtocolError("DeepSeek Harness 已有请求正在执行")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DSHProtocolError("DeepSeek Harness 输入不能为空")
        self.rpc.on_event = self._on_event
        self._reset_turn()
        self._running = True
        try:
            await self._select_model(model)
            result = await self._request("session/prompt", {
                "sessionId": self.native_session_id,
                "prompt": [{"type": "text", "text": prompt}],
            })
            reason = result.get("stopReason")
            if not isinstance(reason, str) or reason not in {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}:
                raise DSHProtocolError("DeepSeek Harness 未返回有效完成状态")
            if self._malformed or self._tools.failed:
                raise DSHProtocolError("DeepSeek Harness 返回了无效会话事件")
            if reason == "end_turn" and self._known_tools != self._finished_tools:
                raise DSHProtocolError("DeepSeek Harness 工具执行结果不完整")
            return {"text": self._text, "tool_events": deepcopy(self._tools.events),
                    "stopped": reason != "end_turn", "native_session_id": self.native_session_id}
        except BaseException:
            self._broken = True
            raise
        finally:
            self._running = False

    async def cancel(self) -> None:
        if self.native_session_id is None:
            return
        try:
            await self.rpc.send({"jsonrpc": "2.0", "method": "session/cancel",
                                 "params": {"sessionId": self.native_session_id}})
        except Exception:
            raise DSHProtocolError("DeepSeek Harness 取消请求失败") from None

    async def drop(self) -> None:
        """Unload and purge one native session, or require a process reset.

        ACP close flushes logs but does not delete them. The pinned profile
        disables the optional asynchronous projection cache; unknown shared
        artifacts fail closed so the owner can discard the whole native home.
        """
        if self._running:
            raise DSHProtocolError("DeepSeek Harness 会话正在执行，不能删除")
        self.rpc.on_event = self._on_event
        if self._dropped:
            return
        identity = self.native_session_id
        self._broken = True
        if identity is None:
            self._dropped = True
            return
        try:
            if not self._native_closed:
                if await self._request("session/close", {"sessionId": identity}) != {}:
                    raise DSHProtocolError("DeepSeek Harness 未确认会话关闭")
                self._native_closed = True
                self._started = False
            await self._purge_state(identity)
        except Exception:
            raise DSHProtocolError("DeepSeek Harness 会话清理未完成，需要重置进程") from None
        self._dropped = True
        self.native_session_id = None
        self._reset_turn()

    def _open_state_root(self, stack: ExitStack) -> int:
        root = self.state_root
        if root is None or not root.is_absolute() or ".." in root.parts:
            raise ValueError("Native state root is invalid")
        # System ancestors need not belong to the service account. Never
        # follow a symlink, including when reopening after an awaited lock.
        ancestor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, ancestor)
        for part in root.parts[1:]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=ancestor)
            stack.callback(os.close, current)
            ancestor = current
        info = os.fstat(ancestor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Native state root is not private")
        if self._state_identity is not None and (info.st_dev, info.st_ino) != self._state_identity:
            raise ValueError("Native state root was replaced")
        return ancestor

    def _bind_state_root(self) -> None:
        if self.state_root is None:  # A transport-only fake has no disk state.
            return
        try:
            with ExitStack() as stack:
                info = os.fstat(self._open_state_root(stack))
                self._state_identity = (info.st_dev, info.st_ino)
        except (OSError, ValueError):
            raise DSHProtocolError("DeepSeek Harness 状态目录无效") from None

    async def _purge_state(self, identity: str) -> None:
        root = self.state_root
        if (root is None or self._state_identity is None
                or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", identity)
                or not shutil.rmtree.avoids_symlink_attacks):
            raise ValueError("Native state cannot be safely identified")
        # Open each ancestor without following symlinks. All subsequent state
        # operations are relative to these FDs, including the final rmtree.
        with ExitStack() as stack:
            def directory(name, parent=None, *, private=False):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or (private and stat.S_IMODE(info.st_mode) != 0o700) or info.st_mode & 0o022:
                    raise ValueError("Native state directory is not owner controlled")
                return fd

            root_fd = self._open_state_root(stack)
            with os.fdopen(os.open("joyagent.patch.yml", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)) as source:
                patches = json.loads(source.read(256 * 1024 + 1))
            cache = [item for item in patches if isinstance(item, dict) and item.get("id") == "session-projection-cache"]
            spill = [item for item in patches if isinstance(item, dict) and item.get("id") == "spill-local"]
            if (cache != [{"id": "session-projection-cache", "disabled": True}]
                    or spill != [{"id": "spill-local", "config": {"root": str(root / "spill"), "cleanupPeriodDays": 0}}]):
                raise ValueError("Native profile has no per-session cleanup barrier")
            for name in ("attachments", "cache", "logs", "storages"):
                fd = directory(name, root_fd)
                if os.listdir(fd):
                    raise ValueError("Shared native artifacts require a process reset")
            sessions = directory("sessions", root_fd)
            projects = os.listdir(sessions)
            if len(projects) > 256:
                raise ValueError("Native state exceeds cleanup limit")
            targets, known, entries = [], set(), 0
            for project in projects:
                project_fd = directory(project, sessions)
                names = os.listdir(project_fd)
                entries += len(names)
                if entries > 4096:
                    raise ValueError("Native state exceeds cleanup limit")
                known.update(names)
                if identity in names:
                    target_fd = directory(identity, project_fd)
                    lock_fd = os.open("session.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=target_fd)
                    stack.callback(os.close, lock_fd)
                    lock_info = os.fstat(lock_fd)
                    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid() or lock_info.st_nlink != 1:
                        raise ValueError("Native session lock is invalid")
                    targets.append((project_fd, lock_fd))
            if len(targets) > 1:
                raise ValueError("Native session identity is ambiguous")
            digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
            if any(name != identity and hashlib.sha256(name.encode()).hexdigest()[:12] == digest for name in known):
                raise ValueError("Native spill identity is ambiguous")
            spill_fd = directory("spill", root_fd)
            spill_name = "session-" + digest
            try:
                directory(spill_name, spill_fd)
            except FileNotFoundError:
                has_spill = False
            else:
                has_spill = True
            # The JSONL backend asynchronously releases its writer after ACP
            # close. Its kernel lock is the final write-drain barrier.
            deadline = asyncio.get_running_loop().time() + 1
            for _, lock_fd in targets:
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise ValueError("Native log writer is still active") from None
                        await asyncio.sleep(.02)
            with ExitStack() as check:
                self._open_state_root(check)
            for project_fd, _ in targets:
                shutil.rmtree(identity, dir_fd=project_fd)
            if has_spill:
                shutil.rmtree(spill_name, dir_fd=spill_fd)
            # A valid replacement directory must not hide the original state
            # and let the owner acknowledge deletion of the wrong native home.
            with ExitStack() as check:
                self._open_state_root(check)

    async def _on_event(self, record) -> None:
        if not isinstance(record, dict):
            self._malformed = True
            return
        if "id" in record and isinstance(record.get("method"), str):
            # Never forward client-side filesystem or permission operations to
            # the host. Only the namespace's preauthorized native tools may act.
            if record["method"] == "session/request_permission":
                response = {"result": {"outcome": {"outcome": "cancelled"}}}
            else:
                response = {"error": {"code": -32601, "message": "Client operation is unavailable"}}
            await self.rpc.send({"jsonrpc": "2.0", "id": record["id"], **response})
            return
        if not self._running or record.get("method") != "session/update":
            return
        params = record.get("params")
        if not isinstance(params, dict):
            self._malformed = True
            return
        if params.get("sessionId") != self.native_session_id:
            return
        update = params.get("update")
        if not isinstance(update, dict):
            self._malformed = True
            return
        try:
            self._update(update)
        except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
            self._malformed = True

    def _update(self, update: dict) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content, identity = update.get("content"), update.get("messageId")
            if not isinstance(content, dict) or content.get("type") != "text":
                return
            if (not isinstance(identity, str) or not identity or len(identity) > 256
                    or not isinstance(content.get("text"), str)):
                self._malformed = True
                return
            if self._message_id != identity:
                self._message_id, self._text = identity, ""
            self._text += content["text"][:max(0, self.output_limit - len(self._text))]
        elif kind in {"tool_call", "tool_call_update"}:
            identity = update.get("toolCallId")
            if not isinstance(identity, str) or not identity or len(identity) > 256:
                self._malformed = True
                return
            if kind == "tool_call":
                if identity in self._known_tools:
                    return
                if len(self._known_tools) >= 64:
                    self._tool_overflow = True
                    return
                name = update.get("title")
                if not isinstance(name, str) or not name or update.get("status") not in {"pending", "in_progress"}:
                    self._malformed = True
                    return
                self._known_tools.add(identity)
                self._tools.feed({"type": "tool_call", "callId": identity, "tool": name})
                return
            status = update.get("status")
            if status in {"pending", "in_progress"}:
                return
            if identity in self._finished_tools:
                return
            if identity not in self._known_tools:
                self._malformed |= not self._tool_overflow
                return
            if status not in {"completed", "failed"}:
                self._malformed = True
                return
            content = update.get("content", [])
            if not isinstance(content, list):
                self._malformed = True
                return
            pieces, remaining = [], self.output_limit
            for item in content[:64]:
                block = item.get("content") if isinstance(item, dict) and item.get("type") == "content" else None
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    text = block["text"][:remaining]
                    pieces.append(text)
                    remaining -= len(text)
            self._finished_tools.add(identity)
            before = len(self._tools.events)
            self._tools.feed({"type": "tool_result", "callId": identity,
                              "status": "completed" if status == "completed" else "error", "result": "".join(pieces)})
            if len(self._tools.events) > before:
                self.publish(deepcopy(self._tools.events[-1]))
