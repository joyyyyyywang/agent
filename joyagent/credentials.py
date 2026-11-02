"""Owner-controlled local credential fallback; values never enter agent memory."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tomllib

MAX_CREDENTIAL_BYTES = 16 * 1024


def _key(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(not 33 <= ord(char) <= 126 for char in value)):
        raise ValueError("模型凭据格式无效；应为不含空白或控制字符的 API Key。")
    return value


def resolve_api_key(kind: str, base_url: str, key_env: str,
                    credentials_file: str | None = None) -> tuple[str, str]:
    """Environment wins; a private file can supply an exact service/URL fallback."""
    environment = os.environ.get(key_env, "").strip()
    if environment:
        return _key(environment), "environment"
    if credentials_file is None:
        return "", "missing"
    if not isinstance(credentials_file, str) or not credentials_file.strip():
        raise ValueError("credentials_file 必须是非空文件路径。")
    try:
        path = Path(credentials_file).expanduser()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return "", "missing"
    except (OSError, RuntimeError, ValueError):
        raise ValueError("无法读取模型凭据文件；请检查路径和文件权限。") from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("模型凭据必须存放在普通文件中。")
            if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise ValueError("模型凭据文件须由当前用户拥有，权限应为 600 或 400。")
            raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
    except OSError:
        raise ValueError("无法读取模型凭据文件。") from None
    if len(raw) > MAX_CREDENTIAL_BYTES:
        raise ValueError("模型凭据文件不能超过 16 KiB。")
    try:
        entries = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("模型凭据文件必须是有效的 UTF-8 TOML。") from None
    if kind not in entries:
        return "", "missing"
    entry = entries[kind]
    if not isinstance(entry, dict) or not isinstance(entry.get("base_url"), str):
        raise ValueError("模型凭据条目须包含 base_url 和 api_key。")
    if entry["base_url"].rstrip("/") != base_url.rstrip("/"):
        return "", "missing"
    return _key(entry.get("api_key")), "file"
