"""Small provider adapters. The loop and persisted messages are vendor-neutral."""

from __future__ import annotations

import asyncio
import json
import math
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .credentials import resolve_api_key
from .types import Message, ModelReply, Provider, ProviderError, ToolCall, ToolSpec

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REASONING_BYTES = 128 * 1024

# Model IDs remain explicit configuration: these are protocol defaults, not model
# recommendations. New services can share a wire adapter without losing identity.
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {"protocol": "chat", "base_url": "https://api.openai.com/v1",
               "api_key_env": "OPENAI_API_KEY", "token_parameter": "max_completion_tokens",
               "requires_api_key": True, "reasoning_content": False, "thinking": False},
    "anthropic": {"protocol": "anthropic", "base_url": "https://api.anthropic.com/v1",
                  "api_key_env": "ANTHROPIC_API_KEY", "token_parameter": "max_tokens",
                  "requires_api_key": True, "reasoning_content": False, "thinking": False},
    "ollama": {"protocol": "ollama", "base_url": "http://localhost:11434",
               "api_key_env": "OLLAMA_API_KEY", "token_parameter": "num_predict",
               "requires_api_key": False, "reasoning_content": False, "thinking": False},
    "deepseek": {"protocol": "chat", "base_url": "https://api.deepseek.com",
                 "api_key_env": "DEEPSEEK_API_KEY", "token_parameter": "max_tokens",
                 "requires_api_key": True, "reasoning_content": True, "thinking": True},
    "kimi": {"protocol": "chat", "base_url": "https://api.moonshot.cn/v1",
             "api_key_env": "MOONSHOT_API_KEY", "token_parameter": "max_tokens",
             "requires_api_key": True, "reasoning_content": True, "thinking": True},
    "zhipu": {"protocol": "chat", "base_url": "https://open.bigmodel.cn/api/paas/v4",
              "api_key_env": "ZHIPU_API_KEY", "token_parameter": "max_tokens",
              "requires_api_key": True, "reasoning_content": True, "thinking": True},
}


def _http_category(code: int) -> str:
    if code in {401, 403}:
        return "auth"
    if code == 402:
        return "quota"
    if code == 429:
        return "rate_limit"
    if code in {400, 404}:
        return "configuration"
    return "upstream" if 500 <= code <= 599 else "http"


def _connection_category(exc: Exception) -> str:
    # urllib wraps DNS, TLS and socket failures; inspect types, never strings
    # that may contain credentials, endpoint URLs or server response details.
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, TimeoutError):
        return "timeout"
    return "network"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # An endpoint redirect must never forward an API key to a different host.
        return None


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise ProviderError(f"模型接口返回 HTTP {code}；请检查模型、地址、配额及 API Key。",
                            category=_http_category(code)) from None
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise ProviderError("模型接口连接失败或超时；请检查 base_url、网络及 timeout_seconds。",
                            category=_connection_category(exc)) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderError("模型接口响应超过 4 MiB 限制。", category="response")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ProviderError("模型接口没有返回有效 JSON。", category="response") from None
    if not isinstance(data, dict) or data.get("error"):
        raise ProviderError("模型接口返回了错误或不支持的响应格式。", category="response")
    return data


def _arguments(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ProviderError("模型生成的工具参数不是有效 JSON。") from None
    if not isinstance(value, dict):
        raise ProviderError("模型工具参数必须是 JSON 对象。")
    return value


def _content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part["text"] for part in value
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    raise ProviderError("模型消息内容格式不受支持。")


def _function_tools(tools: list[ToolSpec]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": tool.name, "description": tool.description, "parameters": tool.parameters,
    }} for tool in tools]


def _chat_messages(messages: list[Message], *, ollama: bool = False) -> list[dict]:
    result = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            item["tool_calls"] = [{
                **({} if ollama else {"id": call.id}), "type": "function", "function": {
                    "name": call.name,
                    "arguments": call.arguments if ollama else json.dumps(call.arguments, ensure_ascii=False),
                },
            } for call in message.tool_calls]
        if message.role == "tool":
            item["tool_name" if ollama else "tool_call_id"] = message.name if ollama else message.tool_call_id
        result.append(item)
    return result


def _tool_call(call_id: Any, name: Any, arguments: Any) -> ToolCall:
    if not isinstance(call_id, str) or not call_id.strip():
        raise ProviderError("模型返回了无效的工具调用 ID。")
    if not isinstance(name, str) or not name.strip():
        raise ProviderError("模型返回了无效的工具名称。")
    return ToolCall(call_id, name, _arguments(arguments))


def _reasoning_content(value: Any) -> str:
    if not isinstance(value, str):
        raise ProviderError("模型 reasoning_content 必须是字符串。")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ProviderError("模型 reasoning_content 不是有效的 UTF-8 文本。") from None
    if size > MAX_REASONING_BYTES:
        # Truncating intermediate reasoning can invalidate a tool conversation.
        raise ProviderError("模型 reasoning_content 超过 128 KiB 限制；请降低 max_tokens 或拆分任务。")
    return value


def _deepseek_model(model: str) -> bool:
    """Recognize native DeepSeek model IDs, including routed gateway aliases."""
    value = model.lower()
    # Distilled Qwen/Llama models do not necessarily implement DeepSeek's wire
    # continuation contract. Match the whole ID, never an embedded substring.
    return ("distill" not in re.split(r"[/_.-]", value)
            and re.fullmatch(
                r"(?:[a-z0-9][a-z0-9_.-]*/)*deepseek-"
                r"(?:chat|reasoner|flash|pro|[vr]\d+(?:\.\d+)*)"
                r"(?:[-_.][a-z0-9]+)*", value) is not None)


def _chat_reply(message: dict, *, require_id: bool = True) -> ModelReply:
    calls = []
    for call in message.get("tool_calls") or []:
        function = call["function"]
        call_id = call.get("id")
        if call_id is None and not require_id:
            call_id = f"call_{uuid.uuid4().hex}"
        calls.append(_tool_call(call_id, function["name"], function.get("arguments", {})))
    return ModelReply(_content(message.get("content") or message.get("refusal")), calls)


class HTTPProvider:
    """Service identity is separate from the underlying HTTP message protocol."""

    def __init__(self, settings: dict[str, Any]):
        self.kind = settings["provider"]
        self.preset = PROVIDER_PRESETS[self.kind]
        self.protocol = self.preset["protocol"]
        self.model = settings.get("model", "")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("真实模型需要在 [model] 配置 model 名称。")
        self.base_url = str(settings.get("base_url") or self.preset["base_url"]).rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url 必须是 http(s) 地址，不能包含用户名、密码、查询参数或 fragment。")
        self.local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme == "http" and not self.local:
            raise ValueError("远程模型接口必须使用 HTTPS；HTTP 仅支持本机模型。")
        timeout = settings.get("timeout_seconds", 60)
        self.max_tokens = settings.get("max_tokens", 2048)
        if type(timeout) not in (int, float) or type(self.max_tokens) is not int:
            raise ValueError("timeout_seconds 必须为数字，max_tokens 必须为整数。")
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.max_tokens <= 0:
            raise ValueError("timeout_seconds 和 max_tokens 必须为正数。")
        self.key_env = str(settings.get("api_key_env") or self.preset["api_key_env"])
        self.credentials_file = settings.get("credentials_file")
        if self.credentials_file is not None and (not isinstance(self.credentials_file, str) or not self.credentials_file.strip()):
            raise ValueError("credentials_file 必须是非空文件路径。")
        # Some compatible services still require max_tokens. Explicitly configurable.
        self.token_parameter = settings.get("token_parameter", self.preset["token_parameter"])
        if self.token_parameter not in {"max_tokens", "max_completion_tokens", "num_predict"}:
            raise ValueError("token_parameter 仅支持 max_tokens 或 max_completion_tokens。")
        if self.protocol == "chat" and self.token_parameter == "num_predict":
            raise ValueError("num_predict 仅适用于 Ollama。")
        self.thinking = settings.get("thinking")
        if "thinking" in settings:
            if not self.preset["thinking"]:
                raise ValueError("thinking 配置仅适用于 deepseek、kimi、zhipu。")
            if not isinstance(self.thinking, str) or self.thinking not in {"enabled", "disabled"}:
                raise ValueError("thinking 必须是 enabled 或 disabled。")
        self._thinking_body = self._thinking_parameters()

    @property
    def requires_api_key(self) -> bool:
        # Only the generic OpenAI-compatible localhost adapter has the key escape
        # hatch. Named cloud-service aliases always validate their own credentials.
        return bool(self.preset["requires_api_key"] and not (self.kind == "openai" and self.local))

    def credential(self) -> tuple[str, str]:
        return resolve_api_key(self.kind, self.base_url, self.key_env, self.credentials_file)

    @property
    def deepseek_tool_reasoning(self) -> bool:
        """DeepSeek continuation behavior without changing service/key identity."""
        return self.kind == "deepseek" or (self.kind == "openai" and _deepseek_model(self.model))

    def _thinking_parameters(self) -> dict | None:
        if self.thinking is None:
            return None
        model = self.model.lower()
        if self.kind == "kimi":
            if model == "kimi-k3":
                raise ValueError("kimi-k3 始终思考且不支持 thinking 参数；请删除 thinking 配置。")
            if model in {"kimi-k2.7-code", "kimi-k2.7-code-highspeed"}:
                if self.thinking == "disabled":
                    raise ValueError("kimi-k2.7-code 系列不能关闭思考；请删除 thinking 或设为 enabled。")
                return {"type": "enabled", "keep": "all"}
            if model.startswith("moonshot-v1") or model in {"kimi-k2", "kimi-k2-0711-preview", "kimi-k2-0905-preview", "kimi-k2-turbo-preview"}:
                raise ValueError("该 Kimi 模型不支持 thinking 切换；请删除 thinking 配置。")
            if model in {"kimi-k2-thinking", "kimi-k2-thinking-turbo"}:
                raise ValueError("kimi-k2-thinking 系列通过模型名开启思考；请删除 thinking 配置。")
        if self.kind == "zhipu" and model in {"glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal"} and self.thinking == "disabled":
            raise ValueError("GLM-5.3 系列不能关闭思考；请删除 thinking 或设为 enabled。")
        return {"type": self.thinking}

    def _messages(self, messages: list[Message], tools: list[ToolSpec]) -> list[dict]:
        converted = _chat_messages(messages, ollama=self.protocol == "ollama")
        if not (self.preset["reasoning_content"] or self.deepseek_tool_reasoning):
            return converted
        last_user = max((index for index, message in enumerate(messages) if message.role == "user"), default=-1)
        for index, (message, item) in enumerate(zip(messages, converted)):
            if message.role != "assistant":
                continue
            state = message.provider_data
            if not isinstance(state, dict) or any(state.get(key) != value for key, value in {
                "provider": self.kind, "model": self.model, "base_url": self.base_url,
            }.items()):
                continue
            if "reasoning_content" not in state:
                continue
            # DeepSeek requires all historical reasoning when tools are offered.
            # Kimi K3/K2.7 always preserve history; K2.x default keeps only the
            # current tool loop. Never mutate stored history while projecting it.
            if self.deepseek_tool_reasoning and not tools:
                continue
            if (self.kind == "kimi" and self.model.lower() not in {
                    "kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed"}
                    and index < last_user):
                continue
            # GLM decides previous-turn retention via server-side clear_thinking;
            # its docs advise forwarding the original history in either mode.
            item["reasoning_content"] = _reasoning_content(state["reasoning_content"])
        return converted

    def _reply_state(self, message: dict) -> dict[str, Any]:
        if not (self.preset["reasoning_content"] or self.deepseek_tool_reasoning) or message.get("reasoning_content") is None:
            return {}
        return {"provider": self.kind, "model": self.model, "base_url": self.base_url,
                "reasoning_content": _reasoning_content(message["reasoning_content"])}

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        try:
            api_key, _ = self.credential()
        except ValueError:
            raise ProviderError("模型凭据不可用；请检查凭据配置和文件权限。", category="credentials") from None
        if not api_key and self.requires_api_key:
            raise ProviderError(f"缺少模型凭据；请设置环境变量 {self.key_env} 或配置匹配的本机凭据文件。",
                                category="credentials")
        headers = {}
        if self.protocol == "anthropic":
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
            endpoint = "/messages"
            payload = self._anthropic_payload(messages, tools)
        else:
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            ollama = self.protocol == "ollama"
            endpoint = "/api/chat" if ollama else "/chat/completions"
            payload = {"model": self.model, "messages": self._messages(messages, tools), "stream": False}
            if tools:
                payload["tools"] = _function_tools(tools)
                if self.protocol == "chat":
                    # Do not rely on a compatible gateway's tool-selection default.
                    payload["tool_choice"] = "auto"
            if ollama:
                payload["options"] = {"num_predict": self.max_tokens}
            else:
                payload[self.token_parameter] = self.max_tokens
            if self._thinking_body is not None:
                payload["thinking"] = dict(self._thinking_body)
        try:
            data = await asyncio.wait_for(asyncio.to_thread(_post_json, self.base_url + endpoint, payload, headers, self.timeout), self.timeout + 1)
        except TimeoutError:
            raise ProviderError("模型请求超时。", category="timeout") from None
        try:
            if self.protocol == "anthropic":
                calls = [_tool_call(block["id"], block["name"], block["input"])
                         for block in data["content"] if block.get("type") == "tool_use"]
                return ModelReply(_content(data["content"]), calls)
            message = data["message"] if self.protocol == "ollama" else data["choices"][0]["message"]
            reply = _chat_reply(message, require_id=self.protocol == "chat")
            reply.provider_data = self._reply_state(message)
            if (self.protocol == "chat" and data["choices"][0].get("finish_reason") == "length"
                    and not reply.content.strip() and not reply.tool_calls):
                raise ProviderError("模型在输出答案前耗尽 token 预算；请调大 max_tokens 或关闭该模型支持的思考模式。")
            return reply
        except (KeyError, TypeError, IndexError, AttributeError):
            raise ProviderError("模型响应缺少有效的消息或工具调用字段。", category="response") from None

    def _anthropic_payload(self, messages: list[Message], tools: list[ToolSpec]) -> dict:
        system = []
        converted: list[dict] = []
        for message in messages:
            if message.role in {"system", "developer"}:
                system.append(message.content)
                continue
            blocks: list[dict] = []
            role = message.role
            if role == "tool":
                role = "user"
                blocks.append({"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.content})
            else:
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in message.tool_calls)
            if not blocks:
                continue
            # All parallel results must share the user message immediately after tool_use.
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": role, "content": blocks})
        payload = {"model": self.model, "max_tokens": self.max_tokens, "messages": converted}
        if system:
            payload["system"] = "\n\n".join(system)
        if tools:
            payload["tools"] = [{"name": tool.name, "description": tool.description, "input_schema": tool.parameters} for tool in tools]
        return payload


class DemoProvider:
    """Deterministic offline demo, deliberately not presented as a language model."""

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        if messages and messages[-1].role == "tool":
            results = []
            for message in reversed(messages):
                if message.role != "tool":
                    break
                results.append(f"{message.name or '工具'}：{message.content}")
            return ModelReply("工具执行结果：\n" + "\n".join(reversed(results)))
        user = next((message.content.strip() for message in reversed(messages) if message.role == "user"), "")
        available = {tool.name for tool in tools}
        name, arguments = "", {}
        match = re.match(r"^(?:记住|remember)\s*[：:]\s*(.+)$", user, re.I | re.S)
        write_match = re.match(r"^写入文件\s+([^：:\n]+)[：:](.*)$", user, re.S)
        read_match = re.match(r"^读取文件\s+(.+)$", user)
        forget_match = re.match(r"^忘记记忆\s+(.+)$", user)
        if match:
            name, arguments = "memory_remember", {"key": "preference", "value": match.group(1).strip()}
        elif write_match:
            name, arguments = "workspace_write", {"path": write_match.group(1).strip(), "content": write_match.group(2), "overwrite": False}
        elif read_match:
            name, arguments = "workspace_read", {"path": read_match.group(1).strip()}
        elif forget_match:
            name, arguments = "memory_forget", {"key": forget_match.group(1).strip()}
        elif user.startswith("搜索记忆"):
            name, arguments = "memory_search", {"query": user[len("搜索记忆"):].strip(" ：:")}
        elif "列出文件" in user or user.lower() == "list files":
            name, arguments = "workspace_list", {"path": "."}
        elif "现在几点" in user or user.lower() == "time":
            name, arguments = "time_now", {}
        if name and name in available:
            return ModelReply(tool_calls=[ToolCall(f"demo_{uuid.uuid4().hex}", name, arguments)])
        if name:
            return ModelReply(f"当前策略未提供 {name} 工具。")
        return ModelReply("我正在离线 Demo 模式。可以试试：\n记住：我喜欢中文\n搜索记忆 中文\n忘记记忆 preference\n列出文件\n写入文件 demo.md：你好\n读取文件 demo.md\n现在几点\n配置真实模型后即可进行自由对话和多步任务。")


def create_provider(settings: dict[str, Any]) -> Provider:
    kind = settings.get("provider", "demo")
    if kind == "demo":
        if "thinking" in settings:
            raise ValueError("thinking 配置仅适用于 deepseek、kimi、zhipu。")
        return DemoProvider()
    if kind not in PROVIDER_PRESETS:
        raise ValueError("provider 仅支持 demo、openai、anthropic、ollama、deepseek、kimi、zhipu。")
    return HTTPProvider({**settings, "provider": kind})
