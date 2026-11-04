"""Command line entry point; the optional TUI has a separate Node runtime."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .channels.common import validate_input
from .config import Config, load_config


def initialize(directory: str) -> int:
    root = Path(directory).expanduser().resolve()
    source = files("joyagent").joinpath("defaults")
    pending: list[tuple[Any, Path]] = []

    def collect(folder: Any, target: Path) -> None:
        for item in folder.iterdir():
            if item.is_dir():
                collect(item, target / item.name)
            elif item.is_file():
                pending.append((item, target / item.name))

    collect(source, root)
    conflicts = [str(target) for _, target in pending if target.exists() or target.is_symlink()]
    if conflicts:
        raise ValueError("初始化不会覆盖已有文件：\n" + "\n".join(conflicts))
    for _, target in pending:
        parent = target.parent
        while parent != root.parent:
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError(f"初始化目录不可用：{parent}")
            if parent == root:
                break
            parent = parent.parent
    for item, target in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation also protects against files appearing after preflight.
        with target.open("xb") as handle:
            handle.write(item.read_bytes())
    (root / "workspace").mkdir(exist_ok=True)
    print(f"已初始化：{root}\n运行：python -m joyagent --config {root / 'agent.toml'} chat")
    print("默认聊天使用百炼 Token Plan deepseek-v4.1-flash，自动路由关闭。聊天优先读取 BAILIAN_TOKEN_PLAN_API_KEY，否则使用配置的百炼 Token Plan 私有凭据文件。")
    print("独立决策工具同样使用百炼 Token Plan，与聊天共用 BAILIAN_TOKEN_PLAN_API_KEY 或配置的百炼 Token Plan 私有凭据文件。")
    print('离线使用请将整个 [model] 段替换为：\n[model]\nprovider = "demo"\nmodel = "demo"')
    print("离线时请同时设置 [routing] enabled = false 和 [decision] enabled = false；启用自动路由或决策工具时仍需配置对应网关的凭据。")
    return 0


def doctor(config: Config) -> int:
    """Validate local configuration only: never open a socket or start MCP."""
    from .policy import Policy
    from .providers import create_provider
    from .decisions import DecisionClient, decision_settings
    from .routing import validate_routing
    from .types import ProviderError

    errors = 0

    def report(ok: bool, message: str) -> None:
        nonlocal errors
        errors += not ok
        print(f"{'OK' if ok else 'FAIL'}  {message}")

    print(f"配置：{config.path}")
    try:
        timezone = config.get("agent", "timezone", "Asia/Shanghai")
        ZoneInfo(timezone)
        report(True, f"时区：{timezone}")
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        report(False, "agent.timezone 不是有效 IANA 时区")
    dimensions = config.get("memory", "vector_dimensions", 256)
    report(type(dimensions) is int and 16 <= dimensions <= 4096, "memory.vector_dimensions 须为 16–4096 的整数")
    report(config.soul_path.is_file(), f"人格文件：{config.soul_path}")
    try:
        Policy.load(config.policy_path)
        report(True, f"策略文件：{config.policy_path}")
    except (OSError, ValueError, TypeError) as exc:
        report(False, f"策略文件：{exc}")
    report(config.skills_dir.is_dir(), f"技能目录：{config.skills_dir}")
    for label, path in (("工作目录", config.workspace), ("数据目录", config.data_dir)):
        if path.exists():
            report(path.is_dir() and os.access(path, os.W_OK), f"{label}：{path}")
        else:
            ancestor = path.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            report(ancestor.is_dir() and os.access(ancestor, os.W_OK), f"{label}：{path}（首次运行创建）")
    try:
        provider = create_provider(config.select_model())
        kind = config.get("model", "provider", "demo")
        report(True, f"模型适配器：{kind}")
        if kind != "demo":
            env = provider.key_env
            required = provider.requires_api_key
            key, source = provider.credential()
            if source == "file":
                report(True, f"模型凭据：本机默认凭据文件已设置（{env} 优先）")
            else:
                report(bool(key) or not required, f"模型凭据：{env} {'已设置' if key else '未设置，且无匹配的本机默认凭据' + ('（本地模型可选）' if not required else '')}")
    except (ValueError, TypeError) as exc:
        report(False, f"模型配置：{exc}")
    try:
        decision = decision_settings(config)
        if decision:
            DecisionClient(decision).credential_source()
            report(True, "决策工具配置和凭据已就绪（未联网）")
        else:
            print("OFF  决策工具未启用")
    except (ValueError, TypeError, OSError, ProviderError) as exc:
        report(False, f"决策工具配置：{exc}")
    try:
        routing = validate_routing(config)
        if routing:
            report(True, f"自动路由：复杂且该类别概率 ≥ {routing['min_probability']:g} → {routing['complex_model']}；简单、不确定或分类失败 → {routing['simple_model']}（未联网）")
        else:
            print("OFF  自动路由未启用")
    except (ValueError, TypeError, OSError, ProviderError) as exc:
        report(False, f"自动路由配置：{exc}")
    from types import SimpleNamespace
    for name in ("telegram", "email"):
        enabled = bool(config.get(name, "allowed_user_ids", [])) if name == "telegram" else config.get(name, "enabled", False)
        if not enabled:
            print(f"OFF  {name} 通道未启用")
            continue
        try:
            if name == "telegram":
                from .channels.telegram import TelegramChannel
                TelegramChannel(SimpleNamespace(config=config))
            else:
                from .channels.email import EmailChannel
                EmailChannel(SimpleNamespace(config=config))
            report(True, f"{name} 通道配置和环境变量已就绪（未联网）")
        except (ValueError, OSError, TypeError) as exc:
            report(False, f"{name}：{exc}")
    from .execution import execution_settings, validate_shared_mcp
    try:
        execution = execution_settings(config)
        if config.get("web", "mode", "local") == "shared":
            validate_shared_mcp(config)
        if execution["enabled"]:
            report(sys.platform == "linux", "隔离执行需要 Linux（未启动进程验证 namespace）")
            for field in ("bwrap_path", "prlimit_path"):
                path = Path(execution[field])
                report(path.is_file() and os.access(path, os.X_OK), f"执行器 {field}：{path}")
        else:
            print("OFF  命令和脚本执行未启用")
    except (ValueError, TypeError, OSError) as exc:
        report(False, f"执行器配置：{exc}")
    for name, entry in config.data.get("mcp", {}).get("servers", {}).items():
        if entry.get("enabled", False):
            command = entry.get("command", "")
            report(bool(command and shutil.which(command)), f"MCP {name}：{command}（只检查可执行文件，未启动）")
    print("本检查未访问网络；不代表模型、Telegram 或邮件账号已连通。")
    return 1 if errors else 0


async def chat(config: Config, message: str | None, session: str) -> int:
    from .runtime import build_runtime

    validate_input(message if message is not None else "session", session)
    runtime = await build_runtime(config)
    try:
        if message is not None:
            result = await runtime.chat(message, session_id=session)
            print(result.text)
            return 1 if result.stopped else 0
        print(f"joyagent · 会话 {session} · /exit 退出，/session NAME 切换会话")
        while True:
            try:
                # No model turn is in flight while waiting for terminal input.
                text = input("\n你 > ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                break
            if text.startswith("/session "):
                try:
                    _, session = validate_input("session", text[9:].strip())
                    print(f"已切换到会话：{session}")
                except ValueError as exc:
                    print(f"错误：{exc}", file=sys.stderr)
                continue
            try:
                validate_input(text, session)
                result = await runtime.chat(text, session_id=session)
                print(f"\nAgent > {result.text}")
                if result.tool_events:
                    print(f"  [{len(result.tool_events)} 次工具活动 · {result.steps} 步]")
            except Exception as exc:
                print(f"错误：{exc}", file=sys.stderr)
        return 0
    finally:
        await runtime.close()


async def run_channel(config: Config, name: str) -> int:
    from .channels.email import EmailChannel
    from .channels.telegram import TelegramChannel
    from .runtime import build_runtime

    # Validate channel opt-in, credentials and allowlist before starting runtime.
    channel_type = TelegramChannel if name == "telegram" else EmailChannel
    from types import SimpleNamespace
    channel_type(SimpleNamespace(config=config))
    runtime = await build_runtime(config)
    try:
        await channel_type(runtime).run()
        return 0
    finally:
        await runtime.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="joyagent", description="六层个人 Agent：模型、循环、工具、记忆、人格、通道")
    result.add_argument("--config", default="agent.toml", help="配置文件路径（默认 agent.toml）")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="创建默认配置，不覆盖已有文件")
    init.add_argument("directory", nargs="?", default=".")
    chat_parser = commands.add_parser("chat", help="命令行对话")
    chat_parser.add_argument("-m", "--message", help="发送单条消息后退出")
    chat_parser.add_argument("--session", default="default")
    tui = commands.add_parser("tui", help="启动基于 pi-tui 的终端界面（需要 Node.js）")
    tui.add_argument("--session", default="default", help="恢复指定会话，与 chat 共用会话名")
    tui.add_argument("--install", action="store_true", help="安装锁定的 TUI 依赖后退出（需要网络）")
    web = commands.add_parser("web", help="启动 Web 聊天界面（默认本机；团队使用需配置 shared 模式）")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    commands.add_parser("telegram", help="启动允许用户的 Telegram 私聊通道")
    commands.add_parser("email", help="启动明确启用的邮件通道")
    commands.add_parser("doctor", help="检查本地配置，不联网、不启动 MCP")
    # Also accept the natural `joyagent chat --config ...` spelling.
    for command in commands.choices.values():
        command.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            return initialize(args.directory)
        if args.command == "tui" and args.install:
            from .channels.tui import install_tui
            return install_tui()
        config = load_config(args.config)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "chat":
            return asyncio.run(chat(config, args.message, args.session))
        if args.command == "tui":
            from .channels.tui import launch_tui
            return launch_tui(config, args.session)
        if args.command == "web":
            from .channels.web import serve_web
            serve_web(config, args.host, args.port)
            return 0
        return asyncio.run(run_channel(config, args.command))
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
