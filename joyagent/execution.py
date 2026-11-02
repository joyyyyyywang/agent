"""Opt-in Linux process tools. Never fall back to an unsandboxed process."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
from typing import Any

from .types import ToolExecutionResult, ToolSpec


ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/workspace",
       "TMPDIR": "/tmp", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
SYSTEM_PATHS = ("/usr", "/bin", "/lib", "/lib64")
# Some distributions route the linker through /etc/alternatives even though
# both its public entry and real executable live under the mounted /usr tree.
# Expose only these fixed executable aliases, never the alternatives directory.
COMPILER_ALIASES = ("/etc/alternatives/ld", "/etc/alternatives/as")
# Only public resolver settings and trust roots; never mount all of /etc or
# private TLS keys. Symlink sources are dereferenced at their fixed target.
NETWORK_PATHS = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
                 "/etc/ssl/certs", "/etc/ssl/cert.pem", "/etc/pki/tls/certs",
                 "/etc/pki/tls/cert.pem", "/etc/pki/ca-trust/extracted")


def execution_settings(config: Any) -> dict:
    raw = config.data.get("execution", {})
    if not isinstance(raw, dict) or type(raw.get("enabled", False)) is not bool:
        raise ValueError("execution.enabled 必须为布尔值")
    defaults = {"enabled": False, "network": "none", "timeout_seconds": 10, "output_chars": 8000,
                "memory_mb": 256, "file_size_mb": 16, "process_limit": 16,
                "bwrap_path": "/usr/bin/bwrap", "prlimit_path": "/usr/bin/prlimit"}
    if set(raw) - set(defaults):
        raise ValueError("execution 包含不支持的配置项")
    settings = {**defaults, **raw}
    if not isinstance(settings["network"], str) or settings["network"] not in {"none", "host"}:
        raise ValueError('execution.network 必须为 "none" 或 "host"')
    for name, minimum, maximum in (("timeout_seconds", 1, 60), ("output_chars", 256, 64000),
                                   ("memory_mb", 64, 1024), ("file_size_mb", 1, 128),
                                   ("process_limit", 4, 4096)):
        value = settings[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"execution.{name} 必须为 {minimum}–{maximum} 的整数")
    for name in ("bwrap_path", "prlimit_path"):
        if not isinstance(settings[name], str) or not Path(settings[name]).is_absolute():
            raise ValueError(f"execution.{name} 必须为可信程序的绝对路径")
    return settings


def validate_shared_mcp(config: Any) -> None:
    mcp = config.data.get("mcp", {})
    servers = mcp.get("servers", {}) if isinstance(mcp, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("MCP servers 必须是表")
    for settings in servers.values():
        if not isinstance(settings, dict) or type(settings.get("enabled", False)) is not bool:
            raise ValueError("MCP enabled 必须为布尔值")
        if not settings.get("enabled", False):
            continue
        if settings.get("shared") is not True or settings.get("sandbox") is not True:
            raise ValueError("共享 Web MCP 必须显式配置 shared=true 和 sandbox=true")
        if not execution_settings(config)["enabled"]:
            raise ValueError("共享 Web MCP 需要启用隔离执行器 execution.enabled")
        if settings.get("env") or settings.get("pass_env"):
            raise ValueError("共享 Web 隔离 MCP 不允许继承或配置环境凭据")


class SandboxExecutor:
    def __init__(self, settings: dict, workspace: Any):
        self.settings = settings
        self.workspace = workspace
        self._checked = False
        self._check_lock = asyncio.Lock()
        if sys.platform != "linux":
            raise ValueError("命令/脚本隔离执行仅支持 Linux + bubblewrap；不会退回宿主执行")
        for key in ("bwrap_path", "prlimit_path"):
            path = Path(settings[key])
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError("隔离执行需要安装 bubblewrap 和 util-linux(prlimit)")
            info = path.stat()
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("隔离程序必须属于 root 且不可被组或其他用户修改")
        root = workspace.root
        for protected in workspace.protected:
            if protected == root or protected.is_relative_to(root) or root.is_relative_to(protected):
                raise ValueError("执行工作区不得与配置、凭据、技能或记忆目录重叠")
            if any(protected.is_relative_to(Path(path).resolve()) for path in SYSTEM_PATHS):
                raise ValueError("执行器的系统运行库目录不得包含 Agent 私有配置或状态")

    def launch(self, argv: list[str], cwd: str = ".", *, script: Path | None = None) -> tuple[list[str], dict[str, str]]:
        # Only the directory is accepted from a user, never a host mount/source.
        directory = self.workspace.path(cwd)
        try:
            with self.workspace._directory(directory.relative_to(self.workspace.root).parts):
                pass
        except OSError:
            raise ValueError("执行目录必须是 workspace 内已有目录") from None
        relative = directory.relative_to(self.workspace.root).as_posix()
        target_cwd = "/workspace" if relative == "." else "/workspace/" + relative
        bwrap = [self.settings["bwrap_path"], "--unshare-all", "--unshare-user",
                 "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
        if self.settings.get("network", "none") == "host":
            # Keep user/mount/PID isolation while using the deployment sandbox's
            # network namespace, including its loopback and platform egress rules.
            bwrap += ["--share-net"]
            if not Path("/etc/resolv.conf").is_file():
                raise ValueError("共享外层网络需要 /etc/resolv.conf DNS 配置")
            for name in NETWORK_PATHS:
                path = Path(name)
                if not path.exists():
                    continue
                source = path.resolve(strict=True)
                if any(source == private or source.is_relative_to(private)
                       or private.is_relative_to(source)
                       for private in [self.workspace.root, *self.workspace.protected]):
                    raise ValueError("网络配置或证书路径不得与工作区或 Agent 私有目录重叠")
                bwrap += ["--ro-bind", str(source), name]
        for name in SYSTEM_PATHS:
            path = Path(name)
            if path.is_symlink():
                bwrap += ["--symlink", os.readlink(path), name]
            elif path.exists():
                bwrap += ["--ro-bind", name, name]
        for name in COMPILER_ALIASES:
            path = Path(name)
            if not path.exists():
                continue
            source = path.resolve(strict=True)
            info = source.stat()
            if (not source.is_relative_to(Path("/usr")) or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 0 or info.st_mode & 0o022 or not info.st_mode & 0o111
                    or any(source == private or source.is_relative_to(private)
                           or private.is_relative_to(source)
                           for private in [self.workspace.root, *self.workspace.protected])):
                raise ValueError("编译工具别名必须指向 /usr 内 root 管理的只读可执行文件")
            bwrap += ["--ro-bind", str(source), name]
        # No procfs: nested containers may forbid mounting it, and workloads do
        # not need process/environment inspection. Never bind the host /proc.
        bwrap += ["--dir", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                  "--dir", "/home", "--bind", str(self.workspace.root), "/workspace"]
        if script is not None:
            bwrap += ["--dir", "/mcp", "--ro-bind", str(script), "/mcp/server.py"]
        # Apply limits to the workload after bwrap creates its namespaces.
        # No preexec_fn in this threaded server. RLIMIT_NPROC is a real-UID
        # limit, not a per-visitor process quota; other host tasks may count.
        # Hard and soft limits match, so the child cannot raise its own limits.
        options = {"as": self.settings["memory_mb"] * 1024 * 1024,
                   "fsize": self.settings["file_size_mb"] * 1024 * 1024,
                   "nproc": self.settings["process_limit"], "nofile": 128, "core": 0}
        bwrap += ["--chdir", target_cwd, "--", self.settings["prlimit_path"],
                  *(f"--{key}={value}:{value}" for key, value in options.items()), "--", *argv]
        return bwrap, dict(ENV)

    async def check(self) -> None:
        async with self._check_lock:
            if self._checked:
                return
            result = await self.run(["/bin/true"], _checking=True)
            if result["exit_code"] != 0 or result["timed_out"]:
                raise ValueError("隔离执行器检查失败；需要可用的 Linux namespace 和配置的网络模式")
            self._checked = True

    async def run(self, argv: list[str], cwd: str = ".", *, _checking: bool = False) -> dict:
        if not _checking:
            await self.check()
        command, env = self.launch(argv, cwd)
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd="/", start_new_session=True,
        )
        chunks = {"stdout": bytearray(), "stderr": bytearray()}
        limit = self.settings["output_chars"] * 4
        truncated = False

        async def drain(name: str, stream: Any) -> None:
            nonlocal truncated
            while part := await stream.read(4096):
                remaining = max(0, limit - sum(len(value) for value in chunks.values()))
                chunks[name].extend(part[:remaining])
                truncated |= len(part) > remaining

        readers = [asyncio.create_task(drain("stdout", process.stdout)),
                   asyncio.create_task(drain("stderr", process.stderr))]
        timed_out = False
        try:
            async with asyncio.timeout(self.settings["timeout_seconds"]):
                await process.wait()
                await asyncio.gather(*readers)
        except TimeoutError:
            timed_out = True
        finally:
            # Kill the bwrap supervisor as well as its group on timeout/cancel.
            # Its isolated PID namespace is reaped, including background children.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        output = {name: data.decode("utf-8", errors="replace") for name, data in chunks.items()}
        budget = self.settings["output_chars"]
        for name in ("stdout", "stderr"):
            truncated |= len(output[name]) > budget
            output[name] = output[name][:budget]
            budget -= len(output[name])
        return {**output, "exit_code": process.returncode, "timed_out": timed_out,
                "truncated": truncated, "cwd": cwd,
                "isError": timed_out or process.returncode != 0}

    async def command_run(self, command: str, cwd: str = ".") -> ToolExecutionResult:
        result = await self.run(["/bin/bash", "--noprofile", "--norc", "-c", command], cwd)
        return ToolExecutionResult(not result.pop("isError"), result)

    async def script_run(self, code: str, language: str = "python", cwd: str = ".") -> ToolExecutionResult:
        if language == "python":
            python = shutil.which("python3", path=ENV["PATH"])
            if python is None:
                raise ValueError("隔离运行库没有 Python 3")
            argv = [python, "-I", "-c", code]
        elif language == "bash":
            argv = ["/bin/bash", "--noprofile", "--norc", "-c", code]
        else:
            raise ValueError("脚本语言仅支持 python 或 bash")
        result = await self.run(argv, cwd)
        return ToolExecutionResult(not result.pop("isError"), result)

    def mcp_launch(self, config: Any, settings: dict) -> tuple[list[str], dict[str, str]]:
        if settings.get("env") or settings.get("pass_env"):
            raise ValueError("隔离 MCP 不允许继承或配置环境凭据")
        source = settings.get("script")
        if not isinstance(source, str) or not source:
            raise ValueError("隔离 MCP 需要 owner 配置 script 文件")
        script = config.resolve(source)
        info = script.stat()
        if (not script.is_file() or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 or script.is_relative_to(self.workspace.root)
                or any(script == p or script.is_relative_to(p) for p in self.workspace.protected)):
            raise ValueError("MCP script 必须是工作区外、owner 管理的只读程序文件")
        command = settings.get("command", "python3")
        executable = shutil.which(command, path=ENV["PATH"])
        if executable is None or not Path(executable).resolve().is_relative_to(Path("/usr")):
            raise ValueError("隔离 MCP command 必须位于系统 /usr 运行库")
        args = settings.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("MCP args 必须是字符串列表")
        return self.launch([executable, "/mcp/server.py", *args], script=script)


def register_execution(registry: Any, executor: SandboxExecutor) -> None:
    network = "与外层沙箱共用网络" if executor.settings.get("network", "none") == "host" else "无网络"
    common = {"cwd": {"type": "string", "minLength": 1, "maxLength": 1024,
                      "description": "工作区内已有目录，默认 ."}}
    for name, description, properties, required, handler in (
        ("command_run", f"在隔离 Linux 工作区执行 Bash 命令，{network}。返回退出码、stdout、stderr。",
         {"command": {"type": "string", "minLength": 1, "maxLength": 10000}, **common}, ["command"], executor.command_run),
        ("script_run", f"在隔离工作区运行 Python 或 Bash 脚本，{network}。文件保存在自己的工作区。",
         {"code": {"type": "string", "minLength": 1, "maxLength": 10000},
          "language": {"type": "string", "enum": ["python", "bash"]}, **common}, ["code"], executor.script_run),
    ):
        registry.register(ToolSpec(name, description, {"type": "object", "properties": properties,
                          "required": required, "additionalProperties": False}), handler)
