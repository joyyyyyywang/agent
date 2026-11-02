"""L3 tools: allowlisted calls, bounded outputs and a confined file workspace."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import errno
import inspect
import json
import math
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .policy import Policy
from .types import ToolExecutionResult, ToolSpec


def validate_arguments(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    """Validate the ordinary JSON Schema subset used by built-ins.

    MCP servers remain responsible for their full, possibly richer schema.
    """
    kind = schema.get("type")
    checks = {
        "object": lambda x: isinstance(x, dict),
        "array": lambda x: isinstance(x, list),
        "string": lambda x: isinstance(x, str),
        "integer": lambda x: type(x) is int,
        "number": lambda x: type(x) in (int, float) and math.isfinite(x),
        "boolean": lambda x: type(x) is bool,
        "null": lambda x: x is None,
    }
    if isinstance(kind, str) and kind in checks and not checks[kind](value):
        raise ValueError(f"{path} 必须是 {kind}")
    if isinstance(kind, list) and not any(k in checks and checks[k](value) for k in kind):
        raise ValueError(f"{path} 类型错误")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} 不在允许值中")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"缺少参数 {path}.{key}")
        for key, item in value.items():
            if key not in props and schema.get("additionalProperties") is False:
                raise ValueError(f"未知参数 {path}.{key}")
            if key in props:
                validate_arguments(item, props[key], f"{path}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_arguments(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", 100000):
            raise ValueError(f"{path} 过长")
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{path} 不能为空")
    if type(value) in (int, float):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} 太小")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} 太大")


@dataclass
class RegisteredTool:
    spec: ToolSpec
    handler: Callable[..., Any]


class ToolRegistry:
    def __init__(self, policy: Policy):
        self.policy = policy
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: Callable[..., Any]) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec.name):
            raise ValueError(f"不支持的工具名称：{spec.name}")
        if spec.name in self._tools:
            raise ValueError(f"工具名称重复：{spec.name}")
        self._tools[spec.name] = RegisteredTool(spec, handler)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for name, tool in self._tools.items() if self.policy.permits(name)]

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.policy.permits(name):
            return {"ok": False, "code": "permission_denied", "error": f"policy 禁止工具：{name}"}
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "code": "unknown_tool", "error": f"工具不存在：{name}"}
        try:
            if not isinstance(arguments, dict):
                raise ValueError("arguments 必须是对象")
            validate_arguments(arguments, tool.spec.parameters)
            # Sync built-ins perform short, bounded local operations in this loop.
            # Running them in detached threads would allow writes after cancellation.
            async with asyncio.timeout(self.policy.limits.tool_timeout_seconds):
                result = tool.handler(**arguments)
                if inspect.isawaitable(result):
                    result = await result
            if isinstance(result, ToolExecutionResult):
                output = dict(result.result)
                cap = self.policy.limits.tool_output_chars
                if len(json.dumps(output, ensure_ascii=False, allow_nan=False)) > cap:
                    output["truncated"] = True
                    for key in ("stdout", "stderr", "cwd"):
                        serialized = json.dumps(output, ensure_ascii=False, allow_nan=False)
                        excess = len(serialized) - cap
                        if excess <= 0:
                            break
                        if isinstance(output.get(key), str):
                            output[key] = output[key][:max(0, len(output[key]) - excess)]
                    if len(json.dumps(output, ensure_ascii=False, allow_nan=False)) > cap:
                        output = {"truncated": True}
                return {"ok": result.ok, **({} if result.ok else {"code": "execution_failed"}),
                        "result": output}
            serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(serialized) > self.policy.limits.tool_output_chars:
                result = {"truncated": True, "content": serialized[:self.policy.limits.tool_output_chars],
                          "isError": isinstance(result, dict) and result.get("isError") is True}
            if isinstance(result, dict) and result.get("isError") is True:
                return {"ok": False, "code": "tool_error", "result": result}
            return {"ok": True, "result": result}
        except TimeoutError:
            return {"ok": False, "code": "timeout", "error": "工具超时；外部操作状态可能需要核实"}
        except Exception as exc:
            # Errors from configured third-party tools are data, never instructions.
            return {"ok": False, "code": "tool_error", "error": str(exc)[:1000]}


class Workspace:
    def __init__(self, root: Path, protected: list[Path] | None = None):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("工作区文件工具需要支持 dir_fd 和 O_NOFOLLOW 的 POSIX 系统")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.protected = [path.resolve() for path in (protected or [])]
        root_info = self.root.stat()
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        self._protected_identities: set[tuple[int, int]] = set()
        self._refresh_protected()

    def _refresh_protected(self) -> None:
        for path in self.protected:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            self._protected_identities.add((info.st_dev, info.st_ino))

    def _check_identity(self, info: os.stat_result) -> None:
        if (info.st_dev, info.st_ino) in self._protected_identities:
            raise ValueError("该路径属于 Agent 配置或状态，不能通过文件工具访问")

    def path(self, value: str) -> Path:
        """Validate the lexical path; I/O must also use _directory and dir_fd."""
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ValueError("路径为空或过长")
        if "\x00" in value:
            raise ValueError("路径不能包含空字符")
        candidate = Path(value)
        if candidate.is_absolute():
            raise ValueError("请使用 workspace 内的相对路径")
        if ".." in candidate.parts:
            raise ValueError("路径不能越出 workspace（包括符号链接）")
        if any(part in {".git", ".env", ".ssh", ".aws", ".codex", ".agents"}
               or part.startswith(".env.") for part in candidate.parts):
            raise ValueError("该路径包含受保护的目录或凭据文件")
        result = self.root / candidate
        if any(result == item or result.is_relative_to(item) for item in self.protected):
            raise ValueError("该路径属于 Agent 配置或状态，不能通过文件工具访问")
        return result

    @staticmethod
    def _open(name: str | Path, flags: int, *, dir_fd: int | None = None,
              mode: int = 0o600) -> int:
        try:
            return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError("工作区路径不能包含符号链接，父路径必须是目录") from None
            raise

    @contextmanager
    def _directory(self, parts: tuple[str, ...], *, create: bool = False):
        """Walk one component at a time and pin each directory before use.

        No path assembled from user input is passed to an absolute-path syscall.
        A concurrent rename or symlink substitution cannot redirect an open fd.
        The owner must keep workspace ancestors outside untrusted process mounts.
        """
        self._refresh_protected()
        fd = self._open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            root_info = os.fstat(fd)
            if (root_info.st_dev, root_info.st_ino) != self._root_identity:
                raise ValueError("工作区根目录已变化，请重启 Agent 后重试")
            self._check_identity(root_info)
            for part in parts:
                try:
                    child = self._open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = self._open(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                try:
                    self._check_identity(os.fstat(child))
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def _entry(self, directory_fd: int, name: str) -> os.stat_result:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("工作区路径不能包含符号链接")
        self._check_identity(info)
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("工作区文件不能是硬链接")
        return info

    def list(self, path: str = ".") -> dict[str, Any]:
        directory = self.path(path)
        entries = []
        truncated = False
        with self._directory(directory.relative_to(self.root).parts) as directory_fd:
            # scandir(fd) keeps iteration on the pinned directory. Do not use
            # DirEntry.path (a name) for subsequent checks or opening files.
            with os.scandir(directory_fd) as children:
                for child in children:
                    try:
                        self.path(str(directory.relative_to(self.root) / child.name))
                        info = self._entry(directory_fd, child.name)
                        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                            continue
                        entries.append({"name": child.name, "type": "directory" if stat.S_ISDIR(info.st_mode) else "file"})
                    except (OSError, ValueError):
                        continue
                    if len(entries) >= 200:
                        truncated = True
                        break
        return {"path": str(directory.relative_to(self.root)),
                "entries": sorted(entries, key=lambda entry: entry["name"]), "truncated": truncated}

    def read(self, path: str) -> dict[str, str]:
        target = self.path(path)
        parts = target.relative_to(self.root).parts
        if not parts:
            raise ValueError("只能读取普通文件")
        with self._directory(parts[:-1]) as directory_fd:
            fd = self._open(parts[-1], os.O_RDONLY | os.O_NONBLOCK, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("只能读取普通文件")
                self._check_identity(info)
                if info.st_nlink != 1:
                    raise ValueError("工作区文件不能是硬链接")
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "rb") as stream:
                content = stream.read(131073)
        if len(content) > 131072:
            raise ValueError("文件超过 128 KiB，请先拆分")
        return {"path": path, "content": content.decode("utf-8")}

    def write(self, path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
        raw = content.encode("utf-8")
        if len(raw) > 131072:
            raise ValueError("内容超过 128 KiB")
        target = self.path(path)
        parts = target.relative_to(self.root).parts
        if not parts:
            raise ValueError("只能写入普通文件")
        with self._directory(parts[:-1], create=True) as directory_fd:
            try:
                info = self._entry(directory_fd, parts[-1])
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("只能写入普通文件")
            except FileNotFoundError:
                pass
            temporary = ".joyagent-" + secrets.token_hex(16)
            fd = self._open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if overwrite:
                    # renameat replaces the entry itself, never a symlink's
                    # destination. Both names stay under the pinned parent fd.
                    os.replace(temporary, parts[-1], src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                else:
                    # linkat is atomic create-without-overwrite. Do not follow a
                    # substituted temporary source symlink either.
                    os.link(temporary, parts[-1], src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd, follow_symlinks=False)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        return {"path": path, "bytes": len(raw), "written": True}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def register_builtins(registry: ToolRegistry, workspace: Workspace, memory: Any, skills: Any, timezone: str) -> None:
    string = {"type": "string", "minLength": 1, "maxLength": 1024}
    specs = [
        ("time_now", "查询当前日期时间和时区。", _schema({}),
         lambda: {"now": datetime.now(ZoneInfo(timezone)).isoformat(), "timezone": timezone}),
        ("workspace_list", "列出 workspace 内一个目录的文件（最多 200 项）。", _schema({"path": string}), workspace.list),
        ("workspace_read", "读取 workspace 内 UTF-8 文件，最大 128 KiB。", _schema({"path": string}, ["path"]), workspace.read),
        ("workspace_write", "写入 workspace 内文件。默认拒绝覆盖；用户要求修改时可显式 overwrite=true。",
         _schema({"path": string, "content": {"type": "string", "maxLength": 131072},
                  "overwrite": {"type": "boolean"}}, ["path", "content"]), workspace.write),
        ("memory_remember", "仅在用户明确要求时保存一条长期记忆/偏好。同名 key 会更新；禁止存储凭据。",
         _schema({"key": {"type": "string", "minLength": 1, "maxLength": 128},
                  "value": {"type": "string", "minLength": 1, "maxLength": 8000}}, ["key", "value"]), memory.remember),
        ("memory_search", "搜索持久化长期记忆；返回带分数的本地词法/哈希向量匹配。",
         _schema({"query": {"type": "string", "minLength": 1, "maxLength": 2000},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["query"]), memory.search),
        ("memory_forget", "用户要求时删除一条当前长期记忆。历史对话和审计日志仍会保留。",
         _schema({"key": {"type": "string", "minLength": 1, "maxLength": 128}}, ["key"]), memory.forget),
        ("skills_list", "列出可阅读的本地技能指南。", _schema({}), skills.list),
        ("skills_read", "读取一个 SKILL.md。指南不会授予任何额外权限。", _schema({"name": string}, ["name"]), skills.read),
    ]
    for name, description, parameters, handler in specs:
        registry.register(ToolSpec(name, description, parameters), handler)
