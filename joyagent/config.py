"""TOML configuration, with all relative paths rooted at the config file."""

from __future__ import annotations

import os
import re
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Config:
    path: Path
    data: dict[str, Any]

    @property
    def root(self) -> Path:
        return self.path.parent

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)

    def resolve(self, value: str) -> Path:
        path = Path(os.path.expandvars(value)).expanduser()
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    @property
    def model_settings(self) -> dict[str, Any]:
        raw = self.data.get("model", {})
        if not isinstance(raw, dict):
            raise ValueError("[model] 必须是 TOML 表")
        settings = deepcopy(raw)
        value = settings.get("credentials_file")
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("credentials_file 必须是非空文件路径。")
            path = Path(os.path.expandvars(value)).expanduser()
            # Do not resolve the final symlink: the credential reader rejects it.
            settings["credentials_file"] = str(path if path.is_absolute() else self.root / path)
        return settings

    @staticmethod
    def _model_id(value: Any) -> str:
        if (not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_.:@-]{0,199}", value)
                or "://" in value):
            raise ValueError("模型名称必须为 1–200 个字母、数字、/、_、-、.、:、@，以字母或数字开头。")
        return value

    @property
    def model_choices(self) -> tuple[str, ...]:
        """Only the owner configuration can grant access to additional models."""
        settings = self.model_settings
        default = settings.get("model", "demo" if settings.get("provider", "demo") == "demo" else None)
        default = self._model_id(default)
        choices = settings.get("choices", [default])
        if not isinstance(choices, list) or not 1 <= len(choices) <= 32:
            raise ValueError("model.choices 必须是包含 1–32 个模型名称的列表。")
        choices = tuple(self._model_id(value) for value in choices)
        if len(set(choices)) != len(choices):
            raise ValueError("model.choices 不能包含重复的模型名称。")
        if default not in choices:
            raise ValueError("默认 model 必须包含在 model.choices 中。")
        return choices

    def select_model(self, value: str | None = None) -> dict[str, Any]:
        """Copy trusted settings, changing only an allowlisted model ID."""
        choices = self.model_choices
        settings = self.model_settings
        default = settings.get("model", "demo")
        selected = default if value is None else self._model_id(value)
        if selected not in choices:
            raise ValueError("该模型未由服务管理员配置为可选模型。")
        settings["model"] = selected
        return settings

    @property
    def model_selection_choices(self) -> tuple[str, ...]:
        from .routing import routing_settings
        choices = self.model_choices
        return ("auto", *choices) if routing_settings(self) else choices

    @property
    def default_model_selection(self) -> str:
        from .routing import routing_settings
        routing = routing_settings(self)
        return "auto" if routing and routing["default_auto"] else self.select_model()["model"]

    def select_model_choice(self, value: str | None = None) -> str:
        selected = self.default_model_selection if value is None else self._model_id(value)
        if selected not in self.model_selection_choices:
            raise ValueError("该模型未由服务管理员配置为可选模型。")
        return selected

    @property
    def backend_choices(self) -> tuple[str, ...]:
        raw = self.data.get("backends", {})
        labels = {"joyagent": "JoyAgent", "codex": "Codex",
                  "pi": "pi", "deepseek": "DeepSeek Harness"}
        if not isinstance(raw, dict) or set(raw) - ((set(labels) - {"joyagent"}) | {"default", "install_dir", "resident_limit"}):
            raise ValueError("backends 配置包含不支持的 Agent 或字段")
        if type(raw.get("resident_limit", 8)) is not int or not 1 <= raw.get("resident_limit", 8) <= 32:
            raise ValueError("backends.resident_limit 必须为 1–32 的整数")
        choices = ["joyagent"]
        for name in labels:
            if name == "joyagent":
                continue
            entry = raw.get(name, {})
            if not isinstance(entry, dict) or set(entry) - {"enabled"} or type(entry.get("enabled", False)) is not bool:
                raise ValueError(f"backends.{name} 仅支持 enabled 布尔值")
            if entry.get("enabled", False):
                choices.append(name)
        default = raw.get("default", "joyagent")
        if not isinstance(default, str) or default not in choices:
            raise ValueError("默认 Agent 必须已启用")
        directory = raw.get("install_dir")
        if directory is not None and (not isinstance(directory, str) or not directory.strip()):
            raise ValueError("backends.install_dir 必须是安装目录")
        return tuple(choices)

    def select_backend(self, value: str | None = None) -> str:
        choices = self.backend_choices
        selected = self.data.get("backends", {}).get("default", "joyagent") if value is None else value
        if not isinstance(selected, str) or selected not in choices:
            raise ValueError("该 Agent 未由管理员启用")
        return selected

    @staticmethod
    def backend_label(value: str) -> str:
        # Keep retired backend names readable in existing conversation history.
        return {"joyagent": "JoyAgent", "codex": "Codex", "opencode": "OpenCode",
                "pi": "pi", "deepseek": "DeepSeek Harness"}.get(value, value)

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.get("agent", "data_dir", "data"))

    @property
    def workspace(self) -> Path:
        return self.resolve(self.get("agent", "workspace", "workspace"))

    @property
    def soul_path(self) -> Path:
        return self.resolve(self.get("agent", "soul", "SOUL.md"))

    @property
    def policy_path(self) -> Path:
        return self.resolve(self.get("agent", "policy", "policy.toml"))

    @property
    def skills_dir(self) -> Path:
        return self.resolve(self.get("agent", "skills_dir", "skills"))


def load_config(path: str | Path = "agent.toml") -> Config:
    resolved = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"配置不存在：{resolved}。先运行 python -m joyagent init。") from exc
    if not isinstance(data.get("model", {}), dict):
        raise ValueError("[model] 必须是 TOML 表")
    return Config(resolved, data)
