"""Per-turn Unix socket model relay. Credentials never enter CLI namespaces."""
from __future__ import annotations

import hashlib
import hmac
import json
from http.server import BaseHTTPRequestHandler
import logging
import os
from pathlib import Path
import socket
import socketserver
import threading
import time
import secrets
from typing import Any

from .providers import HTTPProvider, _post_json, _reasoning_content
from .responses_bridge import ResponsesBridgeError
from .types import ProviderError


MAX_BODY = 1024 * 1024
GLOBAL_CONNECTION_LIMIT = 32
GLOBAL_MODEL_LIMIT = 8
MAX_REPLAY_BYTES = 512 * 1024
MAX_REPLAY_ITEMS = 64
_GLOBAL_CONNECTIONS = threading.BoundedSemaphore(GLOBAL_CONNECTION_LIMIT)
_GLOBAL_MODEL_REQUESTS = threading.BoundedSemaphore(GLOBAL_MODEL_LIMIT)
_LOG = logging.getLogger(__name__)
_FAILURE_CATEGORIES = frozenset({
    "dns", "tls", "network", "timeout", "auth", "quota", "rate_limit", "busy", "budget",
    "configuration", "upstream", "http", "credentials", "response",
})
_FAILURE_SOURCES = frozenset({"local_concurrency", "credentials", "upstream", "protocol", "budget"})


def _assistant_signature(message: dict) -> str:
    """Match the assistant content and every call, never just an untrusted ID."""
    from .responses_bridge import _content
    try:
        content = message.get("content")
        content = "" if content is None else _content(content)
        calls = []
        for call in message.get("tool_calls") or []:
            function = call["function"]
            calls.append({"id": call["id"], "name": function["name"],
                          "arguments": json.loads(function["arguments"])})
        raw = json.dumps({"content": content, "calls": calls}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ProviderError("无法匹配当前轮的模型续接记录。", category="response") from None


def chat_sse(completion: dict) -> bytes:
    message = completion["choices"][0]["message"]
    common = {"id": completion.get("id", "joyagent-chat"), "object": "chat.completion.chunk",
              "created": int(time.time()), "model": completion.get("model", "joyagent")}
    chunks = []
    def add(delta: dict, finish: Any = None, usage: Any = None) -> None:
        data = {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage is not None:
            data["usage"] = usage
        chunks.append("data: " + json.dumps(data, ensure_ascii=False) + "\n\n")
    add({"role": "assistant"})
    if message.get("reasoning_content"):
        add({"reasoning_content": message["reasoning_content"]})
    if message.get("content"):
        add({"content": message["content"]})
    if message.get("tool_calls"):
        add({"tool_calls": [{**call, "index": index} for index, call in enumerate(message["tool_calls"])]})
    add({}, completion["choices"][0].get("finish_reason") or ("tool_calls" if message.get("tool_calls") else "stop"), completion.get("usage"))
    return ("".join(chunks) + "data: [DONE]\n\n").encode()


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(4)
        self.connection_lock = threading.Lock()
        self.connections = set()
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        if not _GLOBAL_CONNECTIONS.acquire(blocking=False):
            self.slots.release()
            self.shutdown_request(request)
            return
        with self.connection_lock:
            self.connections.add(request)
        try:
            super().process_request(request, address)
        except BaseException:
            with self.connection_lock:
                self.connections.discard(request)
            _GLOBAL_CONNECTIONS.release()
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self.connection_lock:
                self.connections.discard(request)
            _GLOBAL_CONNECTIONS.release()
            self.slots.release()

    def close_connections(self):
        with self.connection_lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def handle_error(self, request, address):
        # Never log upstream bodies or request input from an untrusted process.
        pass


class ModelBroker:
    def __init__(self, path: Path, provider: HTTPProvider, *, max_requests: int,
                 max_tool_calls: int, timeout: float, transport: Any = None):
        if not isinstance(provider, HTTPProvider) or provider.protocol != "chat":
            raise ValueError("外部 Agent 当前需要 OpenAI Chat 兼容模型配置")
        self.path, self.provider = path, provider
        self.max_requests, self.max_tool_calls = max_requests, max_tool_calls
        self.deadline = time.monotonic() + timeout
        self.transport = transport or _post_json
        self.calls = self.tool_calls = 0
        self.failure: str | None = None
        self._failure_logged = False
        self.active = True
        self.closed = False
        self.lease_required = False
        self.lease = None
        # Codex sees only neutral Responses items. Keep required DeepSeek
        # continuation on the host, isolated per native conversation when a
        # resident CLI process serves several chats.
        self._replay_enabled = provider.deepseek_tool_reasoning
        self._replay_identity = (provider.kind, provider.base_url, provider.model)
        self._reasoning_replay: dict[str, str | None] = {}
        self._replay_bytes = 0
        self._replay_session = None
        self._session_replays = {}
        self.lock = threading.Lock()
        self.inflight = threading.Lock()
        self.server = _Server(str(path), _Handler)
        self.server.broker = self
        os.chmod(path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def select_session(self, session_id: str) -> None:
        """Switch private continuation state only while the broker is idle."""
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("Invalid native session")
        if not self.inflight.acquire(blocking=False):
            raise RuntimeError("Previous model request is still running")
        try:
            with self.lock:
                if self.closed or self.active:
                    raise RuntimeError("Cannot switch an active or closed broker")
                if session_id == self._replay_session:
                    return
                if self._replay_session is not None:
                    self._session_replays[self._replay_session] = (
                        self._replay_identity, self._reasoning_replay, self._replay_bytes)
                identity, replay, size = self._session_replays.pop(
                    session_id, (self._replay_identity, {}, 0))
                self._replay_session = session_id
                self._replay_identity, self._reasoning_replay, self._replay_bytes = identity, replay, size
        finally:
            self.inflight.release()

    def forget_session(self, session_id: str) -> None:
        if not self.inflight.acquire(blocking=False):
            raise RuntimeError("Previous model request is still running")
        try:
            with self.lock:
                if self.active:
                    raise RuntimeError("Cannot forget an active broker session")
                self._session_replays.pop(session_id, None)
                if self._replay_session == session_id:
                    self._reasoning_replay.clear()
                    self._replay_bytes = 0
                    self._replay_session = None
        finally:
            self.inflight.release()

    def session_usage(self, session_id: str) -> tuple[int, int]:
        with self.lock:
            if self._replay_session == session_id:
                return len(self._reasoning_replay), self._replay_bytes
            _, replay, size = self._session_replays.get(session_id, (None, {}, 0))
            return len(replay), size

    def activate(self, provider: HTTPProvider, *, max_requests: int, max_tool_calls: int, timeout: float) -> str:
        """Grant one fresh turn; never rebind a request still running upstream."""
        if not isinstance(provider, HTTPProvider) or provider.protocol != "chat":
            raise ValueError("Resident Agent requires a Chat-compatible provider")
        if not self.inflight.acquire(blocking=False):
            raise RuntimeError("Previous model request is still running")
        try:
            with self.lock:
                if self.closed:
                    raise RuntimeError("Model broker has been closed")
                if self.active:
                    raise RuntimeError("Previous Agent turn is still active")
                identity = (provider.kind, provider.base_url, provider.model)
                if identity != self._replay_identity:
                    self._reasoning_replay.clear()
                    self._replay_bytes = 0
                self._replay_identity = identity
                self._replay_enabled = provider.deepseek_tool_reasoning
                self.provider = provider
                self.max_requests, self.max_tool_calls = max_requests, max_tool_calls
                self.deadline = time.monotonic() + timeout
                self.calls = self.tool_calls = 0
                self.failure = None
                self._failure_logged = False
                self.lease_required = True
                self.lease = secrets.token_hex(32)
                self.active = True
                return self.lease
        finally:
            self.inflight.release()

    def deactivate(self) -> None:
        # Revoke before checking inflight so a delayed old request cannot start.
        with self.lock:
            if self.closed:
                return
            self.active = False
            self.lease = None
        if not self.inflight.acquire(blocking=False):
            raise RuntimeError("Agent completed with a model request still running")
        self.inflight.release()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.active = False
            self.lease = None
            self._reasoning_replay.clear()
            self._replay_bytes = 0
            self._session_replays.clear()
            self._replay_session = None
        self.server.shutdown()
        self.server.close_connections()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)

    def complete(self, path: str, body: dict, *, lease: str | None = None) -> tuple[str, bytes]:
        if not self.inflight.acquire(blocking=False):
            raise ValueError("本轮已有模型请求在处理")
        try:
            with self.lock:
                if self.lease_required and (not isinstance(lease, str) or not self.lease
                        or not hmac.compare_digest(lease, self.lease)):
                    raise ValueError("Agent turn lease has expired")
            try:
                return self._complete(path, body)
            except ResponsesBridgeError:
                # A rejected protocol reply must reach the runtime as failure,
                # not leave its last progress sentence looking like a result.
                # Log only fixed categories, never tool arguments or text.
                with self.lock:
                    self._record_failure_locked("response", "protocol")
                raise ProviderError("模型工具协议不兼容。", category="response") from None
        finally:
            self.inflight.release()

    def _record_failure_locked(self, category: object, source: object) -> None:
        """Called with self.lock; never log request content or exception text."""
        self.failure = category if type(category) is str and category in _FAILURE_CATEGORIES else "upstream"
        if not self._failure_logged:
            self._failure_logged = True
            safe_source = source if type(source) is str and source in _FAILURE_SOURCES else "protocol"
            _LOG.warning("Native model request failed: category=%s source=%s", self.failure, safe_source)

    def _restore_reasoning(self, request: dict) -> None:
        """Called with the broker lock; no state from another model/turn is valid."""
        if (self.provider.kind, self.provider.base_url, self.provider.model) != self._replay_identity:
            raise ProviderError("本轮模型配置发生变化，无法继续请求。", category="configuration")
        if not request.get("tools"):
            return
        for message in request["messages"]:
            if message.get("role") != "assistant":
                continue
            signature = _assistant_signature(message)
            if signature not in self._reasoning_replay:
                raise ProviderError("模型续接记录不属于当前轮，无法继续工具调用。", category="response")
            reasoning = self._reasoning_replay[signature]
            if reasoning is not None:
                message["reasoning_content"] = reasoning

    def _remember_reasoning(self, message: dict) -> None:
        """Called after bridge validation, under the lock; never truncate state."""
        signature = _assistant_signature(message)
        value = message.get("reasoning_content")
        reasoning = None if value is None else _reasoning_content(value)
        if signature in self._reasoning_replay:
            if self._reasoning_replay[signature] != reasoning:
                raise ProviderError("模型续接记录发生冲突。", category="response")
            return
        size = len(reasoning.encode("utf-8")) if reasoning is not None else 0
        if len(self._reasoning_replay) >= MAX_REPLAY_ITEMS or self._replay_bytes + size > MAX_REPLAY_BYTES:
            raise ProviderError("本轮模型续接内容达到上限，请拆分任务。", category="response")
        self._reasoning_replay[signature] = reasoning
        self._replay_bytes += size

    def _complete(self, path: str, body: dict) -> tuple[str, bytes]:
        if path not in {"/v1/chat/completions", "/v1/responses"}:
            raise ValueError("不支持的模型路由")
        responses = path == "/v1/responses"
        if responses:
            from .responses_bridge import to_chat_request
            request = to_chat_request(body, model=self.provider.model)
        else:
            if not isinstance(body.get("messages"), list) or not body["messages"]:
                raise ValueError("模型请求缺少 messages")
            allowed = {"messages", "tools", "tool_choice", "temperature", "top_p", "parallel_tool_calls"}
            request = {key: value for key, value in body.items() if key in allowed}
        request.update({"model": self.provider.model, "stream": False,
                        self.provider.token_parameter: self.provider.max_tokens})
        request.pop("max_output_tokens", None)
        if self.provider.token_parameter != "max_tokens":
            request.pop("max_tokens", None)
        if self.provider.token_parameter != "max_completion_tokens":
            request.pop("max_completion_tokens", None)
        if self.provider._thinking_body:
            request["thinking"] = self.provider._thinking_body
        if request.get("tools") and "tool_choice" not in request:
            request["tool_choice"] = "auto"
        with self.lock:
            if not self.active or time.monotonic() >= self.deadline:
                raise ValueError("本轮 Agent 已结束")
            if responses and self._replay_enabled:
                try:
                    self._restore_reasoning(request)
                except ProviderError as exc:
                    self._record_failure_locked(exc.category or "response", "protocol")
                    raise
            if self.calls >= self.max_requests:
                self._record_failure_locked("budget", "budget")
                raise ValueError("已达到本轮模型调用上限")
            self.calls += 1
        failure_source = "local_concurrency"
        try:
            if not _GLOBAL_MODEL_REQUESTS.acquire(blocking=False):
                raise ProviderError("本地模型请求通道繁忙，请稍后重试", category="busy")
            try:
                # Cancellation closes the local connection, but urllib may
                # still be waiting upstream. Keep this global slot until the
                # transport really returns; close() must never release it.
                with self.lock:
                    if not self.active or time.monotonic() >= self.deadline:
                        raise ValueError("本轮 Agent 已结束")
                failure_source = "credentials"
                try:
                    key, _ = self.provider.credential()
                except (ValueError, OSError):
                    raise ProviderError("模型凭据不可用", category="credentials") from None
                if not key and self.provider.requires_api_key:
                    raise ProviderError("缺少模型凭据", category="credentials")
                failure_source = "upstream"
                completion = self.transport(self.provider.base_url + "/chat/completions", request,
                    {"Authorization": "Bearer " + key} if key else {},
                    max(.1, min(self.provider.timeout, self.deadline - time.monotonic())))
            finally:
                _GLOBAL_MODEL_REQUESTS.release()
            failure_source = "protocol"
            calls = completion["choices"][0]["message"].get("tool_calls") or []
            with self.lock:
                if not self.active or time.monotonic() >= self.deadline:
                    raise ValueError("本轮 Agent 已结束")
                if self.tool_calls + len(calls) > self.max_tool_calls:
                    self._record_failure_locked("budget", "budget")
                    raise ValueError("已达到本轮工具调用上限")
                self.tool_calls += len(calls)
            if responses:
                from .responses_bridge import from_chat_response, encode_sse
                response = from_chat_response(completion, request=body)
                if self._replay_enabled:
                    with self.lock:
                        if not self.active or time.monotonic() >= self.deadline:
                            raise ValueError("本轮 Agent 已结束")
                        self._remember_reasoning(completion["choices"][0]["message"])
                return ("text/event-stream", encode_sse(response)) if body.get("stream") else (
                    "application/json", json.dumps(response, ensure_ascii=False).encode())
            return ("text/event-stream", chat_sse(completion)) if body.get("stream") else (
                "application/json", json.dumps(completion, ensure_ascii=False).encode())
        except ProviderError as exc:
            with self.lock:
                self._record_failure_locked(exc.category or "upstream", failure_source)
            raise


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def do_POST(self) -> None:
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("无效请求长度")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                raise ValueError("请求过长")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("请求内容不完整")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("请求必须是对象")
            # Reject NaN/Infinity, including overflowing JSON number literals,
            # before spending an upstream request or handing data to a provider.
            json.dumps(body, allow_nan=False)
            if getattr(self.server.broker, "lease_required", False):
                kind, raw = self.server.broker.complete(self.path, body, lease=self.headers.get("X-JoyAgent-Lease"))
            else:
                kind, raw = self.server.broker.complete(self.path, body)
            code = 200
        except Exception:
            # Do not expose credentials, upstream response bodies or host paths.
            code, kind = 400, "application/json"
            raw = b'{"error":{"message":"Agent model request failed or reached its budget","type":"agent_gateway_error"}}'
        try:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
