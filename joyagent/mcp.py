"""MCP stdio transport: JSON-RPC messages delimited by newlines, no shell."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
from typing import Any

from .types import ToolSpec

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
# Enough OS state for Python/Node/package runners, without model or channel keys.
# This is environment hygiene, not a sandbox: configured programs run as this user.
SAFE_ENV_KEYS = {
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
    "LC_CTYPE", "TZ", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SSL_CERT_FILE", "SSL_CERT_DIR",
}


class MCPError(RuntimeError):
    """Protocol or transport failure, without echoing server logs or secrets."""


class MCPClient:
    def __init__(self, name: str, settings: dict[str, Any], *,
                 launch: tuple[list[str], dict[str, str]] | None = None):
        self.name = name
        self.settings = settings
        # Only the composition root supplies this trusted sandbox launch plan.
        self._launch = launch
        timeout = settings.get("timeout_seconds", 30)
        if type(timeout) not in (int, float):
            raise ValueError("MCP timeout_seconds 必须为数字。")
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("MCP timeout_seconds 必须为正数。")
        command, args, env = settings.get("command"), settings.get("args", []), settings.get("env", {})
        if not isinstance(command, str) or not command:
            raise ValueError("MCP command 必须是可执行文件名。")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("MCP args 必须是字符串列表。")
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError("MCP env 必须是字符串字典。")
        pass_env = settings.get("pass_env", [])
        if not isinstance(pass_env, list) or not all(isinstance(key, str) and key for key in pass_env):
            raise ValueError("MCP pass_env 必须是非空环境变量名称的列表。")
        self.process: asyncio.subprocess.Process | None = None
        self.protocol_version: str | None = None
        self.capabilities: dict[str, Any] = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._counter = 0
        self._ready = False
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._ready and self.process and self.process.returncode is None:
                return
            await self.close()
            allowed_env = SAFE_ENV_KEYS | set(self.settings.get("pass_env", []))
            env = {key: value for key, value in os.environ.items() if key in allowed_env}
            env.update({key: os.path.expandvars(value) for key, value in self.settings.get("env", {}).items()})
            command = [self.settings["command"], *self.settings.get("args", [])]
            if self._launch is not None:
                command, env = self._launch
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=env, limit=MAX_MESSAGE_BYTES + 1,
                    cwd="/" if self._launch is not None else None,
                    start_new_session=os.name == "posix",
                )
            except (OSError, ValueError):
                raise MCPError(f"MCP {self.name} 启动失败，请检查 command、args 和环境。") from None
            self._reader = asyncio.create_task(self._read_messages())
            self._stderr = asyncio.create_task(self._drain_stderr())
            try:
                info = await self._request("initialize", {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "joyagent", "version": "0.1.0"},
                })
                if not isinstance(info, dict) or info.get("protocolVersion") not in SUPPORTED_VERSIONS:
                    raise MCPError(f"MCP {self.name} 协议版本不受支持。")
                self.protocol_version = info["protocolVersion"]
                self.capabilities = info.get("capabilities", {})
                if not isinstance(self.capabilities, dict):
                    raise MCPError(f"MCP {self.name} capabilities 格式无效。")
                await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self._ready = True
            except BaseException:
                await self.close()
                raise

    async def _write(self, message: dict) -> None:
        raw = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MCPError(f"MCP {self.name} 请求超过 4 MiB 限制。")
        async with self._write_lock:
            if not self.process or self.process.returncode is not None or not self.process.stdin:
                raise MCPError(f"MCP {self.name} 进程未运行。")
            try:
                self.process.stdin.write(raw)
                await asyncio.wait_for(self.process.stdin.drain(), self.timeout)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                raise MCPError(f"MCP {self.name} 连接已关闭或写入超时。") from None

    async def _request(self, method: str, params: dict) -> Any:
        self._counter += 1
        request_id = self._counter
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            try:
                await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            except MCPError:
                await self.close()
                raise
            return await asyncio.wait_for(future, self.timeout)
        except TimeoutError:
            await self.close()
            raise MCPError(f"MCP {self.name} 请求超时；服务器进程已关闭。") from None
        except asyncio.CancelledError:
            await self.close()
            raise
        except MCPError:
            # A failed connection must never leave a server running in the background.
            if self._reader and self._reader.done():
                await self.close()
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a close/error result if writing failed first.

    def _fail_pending(self, message: str) -> None:
        self._ready = False
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(MCPError(message))

    async def _read_messages(self) -> None:
        process = self.process
        assert process and process.stdout
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    raise MCPError(f"MCP {self.name} 服务器已退出或关闭输出。")
                if len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
                    raise MCPError(f"MCP {self.name} 消息过长或缺少换行分隔符。")
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise MCPError(f"MCP {self.name} stdout 包含无效 JSON；日志应写入 stderr。") from None
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise MCPError(f"MCP {self.name} 返回无效 JSON-RPC 消息。")
                if "method" in message:
                    if "id" in message:
                        # The client advertises no sampling/elicitation capability.
                        response = {"jsonrpc": "2.0", "id": message["id"]}
                        if message["method"] == "ping":
                            response["result"] = {}
                        else:
                            response["error"] = {"code": -32601, "message": "Method not supported"}
                        await self._write(response)
                    continue  # Notifications are informational, never executable instructions.
                response_id = message.get("id")
                if not isinstance(response_id, (str, int)) or isinstance(response_id, bool):
                    raise MCPError(f"MCP {self.name} 响应缺少有效 ID。")
                future = self._pending.get(response_id)
                if future is None or future.done():
                    continue  # A late/unknown response cannot complete another request.
                if ("error" in message) == ("result" in message):
                    raise MCPError(f"MCP {self.name} 响应必须包含 result 或 error。")
                if "error" in message:
                    error = message["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    safe_code = str(code) if isinstance(code, int) else "unknown"
                    future.set_exception(MCPError(f"MCP {self.name} 返回协议错误 {safe_code}。"))
                else:
                    future.set_result(message["result"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(str(exc) if isinstance(exc, MCPError) else f"MCP {self.name} 传输或消息格式错误。")
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()

    async def _drain_stderr(self) -> None:
        process = self.process
        assert process and process.stderr
        # Drain without retaining secrets or allocating unbounded server logs.
        while await process.stderr.read(8192):
            pass

    async def list_tools(self) -> list[ToolSpec]:
        await self.start()
        if "tools" not in self.capabilities:
            return []
        tools = []
        names: set[str] = set()
        cursors: set[str] = set()
        params = {}
        for _ in range(100):
            result = await self._request("tools/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise MCPError(f"MCP {self.name} tools/list 格式无效。")
            for item in result["tools"]:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                        or not item["name"] or not isinstance(item.get("inputSchema"), dict)
                        or not isinstance(item.get("description", ""), str)):
                    raise MCPError(f"MCP {self.name} 工具定义格式无效。")
                if item["name"] in names:
                    raise MCPError(f"MCP {self.name} 返回重复的工具名。")
                names.add(item["name"])
                tools.append(ToolSpec(item["name"], item.get("description", ""), item["inputSchema"]))
                if len(tools) > 1000:
                    raise MCPError(f"MCP {self.name} 工具数量超过 1000 限制。")
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or cursor in cursors:
                raise MCPError(f"MCP {self.name} 返回无效或循环的分页 cursor。")
            cursors.add(cursor)
            params = {"cursor": cursor}
        raise MCPError(f"MCP {self.name} 工具分页超过 100 页限制。")

    async def call_tool(self, name: str, arguments: dict) -> Any:
        await self.start()
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise ValueError("MCP 工具名必须为字符串，arguments 必须为字典。")
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError(f"MCP {self.name} tools/call 格式无效。")
        # Preserve structuredContent, content, and isError for the caller/agent.
        return result

    async def close(self) -> None:
        async with self._close_lock:
            self._ready = False
            self._fail_pending(f"MCP {self.name} 连接已关闭。")
            process = self.process
            if process:
                if process.stdin:
                    process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 0.2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 0.5)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            if os.name == "posix":
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                        await process.wait()
            current = asyncio.current_task()
            for task in (self._reader, self._stderr):
                if task and task is not current:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, OSError):
                        await task
            self.process = None
            self._reader = self._stderr = None
