"""Composition root: all six layers meet here, not inside a channel."""

from __future__ import annotations

import re
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from .agent import Agent
from .config import Config
from .decisions import DecisionClient, decision_settings, register_decision
from .execution import SandboxExecutor, execution_settings, register_execution, validate_shared_mcp
from .memory import MemoryStore
from .mcp import MCPClient
from .policy import Policy
from .providers import create_provider
from .routing import ModelRouter, validate_routing
from .skills import SkillCatalog
from .tools import ToolRegistry, Workspace
from .types import AgentResult, ToolSpec


class ConversationArchivedError(ValueError):
    """An archived conversation can be read but cannot accept a new turn."""


class ConversationBusyError(ValueError):
    """A running or queued turn must finish before destructive metadata changes."""


class ConversationNotFoundError(ValueError):
    """Metadata operations never create an unknown conversation implicitly."""


class ConversationDeletedError(ValueError):
    """A deleted ID cannot be read, restored, or reused for another turn."""


class ConversationDeletionError(RuntimeError):
    """Permanent deletion must finish its storage cleanup before succeeding."""


def mcp_tool_name(server: str, original: str) -> str:
    """Keep simple names readable; hash transformed names to avoid collisions."""
    raw = f"mcp_{server}_{original}"
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw):
        return raw
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return safe[:51] + "_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _mcp_handler(client: MCPClient, name: str):
    async def invoke(**arguments):
        return await client.call_tool(name, arguments)
    return invoke


def agent_status_snapshot(config, stats, *, catalog=None, workers=None, active_backends=None):
    """Project one visitor's counters and process metadata, without starting work.

    An uncached visitor has no external process. Its dependency availability is
    still reported separately by the existing backend catalog endpoint.
    """
    workers = workers or {}
    active_backends = active_backends or {}
    if catalog is None:
        catalog = [{"id": name, "label": config.backend_label(name), "available": True, "reason": ""}
                   for name in config.backend_choices]
    agents = []
    running = 0
    resident_sessions = 0
    for backend in catalog:
        name = backend["id"]
        active = active_backends.get(name, 0) > 0
        row = {"id": name, "label": backend["label"], "status": "stopped",
               "pid": None, "manager_pid": None,
               "session_count": stats["by_backend"].get(name, 0),
               "resident_session_count": 0, "reason": ""}
        if name == "joyagent":
            row.update(status="busy" if active else "ready", pid=os.getpid(), resident_session_count=None)
            running += 1
        elif name in workers:
            state = workers[name]
            row.update({key: state[key] for key in ("status", "pid", "manager_pid", "resident_session_count", "reason")})
            if active and row["status"] == "idle":
                row["status"] = "busy"
            running += int(state["running"])
            resident_sessions += row["resident_session_count"]
        elif active:
            row["status"] = "starting"
        elif not backend["available"]:
            row.update(status="unavailable", reason=backend.get("reason", ""))
        agents.append(row)
    return {"conversation_count": stats["total"], "archived_conversation_count": stats["archived"],
            "resident_session_count": resident_sessions, "running_agent_count": running, "agents": agents}


@dataclass
class Runtime:
    config: Config
    agent: Agent
    memory: MemoryStore
    mcp_clients: list[MCPClient] = field(default_factory=list)
    workspace: Workspace | None = None
    router: ModelRouter | None = None
    _active_sessions: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _active_backends: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _deleting_sessions: set[str] = field(default_factory=set, init=False, repr=False)
    _resident_manager: object | None = field(default=None, init=False, repr=False)

    def _get_resident_manager(self):
        if self._resident_manager is None:
            from .resident_backend import ResidentManager
            self._resident_manager = ResidentManager(
                self.config, self.workspace, self.agent.policy, self.memory)
        return self._resident_manager

    def _check_not_deleting(self, session_id: str) -> None:
        if session_id in self._deleting_sessions:
            raise ConversationBusyError("对话正在删除清理，请等待完成后重试")

    @staticmethod
    def _session_id(session_id: str) -> str:
        if not isinstance(session_id, str) or not re.fullmatch(r"[\w:@.\-]{1,128}", session_id):
            raise ValueError("session_id 只允许 1–128 个字母、数字、下划线、冒号、@、点或连字符")
        return session_id

    def check_conversation_not_deleted(self, session_id: str) -> str:
        session_id = self._session_id(session_id)
        if self.memory.is_conversation_deleted(session_id):
            raise ConversationDeletedError("对话已永久删除，请新建对话")
        return session_id

    def rename_conversation(self, session_id: str, title: str) -> dict:
        """Metadata-only changes are safe while an Agent turn is running."""
        session_id = self._session_id(session_id)
        self._check_not_deleting(session_id)
        session_id = self.check_conversation_not_deleted(session_id)
        if self.memory.get_conversation(session_id) is None:
            raise ConversationNotFoundError("会话不存在，请刷新列表")
        try:
            return self.memory.rename_conversation(session_id, title)
        except ValueError:
            # Another process may delete between the read and the writer lock.
            self.check_conversation_not_deleted(session_id)
            if self.memory.get_conversation(session_id) is None:
                raise ConversationNotFoundError("会话不存在，请刷新列表") from None
            raise

    def set_conversation_archived(self, session_id: str, archived: bool) -> dict:
        """Run synchronously on the runtime loop, atomically with active checks."""
        session_id = self._session_id(session_id)
        self._check_not_deleting(session_id)
        session_id = self.check_conversation_not_deleted(session_id)
        if type(archived) is not bool:
            raise ValueError("archived 必须是布尔值")
        if self._active_sessions.get(session_id):
            raise ConversationBusyError("会话正在执行，请等待本轮完成后再归档或恢复")
        if self.memory.get_conversation(session_id) is None:
            raise ConversationNotFoundError("会话不存在，请刷新列表")
        return self.memory.set_conversation_archived(session_id, archived)

    def delete_conversation(self, session_id: str) -> dict:
        """Compatibility path for conversations with no native state to clean."""
        session_id = self._session_id(session_id)
        self._check_not_deleting(session_id)
        if self._active_sessions.get(session_id):
            raise ConversationBusyError("会话正在执行，请等待本轮完成后再删除")
        native_root = self.config.data_dir / "native_sessions"
        if ((self._resident_manager is not None and self._resident_manager.has_session(session_id))
                or native_root.exists() or native_root.is_symlink()):
            raise ConversationBusyError("此会话需要清理原生 Agent，请使用异步删除")
        return self._delete_conversation_storage(session_id)

    def _delete_conversation_storage(self, session_id: str) -> dict:
        """Keep the tombstone retry path open until the audit mirror is cleaned."""
        try:
            if (not self.memory.is_conversation_deleted(session_id)
                    and self.memory.get_conversation(session_id) is None):
                raise ConversationNotFoundError("会话不存在，请刷新列表")
            return self.memory.delete_conversation(session_id)
        except ConversationNotFoundError:
            raise
        except Exception as exc:
            # SQLite may already have committed the tombstone. A retry must
            # still invoke the store to finish replacing the audit mirror.
            raise ConversationDeletionError("对话删除清理未完成，请重试") from exc

    async def delete_conversation_async(self, session_id: str) -> dict:
        """Stop native agents and erase their state before purging SQLite/audit."""
        session_id = self._session_id(session_id)
        self._check_not_deleting(session_id)
        if self._active_sessions.get(session_id):
            raise ConversationBusyError("会话正在执行，请等待本轮完成后再删除")
        # Register before the first await, so chat, archive and a second delete
        # on this runtime cannot race a slow process or filesystem cleanup.
        self._deleting_sessions.add(session_id)
        try:
            if (not self.memory.is_conversation_deleted(session_id)
                    and self.memory.get_conversation(session_id) is None):
                raise ConversationNotFoundError("会话不存在，请刷新列表")
            manager = self._resident_manager
            native_root = self.config.data_dir / "native_sessions"
            if manager is None and (native_root.exists() or native_root.is_symlink()):
                # A restarted service may have no live manager but still own
                # private files from an interrupted native process.
                manager = self._get_resident_manager()
            if manager is not None:
                await manager.close_session(session_id)
            return self._delete_conversation_storage(session_id)
        except (ConversationNotFoundError, ConversationDeletionError):
            raise
        except Exception as exc:
            raise ConversationDeletionError("对话删除清理未完成，请重试") from exc
        finally:
            self._deleting_sessions.discard(session_id)

    def backend_catalog(self) -> list[dict]:
        from .cli_backend import backend_catalog
        return backend_catalog(self.config, self.workspace, self.agent.provider)

    def agent_status(self) -> dict:
        """Read a coherent event-loop snapshot without acquiring an Agent lock."""
        workers = self._resident_manager.snapshot() if self._resident_manager is not None else {}
        return agent_status_snapshot(self.config, self.memory.conversation_stats(prefix="web:"),
                                     catalog=self.backend_catalog(), workers=workers,
                                     active_backends=self._active_backends)

    async def chat(self, text: str, session_id: str = "default", *,
                   model: str | None = None, backend: str | None = None,
                   turn_id: str | None = None) -> AgentResult:
        # This check and registration have no await between them. All Web
        # metadata mutations use this same event loop, including other tabs.
        session_id = self._session_id(session_id)
        self._check_not_deleting(session_id)
        session_id = self.check_conversation_not_deleted(session_id)
        conversation = self.memory.get_conversation(session_id)
        if conversation is not None and conversation["archived"]:
            raise ConversationArchivedError("会话已归档，请先恢复后再发送消息")
        selected = self.config.select_backend(backend)
        direct = isinstance(text, str) and text.split(maxsplit=1)[:1] in [["/tool"], ["/tools"]]
        active_backend = "joyagent" if direct else selected
        self._active_sessions[session_id] = self._active_sessions.get(session_id, 0) + 1
        if session_id.startswith("web:"):
            self._active_backends[active_backend] = self._active_backends.get(active_backend, 0) + 1
        try:
            return await self._chat(text, session_id, model=model, backend=backend, turn_id=turn_id)
        finally:
            remaining = self._active_sessions[session_id] - 1
            if remaining:
                self._active_sessions[session_id] = remaining
            else:
                del self._active_sessions[session_id]
            if session_id.startswith("web:"):
                remaining = self._active_backends[active_backend] - 1
                if remaining:
                    self._active_backends[active_backend] = remaining
                else:
                    del self._active_backends[active_backend]

    def steer(self, text: str, session_id: str, *, turn_id: str, request_id: str) -> dict:
        session_id = self._session_id(session_id)
        self._check_not_deleting(session_id)
        self.check_conversation_not_deleted(session_id)
        return self.agent.steer(text, session_id, turn_id=turn_id, request_id=request_id)

    async def _chat(self, text: str, session_id: str, *,
                    model: str | None = None, backend: str | None = None,
                    turn_id: str | None = None) -> AgentResult:
        choice = self.config.select_model_choice(model)
        selected = self.config.select_backend(backend)
        default = self.config.select_model()["model"]
        # Explicit commands always retain the ordinary tool gate, even if the
        # selected external executable is currently unavailable.
        direct = isinstance(text, str) and text.split(maxsplit=1)[:1] in [["/tool"], ["/tools"]]
        steering_delivery = "next_turn" if direct or selected != "joyagent" else "next_step"

        def selected_provider(model_id):
            settings = self.config.select_model(model_id)
            provider = self.agent.provider if model_id == default else create_provider(settings)
            if selected != "joyagent" and not direct:
                provider = self._get_resident_manager().provider(selected, provider, session_id)
            return provider

        if choice == "auto" and not direct:
            if self.router is None:
                raise ValueError("自动路由未初始化。")

            async def choose(text, history):
                routing = await self.router.choose(text, history)
                return selected_provider(routing["model"]), routing

            # Classification takes place inside Agent.chat's session lock and
            # whole-turn deadline. The selected provider is then fixed.
            return await self.agent.chat(text, session_id, provider_selector=choose,
                                         turn_id=turn_id, steering_delivery=steering_delivery)
        return await self.agent.chat(text, session_id,
                                     provider=selected_provider(default if choice == "auto" else choice),
                                     turn_id=turn_id, steering_delivery=steering_delivery)

    async def close(self) -> None:
        try:
            if self._resident_manager is not None:
                await self._resident_manager.close()
        finally:
            try:
                for client in reversed(self.mcp_clients):
                    await client.close()
            finally:
                self.memory.close()


async def build_runtime(config: Config) -> Runtime:
    routing = validate_routing(config)
    decision = decision_settings(config)
    if decision:
        DecisionClient(decision).credential_source()
    settings = config.select_model()
    config.select_backend()
    execution = execution_settings(config)
    if config.get("web", "mode", "local") == "shared":
        validate_shared_mcp(config)
    policy = Policy.load(config.policy_path)
    # Fail early for missing/invalid persona rather than after accepting a message.
    if not config.soul_path.is_file():
        raise ValueError(f"SOUL.md 不存在：{config.soul_path}")
    timezone = config.get("agent", "timezone", "Asia/Shanghai")
    ZoneInfo(timezone)
    provider = create_provider(settings)
    memory = MemoryStore(config.data_dir, vector_dimensions=config.get("memory", "vector_dimensions", 256))
    clients: list[MCPClient] = []
    try:
        protected = [config.path, config.soul_path, config.policy_path, config.skills_dir, config.data_dir]
        if settings.get("credentials_file"):
            protected.append(Path(settings["credentials_file"]))
        if decision and decision.get("credentials_file"):
            protected.append(Path(decision["credentials_file"]))
        workspace = Workspace(config.workspace, protected=protected)
        skills = SkillCatalog(config.skills_dir)
        registry = ToolRegistry(policy)
        register_args = (registry, workspace, memory, skills, timezone)
        from .tools import register_builtins
        register_builtins(*register_args)
        if decision:
            register_decision(registry, decision)
        executor = SandboxExecutor(execution, workspace) if execution["enabled"] else None
        if executor:
            await executor.check()
            register_execution(registry, executor)
        for name, settings in config.data.get("mcp", {}).get("servers", {}).items():
            if not settings.get("enabled", False):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", name):
                raise ValueError("MCP server 名称只允许 1–20 个字母、数字、连字符、下划线")
            launch = None
            if settings.get("sandbox"):
                if executor is None:
                    raise ValueError("隔离 MCP 需要 execution.enabled=true")
                launch = executor.mcp_launch(config, settings)
            client = MCPClient(name, settings, launch=launch) if launch is not None else MCPClient(name, settings)
            clients.append(client)
            await client.start()
            for spec in await client.list_tools():
                exported = mcp_tool_name(name, spec.name)
                registry.register(ToolSpec(exported, spec.description, spec.parameters),
                                  _mcp_handler(client, spec.name))
        agent = Agent(provider, memory, registry, policy, config.soul_path,
                      redact_errors=config.get("web", "mode", "local") == "shared")
        return Runtime(config, agent, memory, clients, workspace, ModelRouter(config) if routing else None)
    except BaseException:
        try:
            for client in reversed(clients):
                await client.close()
        finally:
            memory.close()
        raise
