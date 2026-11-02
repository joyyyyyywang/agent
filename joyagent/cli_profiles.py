"""Owner-generated configuration for isolated external agent processes.

These profiles contain only a loopback broker address and a disposable token.
They are not a sandbox: callers must mount the generated configuration read-only,
give the child a clean environment, and confine its filesystem and network.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


DSH_VERSION = "0.2.1-rc.2"
DSH_STATE_DIRECTORIES = ("sessions", "storages", "attachments", "logs", "spill", "cache")
# The pinned launcher unconditionally truncates this generated empty include
# root before loading plugins. It is not an owner patch. Give only this file a
# writable bind; use a fresh home for each invocation and keep HMR disabled.
DSH_WRITABLE_FILES = ("profiles/headless/cordis.yml",)

# These are ids from the pinned, published dsh-base profile. In particular,
# disabling title generation avoids a second, otherwise implicit model request.
_DSH_DISABLED = (
    "hmr", "plugin-manager", "tool-plugin-manager", "config-editor", "settings",
    "credentials", "authorization", "deepseek-account", "llm-deepseek",
    "llm-deepseek-account", "session-title-llm", "session-telemetry-otel",
    "plugin-package-inventory-deepseek", "agent-instructions", "skill-filesystem",
    "tool-skill", "tool-web", "web-search-deepseek", "web-deepseek-api",
    "web-fetch-http", "tool-subagent", "tool-subagent-fork",
    "tool-subagent-control", "tool-subagent-list-agents", "subagent-spawn-in-process",
    "subagent-fork-in-process", "tool-workflow", "workflow-ptc", "ptc-runtime",
)


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Agent 配置目录不能是符号链接")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.geteuid():
        raise ValueError("Agent 配置目录必须由服务所有者管理")
    path.chmod(0o700)


def _write_config(path: Path, data: object) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _write_text(path: Path, contents: str) -> None:
    """Atomically replace a generated file without following a target symlink."""
    _private_directory(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=".joyagent-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(contents)
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str) or any(ord(char) < 32 for char in endpoint):
        raise ValueError("Agent 模型地址必须是本地 broker")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Agent 模型地址必须是本地 broker") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or port is None or port < 1 or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") != "/v1"):
        raise ValueError("Agent 模型地址必须是 http://127.0.0.1:端口/v1")


def build_profile(
    backend: str, model: str, endpoint: str, home: Path, install_root: Path,
    *, broker_token: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Prepare a pinned CLI profile and return argv plus explicit env additions.

    ``install_root`` is the npm prefix containing ``node_modules/.bin/dsh``.
    Absolute paths must refer to the same locations inside the child namespace.
    ``home`` must be a fresh, service-owned temporary directory, not a visitor's
    workspace. Only ``DSH_STATE_DIRECTORIES`` and ``DSH_WRITABLE_FILES`` need
    writes below ``home/dsh``; its other paths must remain read-only.
    The caller owns the subprocess lifecycle and passes the prompt on stdin.
    """
    if broker_token is None:
        broker_token = secrets.token_hex(32)
    if (not isinstance(broker_token, str) or len(broker_token) != 64
            or any(char not in "0123456789abcdef" for char in broker_token)):
        raise ValueError("Agent 本地转发凭据无效")
    if (not isinstance(model, str) or not model.strip() or len(model) > 256
            or any(ord(char) < 32 for char in model)):
        raise ValueError("Agent 模型名称无效")
    if backend in {"codex", "pi"}:
        return _other_profile(backend, model, endpoint, home, install_root, broker_token)
    if backend != "dsh":
        raise ValueError(f"未支持的 Agent profile：{backend}")
    _validate_endpoint(endpoint)
    home, install_root = Path(home).absolute(), Path(install_root).absolute()
    _private_directory(home)
    dsh_home = home / "dsh"
    profile = dsh_home / "profiles" / "headless"
    for directory in (dsh_home, dsh_home / "profiles", profile):
        _private_directory(directory)
    for directory in DSH_STATE_DIRECTORIES:
        _private_directory(dsh_home / directory)
    # Pre-create the shipped manifest so startup never needs to install or
    # initialize a profile. The CLI resolves both bundles from its installation.
    _write_config(profile / "package.json", {
        "name": "joyagent-dsh-profile", "private": True,
        "dsh": {"profile": {"bundles": [
            "@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless",
        ]}},
    })
    _write_config(profile / "cordis.patch.yml", [])
    _write_config(profile / "cordis.yml", [])
    _write_config(dsh_home / "cordis.patch.yml", [])
    patches: list[dict[str, object]] = [
        {"id": name, "disabled": True} for name in _DSH_DISABLED
    ]
    patches.extend([
        {"id": "session-log-deepseek", "config": {"enabled": False}},
        {"id": "agent-default-model", "config": {
            "provider": "joyagent", "model": model,
        }},
        {"id": "llm-pi-ai", "config": {"providers": {"joyagent": {
            "api": "openai-completions", "baseURL": endpoint,
            "apiKeyEnv": "JOYAGENT_BROKER_TOKEN",
            "retryPolicy": {"mode": "normal", "maxRetries": 0},
            "models": [{"id": model, "contextWindow": 65536,
                        "maxTokens": 16384, "input": ["text"]}],
            "compat": {"maxTokensField": "max_tokens", "supportsStore": False,
                       "supportsDeveloperRole": False, "supportsStrictMode": False,
                       "supportsReasoningEffort": False},
        }}}},
        # The outer namespace provides isolation. A nested DSH sandbox may not
        # be available in a container and must never broaden the outer boundary.
        {"id": "sandbox-policy", "config": {
            "mode": "danger-full-access", "workspaceRoot": "/workspace",
        }},
        {"id": "approval", "config": {"policy": "never"}},
    ])
    overlay = dsh_home / "joyagent.patch.yml"
    _write_config(overlay, patches)
    argv = [str(install_root / "node_modules" / ".bin" / "dsh"),
            "--profile", "headless", "--patch", str(overlay), "--json", "-"]
    return argv, {
        "DSH_HOME": str(dsh_home), "DSH_TELEMETRY_DISABLED": "1",
        "DSH_PERMISSION_MODE": "danger-full-access", "DSH_PRIMARY_RUNTIME": "",
        "JOYAGENT_BROKER_TOKEN": broker_token,
    }


def _other_profile(backend: str, model: str, endpoint: str, home: Path,
                   install_root: Path, broker_token: str) -> tuple[list[str], dict[str, str]]:
    _validate_endpoint(endpoint)
    home, install_root = Path(home).absolute(), Path(install_root).absolute()
    _private_directory(home)
    binary = str(install_root / "node_modules/.bin" / backend)
    env = {"JOYAGENT_BROKER_TOKEN": broker_token}
    if backend == "codex":
        codex_home = home / "codex"
        _private_directory(codex_home)
        env["CODEX_HOME"] = str(codex_home)
        argv = [binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--ignore-user-config", "--ignore-rules", "--color", "never", "-C", "/workspace",
                "-m", model, "-s", "danger-full-access"]
        options = {"model_provider": "joyagent", "model_providers.joyagent.name": "joyagent",
                   "model_providers.joyagent.base_url": endpoint,
                   "model_providers.joyagent.wire_api": "responses",
                   "model_providers.joyagent.env_key": "JOYAGENT_BROKER_TOKEN",
                   "model_providers.joyagent.supports_websockets": False,
                   "web_search": "disabled", "project_doc_max_bytes": 0,
                   "approval_policy": "never", 'projects."/workspace".trust_level': "untrusted"}
        for feature in ("hooks", "plugins", "multi_agent", "multi_agent_v2", "apps",
                        "in_app_browser", "shell_snapshot", "skill_mcp_dependency_install"):
            options["features." + feature] = False
        for key, value in options.items():
            argv += ["-c", key + "=" + json.dumps(value)]
        return argv + ["-"], env
    if backend == "pi":
        pi_home = home / "pi"
        _write_config(pi_home / "models.json", {"providers": {"joyagent": {
            "baseUrl": endpoint, "api": "openai-completions", "apiKey": "${JOYAGENT_BROKER_TOKEN}",
            "models": [{"id": model, "contextWindow": 65536, "maxTokens": 16384,
                        "reasoning": False, "input": ["text"], "compat": {
                            "maxTokensField": "max_tokens", "supportsStore": False,
                            "supportsDeveloperRole": False}}],
        }}})
        env.update({"PI_CODING_AGENT_DIR": str(pi_home), "PI_OFFLINE": "1"})
        return [binary, "--print", "--mode", "json", "--no-session", "--no-extensions",
                "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-context-files",
                "--no-themes", "--no-approve", "--offline", "--tools", "read,bash,edit,write,grep,find,ls",
                "--provider", "joyagent", "--model", model], env
    raise ValueError(f"未支持的 Agent profile：{backend}")


def build_resident_profile(backend: str, model: str, endpoint: str, home: Path,
                           install_root: Path, models: tuple[str, ...], *,
                           broker_token: str | None = None) -> tuple[list[str], dict[str, str]]:
    """Expose only owner models to a long-lived native protocol process."""
    if (not isinstance(models, (tuple, list)) or not models or len(models) > 32 or model not in models
            or any(not isinstance(item, str) or not item.strip() or len(item) > 256
                   or any(ord(char) < 32 for char in item) for item in models)
            or len(set(models)) != len(models)):
        raise ValueError("常驻 Agent 模型列表无效")
    argv, env = build_profile("dsh" if backend == "deepseek" else backend,
                              model, endpoint, home, install_root, broker_token=broker_token)
    if backend == "codex":
        env["CODEX_SQLITE_HOME"] = env["CODEX_HOME"]
        options = [value for index, value in enumerate(argv) if index and argv[index - 1] == "-c"]
        argv = [argv[0], "app-server", "--listen", "stdio://"]
        for option in [*options, "model=" + json.dumps(model), 'sandbox_mode="danger-full-access"',
                       "model_providers.joyagent.request_max_retries=0",
                       "model_providers.joyagent.stream_max_retries=0"]:
            argv += ["-c", option]
        return argv, env
    if backend == "pi":
        path = home / "pi/models.json"
        data = json.loads(path.read_text())
        provider = data["providers"]["joyagent"]
        template = provider["models"][0]
        provider["models"] = [{**template, "id": item} for item in models]
        _write_config(path, data)
        argv.remove("--print")
        argv[argv.index("--mode") + 1] = "rpc"
        argv.remove("--no-session")
        sessions = home / "pi/sessions"
        sessions.mkdir(mode=0o700)
        env["JOYAGENT_PI_SESSION_DIR"] = str(sessions)
        argv += ["--session-dir", str(sessions)]
        return argv, env
    dsh = home / "dsh"
    profile = dsh / "profiles/acp"
    _write_config(profile / "package.json", {"name": "joyagent-dsh-acp", "private": True,
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app"]}}})
    _write_config(profile / "cordis.patch.yml", [])
    _write_config(profile / "cordis.yml", [])
    path = dsh / "joyagent.patch.yml"
    patches = json.loads(path.read_text())
    for patch in patches:
        if patch["id"] == "llm-pi-ai":
            provider = patch["config"]["providers"]["joyagent"]
            template = provider["models"][0]
            provider["models"] = [{**template, "id": item} for item in models]
    patches += [{"id": "llm-retry", "disabled": True},
                # ACP close drains its log, but the optional projection cache
                # writes after disposal without an awaited deletion barrier.
                {"id": "session-projection-cache", "disabled": True},
                {"id": "spill-local", "config": {"root": str(dsh / "spill"), "cleanupPeriodDays": 0}},
                {"id": "acp", "config": {"provider": "joyagent", "model": model}}]
    _write_config(path, patches)
    return [argv[0], "--profile", "acp", "--patch", str(path)], env
