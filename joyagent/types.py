"""Provider-neutral messages. No vendor SDK objects cross this boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


_SHARED_PROVIDER_MESSAGES = {
    "dns": "模型服务域名解析失败，请联系服务管理员检查网络配置。",
    "tls": "模型服务安全连接失败，请联系服务管理员检查证书和网络配置。",
    "network": "暂时无法连接模型服务，请稍后重试或联系服务管理员。",
    "timeout": "模型服务响应超时，请稍后重试。",
    "auth": "模型服务认证失败，请联系服务管理员检查凭据。",
    "quota": "模型服务额度不足，请联系服务管理员。",
    "rate_limit": "模型服务请求过于频繁，请稍后重试。",
    "busy": "本地模型请求通道繁忙，请稍后重试。",
    "budget": "已达到本轮模型或工具调用上限，任务已暂停。已执行的操作见工具记录。",
    "configuration": "模型服务配置不受支持，请联系服务管理员检查模型和接口地址。",
    "upstream": "模型服务暂时不可用，请稍后重试。",
    "http": "模型服务拒绝了请求，请稍后重试或联系服务管理员。",
    "credentials": "模型服务凭据不可用，请联系服务管理员检查配置。",
    "response": "模型服务返回了无法处理的响应，请稍后重试或联系服务管理员。",
}


def shared_provider_message(category: object) -> str:
    """Map only plain, allowlisted categories; never trust exception text."""
    if type(category) is not str:
        category = None
    return _SHARED_PROVIDER_MESSAGES.get(category, "本轮未完成，请稍后重试或联系服务管理员。")


class ProviderError(RuntimeError):
    """Provider-neutral failure with an allowlisted, shared-safe explanation.

    The exception text is for local diagnostics only. Neither it nor an unknown
    category may be reflected into shared conversations or durable history.
    """

    def __init__(self, message: str, *, category: str | None = None):
        super().__init__(message)
        self.category = category if type(category) is str and category in _SHARED_PROVIDER_MESSAGES else None

    @property
    def shared_message(self) -> str:
        return shared_provider_message(self.category)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    # Opaque protocol continuation data. Only the matching adapter may replay it.
    provider_data: dict[str, Any] = field(default_factory=dict, repr=False)
    # Public backend identity and bounded outcomes, never native CLI transcripts.
    backend: str | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    # Public model-routing outcome, never raw classifier input or credentials.
    routing: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_provider_data: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_provider_data:
            data.pop("provider_data", None)
        if self.backend is None:
            data.pop("backend", None)
        if not self.tool_events:
            data.pop("tool_events", None)
        if not self.routing:
            data.pop("routing", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        provider_data = data.get("provider_data", {})
        if not isinstance(provider_data, dict):
            raise ValueError("消息 provider_data 必须是对象")
        backend = data.get("backend")
        # OpenCode is retired; its stored replies still retain their provenance.
        if not isinstance(backend, str) or backend not in {"joyagent", "codex", "opencode", "pi", "deepseek"}:
            backend = None
        return cls(
            role=data["role"], content=data.get("content", ""),
            tool_calls=[ToolCall(**item) for item in data.get("tool_calls", [])],
            tool_call_id=data.get("tool_call_id"), name=data.get("name"),
            provider_data=provider_data,
            backend=backend,
            tool_events=data.get("tool_events", []) if isinstance(data.get("tool_events", []), list) else [],
            routing=data.get("routing", {}) if isinstance(data.get("routing", {}), dict) else {},
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolExecutionResult:
    """Trusted local execution outcome; never constructed from an MCP response."""
    ok: bool
    result: dict[str, Any]


@dataclass
class ModelReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider_data: dict[str, Any] = field(default_factory=dict, repr=False)
    backend: str | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    stopped: bool = False


class Provider(Protocol):
    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> ModelReply: ...


@dataclass
class AgentResult:
    text: str
    steps: int
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    stopped: bool = False
    routing: dict[str, Any] = field(default_factory=dict)
    steering: list[dict[str, Any]] = field(default_factory=list)
