"""Local HTTP UI and browser-isolated anonymous shared hosting."""

from __future__ import annotations

import asyncio
import concurrent.futures
import html
import ipaddress
import json
import os
import re
import secrets
import threading
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .common import validate_input
from .web_history import project_history, project_routing
from .web_jobs import ChatJobs, JobBusyError, JobConflictError
from .web_progress import ProgressStore
from ..progress import TurnProgress, progress_scope
from ..native_cleanup import cleanup_tasks
from ..steering import SteeringConflictError, SteeringLimitError, validate_identifier

MAX_BODY_BYTES = 65_536


def news_settings(config: Any, section: str = "news") -> tuple[bool, int]:
    settings = config.data.get(section, {})
    allowed = {"enabled", "interval_seconds"} | ({"translation_runtime"} if section == "papers" else set())
    if not isinstance(settings, dict) or set(settings) - allowed:
        raise ValueError(f"{section} 包含不支持的配置项")
    enabled = settings.get("enabled", False)
    interval = settings.get("interval_seconds", 7200)
    if type(enabled) is not bool:
        raise ValueError(f"{section}.enabled 必须为布尔值")
    if type(interval) is not int or not 300 <= interval <= 86400:
        raise ValueError(f"{section}.interval_seconds 必须为 300–86400 秒的整数")
    if "translation_runtime" in settings:
        value = settings["translation_runtime"]
        if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
            raise ValueError("papers.translation_runtime 必须为绝对路径")
    return enabled, interval


def project_conversation(value: Any) -> dict[str, Any] | None:
    """Expose only valid Web IDs and public metadata, including old databases."""
    if not isinstance(value, dict):
        return None
    stored = value.get("session_id")
    if not isinstance(stored, str) or not stored.startswith("web:"):
        return None
    session_id = stored[4:]
    try:
        validate_input("conversation", session_id)
        validate_input("conversation", stored)
    except ValueError:
        return None
    if (not isinstance(value.get("title"), str) or type(value.get("archived")) is not bool
            or not isinstance(value.get("created_at"), str) or not isinstance(value.get("updated_at"), str)
            or type(value.get("message_count")) is not int or value["message_count"] < 0):
        return None
    return {"session_id": session_id, **{name: value[name] for name in (
        "title", "archived", "created_at", "updated_at", "message_count")}}


class RuntimeLoop:
    """Own the runtime and its async clients on exactly one event loop."""

    def __init__(self, config: Any) -> None:
        from ..runtime import build_runtime

        self._lifecycle_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        try:
            self.runtime = self.call(build_runtime(config))
        except BaseException:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            self.loop.close()
            raise

    def submit(self, coroutine: Any) -> concurrent.futures.Future:
        with self._lifecycle_lock:
            if self._closing or self._closed:
                coroutine.close()
                raise concurrent.futures.CancelledError()
            try:
                return asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            except BaseException:
                coroutine.close()
                raise

    def call(self, coroutine: Any) -> Any:
        future = self.submit(coroutine)
        # Synchronous callers retain their existing wait-until-completion contract.
        return future.result()

    def close(self) -> None:
        async def shutdown() -> None:
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            protected = cleanup_tasks()
            for task in pending:
                if task not in protected:
                    task.cancel()
            # Join cleanup already in flight, including workers removed from a
            # manager's pool by deletion/eviction. Cancelling it would poison a
            # cached close task and leave its native state behind.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self.runtime.close()

        with self._lifecycle_lock:
            if self._closing or self._closed:
                return
            self._closing = True
            future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            future.result()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            self.loop.close()
            with self._lifecycle_lock:
                self._closed = True


def validate_public_origin(value: Any) -> str:
    """Require an exact HTTPS origin, without proxy-supplied interpretation."""
    if not isinstance(value, str) or not value.isascii() or any(char.isspace() for char in value):
        raise ValueError("共享 Web 必须配置有效的 HTTPS public_origin")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.netloc
                 and not parsed.username and not parsed.password
                 and not parsed.path and not parsed.query and not parsed.fragment
                 and value == f"https://{parsed.netloc}"
                 and not parsed.netloc.endswith(":")
                 and parsed.port != 0 and "\\" not in value
                 and all(ord(char) >= 32 and ord(char) != 127 for char in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("共享 Web 必须配置完整 HTTPS origin，不含路径或用户信息")
    return value


def _validate_bind(host: str, shared: bool) -> str:
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError as exc:
        raise ValueError("Web 通道必须绑定有效 IPv4 地址") from exc
    if not shared and not address.is_loopback:
        raise ValueError("本地 Web 仅允许 loopback；远程访问须显式配置共享模式和 HTTPS 访问")
    return host


def validate_proxy_hosts(value: Any) -> frozenset[str]:
    """Owner-configured upstream IP:port aliases, never forwarded assertions."""
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("web.proxy_hosts 必须是最多 16 个上游 IP:端口组成的列表")
    hosts = set()
    for entry in value:
        try:
            if not isinstance(entry, str) or not entry.isascii():
                raise ValueError
            parsed = urlsplit("//" + entry)
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
            host = f"[{address.compressed}]" if address.version == 6 else address.compressed
            if (not port or "%" in host or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment or entry != f"{host}:{port}"):
                raise ValueError
        except ValueError:
            raise ValueError("web.proxy_hosts 仅接受精确的上游 IP:端口（IPv6 使用 [地址]:端口），不能包含通配符或 URL") from None
        hosts.add(entry)
    return frozenset(hosts)


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], bridge: Any, *, sessions: Any = None,
                 public_origin: str | None = None) -> None:
        self.config = bridge.config if hasattr(bridge, "config") else bridge.runtime.config
        # Validate owner-controlled choices before binding a listener.
        self.config.model_selection_choices
        self.config.backend_choices
        mode = self.config.get("web", "mode", "local")
        if mode not in {"local", "shared"}:
            raise ValueError("web.mode 必须为 local 或 shared")
        self.shared = mode == "shared"
        if self.shared:
            self.public_origin = validate_public_origin(public_origin or self.config.get("web", "public_origin"))
            if sessions is None:
                raise ValueError("共享 Web 需要匿名会话存储")
            self.public_host = urlsplit(self.public_origin).netloc
            self.accepted_hosts = {self.public_host} | validate_proxy_hosts(self.config.get("web", "proxy_hosts", []))
        elif sessions is not None or public_origin is not None:
            raise ValueError("共享会话需要显式配置 web.mode = shared")
        host = _validate_bind(address[0], self.shared)
        self.bridge = bridge
        self.sessions = sessions
        self.csrf_token = secrets.token_urlsafe(32)
        self._request_slots = threading.BoundedSemaphore(64)
        self.chat_jobs = ChatJobs()
        self.progress = ProgressStore()
        self.news = None
        self.papers = None
        super().__init__((host, address[1]), AgentHandler)

    def server_close(self) -> None:
        self.chat_jobs.close()
        self.progress.close()
        if self.news is not None:
            self.news.close()
        if self.papers is not None:
            self.papers.close()
        super().server_close()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class AgentHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer
    protocol_version = "HTTP/1.1"
    cookie_name = "__Host-joyagent_session"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format: str, *args: Any) -> None:
        # Requests can contain user-chosen session names; do not log them.
        pass

    def _send(self, status: int, body: bytes, content_type: str,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if self.server.shared:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _json(self, status: int, value: Any, headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

    def _origin_ok(self, require_origin: bool = False) -> bool:
        host_values = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if len(host_values) != 1 or len(origins) > 1 or (require_origin and not origins):
            return False
        host = host_values[0]
        origin = origins[0] if origins else None
        if self.server.shared:
            # Some gateways replace Host with the upstream IP:port. These exact
            # aliases come from owner config; forwarded headers are not trusted.
            return host in self.server.accepted_hosts and (origin is None or origin == self.server.public_origin)
        hosts = {f"localhost:{self.server.server_port}", f"{self.server.server_address[0]}:{self.server.server_port}"}
        return host in hosts and (origin is None or origin == f"http://{host}")

    def _cookie_token(self) -> str:
        cookies = self.headers.get_all("Cookie", [])
        if len(cookies) != 1 or len(cookies[0]) > 8192:
            return ""
        matches = []
        for part in cookies[0].split(";"):
            name, separator, value = part.strip().partition("=")
            if name == self.cookie_name and separator:
                matches.append(value)
        if len(matches) != 1:
            return ""
        token = matches[0]
        if not token or len(token) > 256 or not token.isascii() or not all(char.isalnum() or char in "-_" for char in token):
            return ""
        return token

    def _session(self) -> Any:
        token = self._cookie_token()
        return self.server.sessions.authenticate(token) if token else None

    def _authenticated(self, require_origin: bool = False, session: Any = None) -> bool:
        supplied_values = self.headers.get_all("X-CSRF-Token", [])
        if len(supplied_values) != 1:
            return False
        supplied = supplied_values[0]
        expected = session.csrf if session is not None else self.server.csrf_token
        return supplied.isascii() and self._origin_ok(require_origin) and secrets.compare_digest(supplied, expected)

    def _cookie(self, token: str) -> str:
        # Preserve the browser's space when opening a link from another site;
        # mutations still require our exact Origin and the session's CSRF token.
        return (f"{self.cookie_name}={token}; Path=/; Max-Age=604800; "
                "Secure; HttpOnly; SameSite=Lax")

    def _render_index(self, session: Any = None) -> bytes:
        content = files("joyagent").joinpath("static", "index.html").read_text(encoding="utf-8")
        config = self.server.config
        provider = str(config.get("model", "provider", "demo"))
        model = config.default_model_selection
        label = "自动选择（Decision）" if model == "auto" else "离线 Demo" if provider == "demo" else f"{provider} · {model}"
        note = ("每轮先判断任务复杂度，再选择模型；回复中显示实际模型和选择原因。" if model == "auto"
                else "当前为离线工具演示；配置真实模型后可自由对话。" if provider == "demo" else f"模型配置：{provider} / {model}")
        replacements = {
            "__CSRF_TOKEN__": session.csrf if session else self.server.csrf_token,
            "__PROVIDER_KIND__": provider,
            "__PROVIDER_LABEL__": label,
            "__PROVIDER_NOTE__": note,
            "__MANUAL_MODEL__": config.select_model()["model"],
            "__SHARED_MODE__": "true" if self.server.shared else "false",
            "__RUNTIME_NOTE__": "共享服务 · 按浏览器隔离保存，清除 Cookie 或 7 天到期后进入新空间" if self.server.shared else "本地服务 · 数据保存在当前机器",
        }
        replacements = {marker: html.escape(value, quote=True) for marker, value in replacements.items()}
        replacements["__MODEL_OPTIONS__"] = "".join(
            f'<option value="{html.escape(choice, quote=True)}"{" selected" if choice == model else ""}{" hidden" if choice == "auto" else ""}>'
            f'{"自动选择（Decision）" if choice == "auto" else html.escape(choice)}</option>' for choice in config.model_selection_choices
        )
        replacements["__BACKEND_OPTIONS__"] = "".join(
            f'<option value="{html.escape(choice, quote=True)}"'
            f'{" selected" if choice == config.select_backend() else ""}>'
            f'{html.escape(config.backend_label(choice))}</option>' for choice in config.backend_choices
        )
        # Replace only template markers, never markers inside owner-provided names.
        content = re.sub("|".join(map(re.escape, replacements)), lambda match: replacements[match[0]], content)
        return content.encode()

    def _require_session(self, *, csrf: bool = True) -> Any:
        session = self._session()
        if session is None:
            self._json(401, {"error": "浏览器会话已失效，请刷新页面"})
            return None
        if csrf and not self._authenticated(session=session):
            self._json(403, {"error": "请求验证失败，请刷新页面"})
            return None
        return session

    def do_GET(self) -> None:
        if not self._origin_ok():
            self._json(403, {"error": "仅接受配置的同源请求"})
            return
        try:
            self._get()
        except Exception:
            self._json(500, {"error": "读取请求失败，请稍后重试"})

    def _get(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if self.server.shared and parsed.path == "/api/me":
            session = self._require_session(csrf=False)
            if session:
                self._json(200, {"csrf": session.csrf})
            return
        if self.server.shared and parsed.path == "/login":
            self._send(303, b"", "text/plain", {"Location": "/"})
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if parsed.path in assets:
            name, content_type = assets[parsed.path]
            session = None
            headers = None
            if self.server.shared and name == "index.html":
                session = self._session()
                if session is None:
                    session = self.server.sessions.create()
                    headers = {"Set-Cookie": self._cookie(session.token)}
            if name == "index.html":
                content = self._render_index(session)
            else:
                content = files("joyagent").joinpath("static", name).read_bytes()
            self._send(200, content, content_type, headers)
            return
        if parsed.path in {"/api/history", "/api/tools", "/api/backends", "/api/conversations", "/api/agents/status", "/api/chat/result", "/api/chat/progress", "/api/news", "/api/papers"}:
            session = None
            if self.server.shared:
                session = self._require_session()
                if session is None:
                    return
            elif not self._authenticated():
                self._json(403, {"error": "请求验证失败，请刷新页面"})
                return
            try:
                if parsed.path in {"/api/news", "/api/papers"}:
                    label = "论文" if parsed.path == "/api/papers" else "AI 资讯"
                    service = self.server.papers if parsed.path == "/api/papers" else self.server.news
                    if parsed.query:
                        self._json(400, {"error": f"{label}接口不接受查询参数"})
                        return
                    try:
                        snapshot = service.snapshot() if service is not None else {
                            "enabled": False, "interval_seconds": 7200, "refreshing": False,
                            "last_attempt_at": None, "next_refresh_at": None, "sources": [], "items": [],
                        }
                    except Exception:
                        self._json(503, {"error": f"读取{label if label == '论文' else ' AI 资讯'}失败，请稍后刷新"})
                        return
                    self._json(200, snapshot)
                    return
                if parsed.path in {"/api/chat/result", "/api/chat/progress"}:
                    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
                    if set(query) != {"session_id", "turn_id"} or any(len(value) != 1 for value in query.values()):
                        raise ValueError("结果查询需要 session_id 和 turn_id")
                    session_id = query["session_id"][0]
                    validate_input("result", session_id)
                    validate_input("result", f"web:{session_id}")
                    turn_id = validate_identifier(query["turn_id"][0])
                    owner = session.user_id if self.server.shared else "local"
                    if parsed.path == "/api/chat/progress":
                        progress = self.server.progress.get(owner, session_id, turn_id)
                        self._json(200, progress or {"status": "unavailable", "events": []})
                        return
                    result = self.server.chat_jobs.get(owner, session_id, turn_id)
                    if result is None:
                        self._json(404, {"error": "本轮结果不可用，可能已过期或服务已重启；请打开会话历史核对，不要重复执行"})
                    else:
                        self._json(200, result)
                    return
                if parsed.path == "/api/agents/status":
                    # Identity comes only from the signed browser session.
                    # Do not accept a workspace, PID or visitor selector.
                    if parsed.query:
                        self._json(400, {"error": "Agent 状态接口不接受查询参数"})
                        return
                    try:
                        if self.server.shared:
                            snapshot = self.server.bridge.call(self.server.bridge.agent_status(session.user_id))
                        else:
                            async def agent_status() -> dict[str, Any]:
                                return self.server.bridge.runtime.agent_status()
                            snapshot = self.server.bridge.call(agent_status())
                    except Exception:
                        self._json(500, {"error": "读取 Agent 运行状态失败，请稍后重试"})
                        return
                    self._json(200, {**snapshot, "scope": "visitor" if self.server.shared else "local"})
                    return
                if parsed.path == "/api/backends":
                    if self.server.shared:
                        catalog = self.server.bridge.call(self.server.bridge.backend_catalog(session.user_id))
                    else:
                        async def backend_catalog() -> list[dict[str, Any]]:
                            return self.server.bridge.runtime.backend_catalog()
                        catalog = self.server.bridge.call(backend_catalog())
                    self._json(200, {"default": self.server.config.select_backend(), "backends": catalog})
                    return
                if parsed.path == "/api/tools":
                    if self.server.shared:
                        catalog = self.server.bridge.call(self.server.bridge.tool_catalog(session.user_id))
                    else:
                        async def tool_catalog() -> list[dict[str, Any]]:
                            return [asdict(spec) for spec in self.server.bridge.runtime.agent.tools.specs()]
                        catalog = self.server.bridge.call(tool_catalog())
                    self._json(200, {"tools": catalog})
                    return
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
                if parsed.path == "/api/conversations":
                    if set(query) - {"archived"} or len(query.get("archived", ["false"])) != 1:
                        raise ValueError("会话列表参数无效")
                    value = query.get("archived", ["false"])[0]
                    if value not in {"true", "false"}:
                        raise ValueError("archived 仅支持 true 或 false")
                    archived = value == "true"
                    if self.server.shared:
                        rows = self.server.bridge.call(self.server.bridge.list_conversations(session.user_id, archived))
                    else:
                        async def conversations() -> list[dict[str, Any]]:
                            return self.server.bridge.runtime.memory.list_conversations(
                                prefix="web:", archived=archived, limit=1000)
                        rows = self.server.bridge.call(conversations())
                    self._json(200, {"conversations": [row for value in rows
                                if (row := project_conversation(value)) is not None]})
                    return
                if set(query) - {"session_id"} or len(query.get("session_id", ["default"])) != 1:
                    raise ValueError("会话参数无效")
                session_id = query.get("session_id", ["default"])[0]
                validate_input("history", session_id)
                validate_input("history", f"web:{session_id}")
                if self.server.shared:
                    result = self.server.bridge.call(self.server.bridge.conversation_history(session.user_id, session_id))
                else:
                    async def history() -> dict[str, Any]:
                        self.server.bridge.runtime.check_conversation_not_deleted(f"web:{session_id}")
                        memory = self.server.bridge.runtime.memory
                        messages = memory.history(f"web:{session_id}", limit=80)
                        return {"messages": project_history(messages, model_choices=self.server.config.model_choices),
                                "conversation": memory.get_conversation(f"web:{session_id}")}
                    result = self.server.bridge.call(history())
                self._json(200, {"messages": result["messages"],
                                 "conversation": project_conversation(result["conversation"])})
            except ValueError as exc:
                errors = {"/api/tools": "读取工具列表失败，请稍后重试", "/api/backends": "读取 Agent 列表失败，请稍后重试"}
                self._value_error(exc, errors.get(parsed.path, "会话参数无效"))
            except Exception:
                errors = {"/api/tools": "读取工具列表失败，请稍后重试", "/api/backends": "读取 Agent 列表失败，请稍后重试"}
                self._json(500, {"error": errors.get(parsed.path, "读取会话失败，请稍后重试")})
            return
        self._json(404, {"error": "路径不存在"})

    def _read_json(self) -> Any:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json(415, {"error": "请使用 application/json"})
            return None
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise ValueError("缺少有效的请求长度")
        size = int(lengths[0])
        if size <= 0 or size > MAX_BODY_BYTES:
            self._json(413, {"error": "请求体过大或为空"})
            return None
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("请求体不完整")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("请求体须为 JSON 对象")
        return data

    def _value_error(self, exc: ValueError, fallback: str) -> None:
        from ..runtime import (
            ConversationArchivedError, ConversationBusyError, ConversationDeletedError, ConversationNotFoundError,
        )
        for kind, status, message in (
            (JobConflictError, 409, "本轮编号已用于其他请求，请重新发送"),
            (JobBusyError, 429, "请求过于频繁或服务繁忙，请稍后重试"),
            (SteeringConflictError, 409, "当前任务已结束或补充请求不匹配；请保留输入并重新发送"),
            (SteeringLimitError, 429, "本轮补充指令已达到数量或长度上限，请等待任务结束后继续"),
            (ConversationDeletedError, 410, "对话已永久删除，请新建对话"),
            (ConversationArchivedError, 409, "会话已归档，请先恢复后再发送消息"),
            (ConversationBusyError, 409, "会话正在执行，请等待本轮完成后再操作"),
            (ConversationNotFoundError, 404, "会话不存在，请刷新列表"),
        ):
            if isinstance(exc, kind):
                self._json(status, {"error": message})
                return
        if self.server.shared:
            from .web_tenants import TenantBusyError
            if isinstance(exc, TenantBusyError):
                self._json(429, {"error": "请求过于频繁或服务繁忙，请稍后重试"}, {"Retry-After": "60"})
                return
        self._json(400, {"error": fallback if self.server.shared else str(exc)})

    def do_POST(self) -> None:
        from ..runtime import ConversationDeletionError
        try:
            self._post()
        except (ValueError, UnicodeDecodeError) as exc:
            self._value_error(exc, "请求参数无效")
        except (concurrent.futures.CancelledError, asyncio.CancelledError):
            self._json(503, {"error": "Agent 正在停止"})
        except ConversationDeletionError:
            self._json(503, {"error": "对话删除清理未完成，请重试"})
        except Exception as exc:
            error = "Agent 执行失败，请稍后重试" if self.server.shared else f"Agent 执行失败：{exc}"
            self._json(500, {"error": error})

    def _post(self) -> None:
        parsed = urlsplit(self.path)
        route = parsed.path
        if route not in {"/api/chat", "/api/chat/start", "/api/steer", "/api/conversations", "/api/conversations/archive", "/api/conversations/delete", "/api/conversations/rename"}:
            self._json(404, {"error": "路径不存在"})
            return
        session = None
        if self.server.shared:
            if not self._origin_ok(require_origin=True):
                self._json(403, {"error": "请求来源验证失败"})
                return
            session = self._require_session()
            if session is None:
                return
        elif not self._authenticated(require_origin=True):
            self._json(403, {"error": "请求验证失败，请刷新页面"})
            return
        data = self._read_json()
        if data is None:
            return
        if route == "/api/steer":
            if parsed.query or set(data) != {"message", "session_id", "turn_id", "request_id"}:
                raise ValueError("补充指令仅接受 message、session_id、turn_id 和 request_id")
            text, session_id = validate_input(data["message"], data["session_id"])
            validate_input(text, f"web:{session_id}")
            turn_id = validate_identifier(data["turn_id"])
            request_id = validate_identifier(data["request_id"], "request_id")
            if self.server.shared:
                receipt = self.server.bridge.call(self.server.bridge.steer(
                    session.user_id, session_id, text, turn_id=turn_id, request_id=request_id))
            else:
                async def steer() -> dict[str, Any]:
                    return self.server.bridge.runtime.steer(
                        text, f"web:{session_id}", turn_id=turn_id, request_id=request_id)
                receipt = self.server.bridge.call(steer())
            self._json(200, receipt)
            return
        if route in {"/api/conversations", "/api/conversations/archive", "/api/conversations/delete", "/api/conversations/rename"}:
            if parsed.query:
                raise ValueError("会话操作不接受查询参数")
            if route == "/api/conversations":
                if data:
                    raise ValueError("新建会话不接受自定义字段")
                if self.server.shared:
                    row = self.server.bridge.call(self.server.bridge.create_conversation(session.user_id))
                else:
                    async def create_conversation() -> dict[str, Any]:
                        return self.server.bridge.runtime.memory.create_conversation("web:chat-" + uuid.uuid4().hex)
                    row = self.server.bridge.call(create_conversation())
                self._json(201, {"conversation": project_conversation(row)})
                return
            if route == "/api/conversations/rename":
                if set(data) != {"session_id", "title"}:
                    raise ValueError("重命名仅接受 session_id 与 title")
                _, session_id = validate_input("rename", data["session_id"])
                validate_input("rename", f"web:{session_id}")
                if self.server.shared:
                    row = self.server.bridge.call(self.server.bridge.rename_conversation(
                        session.user_id, session_id, data["title"]))
                else:
                    async def rename_conversation() -> dict[str, Any]:
                        return self.server.bridge.runtime.rename_conversation(f"web:{session_id}", data["title"])
                    row = self.server.bridge.call(rename_conversation())
                self._json(200, {"conversation": project_conversation(row)})
                return
            if route == "/api/conversations/delete":
                if set(data) != {"session_id", "confirm"} or data["confirm"] is not True:
                    raise ValueError("永久删除需要 session_id 与 confirm: true")
                _, session_id = validate_input("delete", data["session_id"])
                validate_input("delete", f"web:{session_id}")
                owner = session.user_id if self.server.shared else "local"
                if self.server.chat_jobs.active(owner, session_id):
                    from ..runtime import ConversationBusyError
                    raise ConversationBusyError("会话正在执行")
                if self.server.shared:
                    self.server.bridge.call(self.server.bridge.delete_conversation(session.user_id, session_id))
                else:
                    async def delete_conversation() -> dict[str, Any]:
                        return await self.server.bridge.runtime.delete_conversation_async(f"web:{session_id}")
                    self.server.bridge.call(delete_conversation())
                self.server.chat_jobs.forget(owner, session_id)
                self.server.progress.forget(owner, session_id)
                self._json(200, {"session_id": session_id, "deleted": True})
                return
            if set(data) != {"session_id", "archived"} or type(data["archived"]) is not bool:
                raise ValueError("归档操作需要 session_id 与布尔值 archived")
            _, session_id = validate_input("archive", data["session_id"])
            validate_input("archive", f"web:{session_id}")
            owner = session.user_id if self.server.shared else "local"
            if self.server.chat_jobs.active(owner, session_id):
                from ..runtime import ConversationBusyError
                raise ConversationBusyError("会话正在执行")
            if self.server.shared:
                row = self.server.bridge.call(self.server.bridge.set_conversation_archived(
                    session.user_id, session_id, data["archived"]))
            else:
                async def archive_conversation() -> dict[str, Any]:
                    return self.server.bridge.runtime.set_conversation_archived(f"web:{session_id}", data["archived"])
                row = self.server.bridge.call(archive_conversation())
            self._json(200, {"conversation": project_conversation(row)})
            return
        if route == "/api/chat/start" and (parsed.query or "turn_id" not in data):
            raise ValueError("异步任务必须提供 turn_id 且不接受查询参数")
        if set(data) - {"message", "session_id", "model", "backend", "turn_id"}:
            raise ValueError("请求包含不支持的字段")
        text, session_id = validate_input(data.get("message"), data.get("session_id", "default"))
        validate_input(text, f"web:{session_id}")
        turn_options = {"turn_id": validate_identifier(data["turn_id"])} if "turn_id" in data else {}
        try:
            if "model" in data and not isinstance(data["model"], str):
                raise ValueError
            selected_model = self.server.config.select_model_choice(data.get("model"))
        except ValueError:
            self._json(400, {"error": "所选模型不可用，请刷新页面后选择"})
            return
        try:
            if "backend" in data and not isinstance(data["backend"], str):
                raise ValueError
            selected_backend = self.server.config.select_backend(data.get("backend"))
        except ValueError:
            self._json(400, {"error": "所选 Agent 不可用，请刷新页面后选择"})
            return
        server = self.server
        owner = session.user_id if server.shared else "local"

        async def execute_chat() -> dict[str, Any]:
            if server.shared:
                result = await server.bridge.chat(owner, session_id, text, model=selected_model,
                                                  backend=selected_backend, **turn_options)
            else:
                result = await server.bridge.runtime.chat(text, session_id=f"web:{session_id}",
                                                          model=selected_model, backend=selected_backend, **turn_options)
            direct = text.split(None, 1)[0] in {"/tool", "/tools"}
            actual_backend = "joyagent" if direct else selected_backend
            routing = {} if direct else project_routing(getattr(result, "routing", None), server.config.model_choices)
            actual_model = None if direct and result.steps == 0 else routing.get(
                "model", server.config.select_model()["model"] if direct and selected_model == "auto"
                else selected_model if selected_model != "auto" else None)
            return {**asdict(result), "routing": routing, "model": actual_model, "backend": actual_backend,
                    "backend_label": server.config.backend_label(actual_backend)}

        async def execute() -> dict[str, Any]:
            progress = (server.progress.begin(owner, session_id, turn_options["turn_id"])
                        if turn_options else TurnProgress())
            with progress_scope(progress):
                try:
                    result = await execute_chat()
                    progress("stopped" if result.get("stopped") else "completed")
                    if turn_options:
                        result["progress"] = progress.snapshot()
                    return result
                except BaseException:
                    progress("failed")
                    raise

        if route == "/api/chat/start":
            receipt = server.chat_jobs.start(owner, session_id, turn_options["turn_id"],
                                            {"message": text, "model": selected_model, "backend": selected_backend},
                                            lambda: server.bridge.submit(execute()))
            self._json(202, receipt)
        else:
            self._json(200, server.bridge.call(execute()))



def serve_web(config: Any, host: str = "127.0.0.1", port: int = 8765) -> None:
    # Refuse unsafe configuration before constructing providers or MCP processes.
    news_enabled, news_interval = news_settings(config)
    papers_enabled, papers_interval = news_settings(config, "papers")
    translator = None
    if papers_enabled and config.get("papers", "translation_runtime"):
        from ..paper_translation import paper_translator
        translator = paper_translator(config)
    from ..decisions import DecisionClient, decision_settings
    decision = decision_settings(config)
    if decision:
        DecisionClient(decision).credential_source()
    from ..routing import validate_routing
    validate_routing(config)
    config.model_selection_choices
    config.backend_choices
    mode = config.get("web", "mode", "local")
    if mode not in {"local", "shared"}:
        raise ValueError("web.mode 必须为 local 或 shared")
    shared = mode == "shared"
    host = _validate_bind(host, shared)
    bridge = server = sessions = None
    public_origin = None
    try:
        if shared:
            from .web_sessions import VisitorStore
            from .web_tenants import SharedRuntimeLoop
            public_origin = validate_public_origin(config.get("web", "public_origin"))
            validate_proxy_hosts(config.get("web", "proxy_hosts", []))
            session_value = config.get("web", "session_dir")
            if not isinstance(session_value, str) or not session_value.strip():
                raise ValueError("共享 Web 必须配置 session_dir")
            session_dir = Path(os.path.expandvars(session_value)).expanduser()
            if not session_dir.is_absolute():
                session_dir = config.root / session_dir
            resolved_session_dir = session_dir.resolve()
            if any(resolved_session_dir.is_relative_to(root)
                   for root in (config.workspace, config.data_dir)):
                raise ValueError("session_dir 必须位于 workspace 和 data_dir 之外")
            sessions = VisitorStore(session_dir)
            bridge = SharedRuntimeLoop(config)
        else:
            bridge = RuntimeLoop(config)
        server = AgentHTTPServer((host, port), bridge, sessions=sessions, public_origin=public_origin)
        if news_enabled:
            from ..news import NewsService
            server.news = NewsService(config.data_dir / "news.sqlite3", interval_seconds=news_interval)
            server.news.start()
        if papers_enabled:
            from ..papers import AgentPapersService as PapersService
            options = {"translator": translator} if translator is not None else {}
            server.papers = PapersService(config.data_dir / "papers.sqlite3", interval_seconds=papers_interval, **options)
            server.papers.start()
        display_url = public_origin if shared else f"http://127.0.0.1:{server.server_port}"
        print(f"joyagent → {display_url}  （Ctrl+C 停止）", flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        if bridge:
            bridge.close()
        if sessions:
            sessions.close()
