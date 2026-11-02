"""Launch the optional Node terminal UI without adding Python dependencies."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .common import validate_input
from ..config import Config


TUI_DIR = Path(__file__).resolve().parents[1] / "tui"


def node_executable() -> str:
    node = shutil.which("node")
    if not node:
        raise ValueError("TUI 需要 Node.js 22.19 或更新版本；普通 chat 命令无需 Node.js。")
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("检查 Node.js 版本超时。") from exc
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-[\w.-]+)?\s*", version.stdout)
    if version.returncode or not match or tuple(map(int, match.groups())) < (22, 19, 0):
        raise ValueError("TUI 需要 Node.js 22.19 或更新版本，请升级 Node.js。")
    return node


def install_tui() -> int:
    """Explicit opt-in installation; normal launches never install or fetch code."""
    node_executable()
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("未找到 npm，请安装带 npm 的 Node.js 后重试。")
    result = subprocess.run(
        [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=TUI_DIR,
    )
    if result.returncode == 0:
        print("TUI 依赖已安装。运行：python3 -m joyagent tui")
    return result.returncode


def launch_tui(config: Config, session: str) -> int:
    validate_input("session", session)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("TUI 需要交互终端；脚本或管道请使用 joyagent chat -m '消息'。")
    node = node_executable()
    if not (TUI_DIR / "node_modules/@earendil-works/pi-tui/package.json").is_file():
        raise ValueError("尚未安装 TUI 依赖，请先运行：python3 -m joyagent tui --install")
    # Replace the launcher so terminal signals reach the UI directly. No shell
    # interpolation: paths and session names remain separate process arguments.
    os.execv(node, [node, str(TUI_DIR / "main.mjs"), "--python", sys.executable,
                   "--config", str(config.path), "--session", session])
    return 0
