"""Private JSONL transport for the optional pi-tui terminal frontend.

The bridge is a channel, not another agent loop. stdout contains protocol JSON
only; model calls, tools, persistence and cancellation stay in the shared Agent.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterable, Callable
from concurrent.futures import CancelledError as FutureCancelled, TimeoutError as FutureTimeout
from contextlib import redirect_stdout
import json
import os
import re
import signal
import sys
import threading
from typing import Any, BinaryIO, TextIO

from .common import validate_input
from ..config import Config, load_config
from ..runtime import build_runtime

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 128 * 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_METHODS = {"chat", "history", "cancel", "shutdown"}


def _reject_constant(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _request(line: bytes) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Validate an envelope without reflecting invalid JSON or credentials."""
    if len(line) > MAX_LINE_BYTES:
        return None, None, "请求行不能超过 128 KiB"
    try:
        value = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, None, "请求必须是有效的 UTF-8 JSON 对象"
    if not isinstance(value, dict):
        return None, None, "请求必须是 JSON 对象"
    request_id = value.get("id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        return None, None, "请求 id 须为 1–128 位字母、数字、点、冒号、下划线或连字符"
    method = value.get("method")
    params = value.get("params", {})
    if (set(value) - {"id", "method", "params"} or not isinstance(method, str)
            or method not in _METHODS or not isinstance(params, dict)):
        return request_id, None, "请求方法或参数格式不正确"
    allowed = {"text", "session"} if method == "chat" else {"session"} if method == "history" else set()
    if set(params) - allowed:
        return request_id, None, "请求包含不支持的参数"
    try:
        if method in {"chat", "history"}:
            text, session = validate_input(params.get("text") if method == "chat" else "history",
                                           params.get("session", "default"))
            params = {"session": session, **({"text": text} if method == "chat" else {})}
    except ValueError as exc:
        return request_id, None, str(exc)
    return request_id, {"method": method, "params": params}, None


class _Bridge:
    def __init__(self, runtime: Any, emit: Callable[[dict[str, Any]], None]):
        self.runtime = runtime
        self.emit = emit
        self.turn: asyncio.Task[None] | None = None
        self.turn_id: str | None = None

    def success(self, request_id: str, result: dict[str, Any]) -> None:
        self.emit({"id": request_id, "ok": True, "result": result})

    def error(self, request_id: str | None, message: str) -> None:
        self.emit({"id": request_id, "ok": False, "error": message})

    async def chat(self, request_id: str, text: str, session: str) -> None:
        try:
            result = await self.runtime.chat(text, session_id=session)
            response = {"id": request_id, "ok": True, "result": {
                "text": result.text, "steps": result.steps, "stopped": result.stopped,
                "tool_events": result.tool_events,
            }}
        except asyncio.CancelledError:
            response = {"id": request_id, "ok": True, "result": {
                "text": "本轮处理被取消。", "steps": 0, "stopped": True,
                "tool_events": [], "cancelled": True,
            }}
        except Exception:
            # An adapter/transport exception may contain a URL or credential.
            response = {"id": request_id, "ok": False, "error": "本轮处理失败，请检查本地配置。"}
        self.emit(response)

    async def cancel(self) -> bool:
        if self.turn is None or self.turn.done():
            if self.turn is not None:
                await self.turn  # Retrieve a failed output write, if any.
            return False
        self.turn.cancel()
        await self.turn
        return True

    async def run(self, lines: AsyncIterable[bytes]) -> str | None:
        async for line in lines:
            request_id, request, error = _request(line)
            if error is not None:
                self.error(request_id, error)
                continue
            assert request is not None and request_id is not None
            method, params = request["method"], request["params"]
            busy = self.turn is not None and not self.turn.done()
            if busy and request_id == self.turn_id:
                self.error(None, "请求 id 与正在处理的请求重复")
                continue
            if method in {"chat", "history"} and busy:
                self.error(request_id, "正在处理上一轮消息，请等待完成或先取消。")
                continue
            if method == "chat":
                self.turn_id = request_id
                self.turn = asyncio.create_task(self.chat(request_id, params["text"], params["session"]))
                # Start the task before accepting cancel, even when input is buffered.
                await asyncio.sleep(0)
            elif method == "history":
                try:
                    messages = self.runtime.memory.history(params["session"], limit=80)
                    result = [{"role": message.role, "content": message.content} for message in messages
                              if message.role in {"user", "assistant"} and message.content
                              and not message.tool_calls]
                except Exception:
                    self.error(request_id, "无法读取会话记录，请检查本地数据目录。")
                else:
                    self.success(request_id, {"session": params["session"], "messages": result})
            elif method == "cancel":
                self.success(request_id, {"cancelled": await self.cancel()})
            else:
                await self.cancel()
                return request_id
        return None


async def run_bridge(config: Config, lines: AsyncIterable[bytes],
                     emit: Callable[[dict[str, Any]], None]) -> int:
    """Run one runtime and close it after EOF, shutdown, or channel failure."""
    try:
        runtime = await build_runtime(config)
    except Exception:
        emit({"event": "error", "error": "初始化失败，请运行 joyagent doctor 检查本地配置。"})
        return 1
    bridge = _Bridge(runtime, emit)
    try:
        emit({"event": "ready", "protocol": PROTOCOL_VERSION,
              "provider": config.get("model", "provider", "demo"),
              "model": config.get("model", "model", "demo")})
        shutdown_id = await bridge.run(lines)
    finally:
        async def cleanup() -> None:
            try:
                await bridge.cancel()
            finally:
                await runtime.close()

        # SIGTERM may arrive during ordinary shutdown. Keep the actual cleanup
        # in a shielded task so cancellation cannot abandon an MCP process.
        closing = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise
    # Acknowledgment means MCP clients and SQLite have finished closing. The
    # frontend can now terminate the bridge without interrupting child cleanup.
    if shutdown_id is not None:
        bridge.success(shutdown_id, {"shutdown": True})
    return 0


async def _pipe_lines(source: BinaryIO) -> AsyncIterable[bytes]:
    """Read bounded lines without putting a blocking stdin read in the executor.

    A daemon reader works with POSIX and Windows child-process pipes. Its queue
    is bounded for backpressure. It cannot hold asyncio.run open on shutdown;
    the owning frontend closes stdin when it receives the shutdown response.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
    stopped = threading.Event()

    def deliver(value: bytes | None) -> bool:
        if stopped.is_set():
            return False
        pending = queue.put(value)
        try:
            future = asyncio.run_coroutine_threadsafe(pending, loop)
        except RuntimeError:
            pending.close()
            return False
        while not stopped.is_set():
            try:
                future.result(timeout=0.1)
                return True
            except FutureTimeout:
                pass
            except FutureCancelled:
                return False
        future.cancel()
        return False

    def read() -> None:
        try:
            while not stopped.is_set():
                line = source.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if not deliver(line):
                    return
                # Only one rejection for an oversized logical line. Never grow
                # an unbounded buffer while discarding the remaining fragments.
                if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                    while not stopped.is_set():
                        fragment = source.readline(MAX_LINE_BYTES + 1)
                        if not fragment or fragment.endswith(b"\n"):
                            break
        except (OSError, ValueError):
            pass
        finally:
            deliver(None)

    reader = threading.Thread(target=read, name="joyagent-tui-input", daemon=True)
    reader.start()
    try:
        while True:
            line = await queue.get()
            if line is None:
                break
            yield line
    finally:
        stopped.set()


def _emitter(output: TextIO) -> Callable[[dict[str, Any]], None]:
    def emit(value: dict[str, Any]) -> None:
        output.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
        output.flush()
    return emit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="joyagent pi-tui JSONL bridge")
    parser.add_argument("--config", default="agent.toml")
    args = parser.parse_args(argv)
    emit = _emitter(sys.stdout)

    async def run() -> int:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        terminating = False

        def terminate() -> None:
            nonlocal terminating
            if not terminating:
                terminating = True
                # Only cancel once: a second TERM must not interrupt the
                # BaseException cleanup in build_runtime / MCPClient.start.
                assert task is not None
                task.cancel()

        if os.name == "posix":
            loop.add_signal_handler(signal.SIGTERM, terminate)
        try:
            # A daemon must not hold sys.stdin's BufferedReader lock during
            # Python finalization. A dedicated unbuffered descriptor avoids
            # that lock while the frontend still has its pipe open.
            with os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0) as source:
                lines = _pipe_lines(source)
                try:
                    return await run_bridge(load_config(args.config), lines, emit)
                finally:
                    await lines.aclose()
        except asyncio.CancelledError:
            if terminating:
                return 143
            raise
        finally:
            if os.name == "posix":
                loop.remove_signal_handler(signal.SIGTERM)

    # Reserve original stdout for the protocol, including during runtime setup.
    with redirect_stdout(sys.stderr):
        try:
            return asyncio.run(run())
        except (BrokenPipeError, ConnectionResetError):
            return 0
        except KeyboardInterrupt:
            return 130
        except Exception:
            emit({"event": "error", "error": "终端通道无法运行，请检查本地配置和数据目录。"})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
