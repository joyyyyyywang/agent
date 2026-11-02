"""An optional, owner-configured TypeSafe System One decision tool."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
from typing import Any

from .config import Config
from .providers import HTTPProvider, _post_json
from .tools import ToolRegistry
from .types import ProviderError, ToolSpec


DEFAULT_BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"
DEFAULT_MODEL = "decision-model-preview"
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_QUESTIONS = 16
_OWNER_FIELDS = {"enabled", "base_url", "model", "api_key_env", "credentials_file", "timeout_seconds"}


def _settings(raw: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - _OWNER_FIELDS:
        raise ValueError("decision 包含不支持的配置字段。")
    if type(raw.get("enabled", False)) is not bool:
        raise ValueError("decision.enabled 必须是布尔值。")
    result = {"base_url": raw.get("base_url", DEFAULT_BASE_URL),
              "model": raw.get("model", DEFAULT_MODEL),
              "api_key_env": raw.get("api_key_env", "QWENCLOUD_API_KEY"),
              "credentials_file": raw.get("credentials_file"),
              "timeout_seconds": raw.get("timeout_seconds", 15)}
    if not isinstance(result["base_url"], str) or not result["base_url"].strip():
        raise ValueError("decision.base_url 必须是非空地址。")
    if result["model"] != DEFAULT_MODEL:
        raise ValueError("decision.model 仅支持 decision-model-preview。")
    if (not isinstance(result["api_key_env"], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", result["api_key_env"])):
        raise ValueError("decision.api_key_env 必须是有效的环境变量名称。")
    value = result["credentials_file"]
    if value is not None:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("decision.credentials_file 必须是非空文件路径。")
        path = Path(os.path.expandvars(value)).expanduser()
        # Preserve the final symlink so the shared credential reader rejects it.
        result["credentials_file"] = str(root / path if root is not None and not path.is_absolute() else path)
    # Reuse the ordinary provider's URL and timeout rules, without reading keys.
    provider = HTTPProvider({"provider": "openai", **result})
    result["base_url"] = provider.base_url
    result["timeout_seconds"] = provider.timeout
    return result


def decision_settings(config: Config) -> dict[str, Any] | None:
    """Validate owner configuration only; never read credentials or use the network."""
    raw = config.data.get("decision", {})
    result = _settings(raw, config.root)
    return result if raw.get("enabled", False) else None


def _json_copy(value: Any) -> Any:
    remaining = 16384

    def check(item: Any, depth: int = 0) -> None:
        nonlocal remaining
        remaining -= 1
        if depth > 32 or remaining < 0:
            raise ValueError("决策请求嵌套或元素数量过多。")
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                check(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise ValueError("决策请求必须是有限数值组成的 JSON 数据。")

    try:
        check(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("决策请求不能超过 64 KiB。")
        return json.loads(encoded)
    except (RecursionError, UnicodeError, OverflowError):
        raise ValueError("决策请求不是有效的有界 JSON 数据。") from None


def _label(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 128
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _questions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not 1 <= len(value) <= MAX_QUESTIONS:
        raise ValueError("questions 必须是包含 1–16 个问题的对象。")
    for name, question in value.items():
        if not _label(name) or not isinstance(question, dict):
            raise ValueError("questions 的问题 ID 或问题对象无效。")
        if set(question) - {"type", "instructions", "criteria"}:
            raise ValueError("决策问题包含不支持的字段。")
        kind = question.get("type")
        if kind not in ("choice", "score", "noul"):
            raise ValueError("决策问题类型仅支持 choice、score、noul。")
        if "instructions" in question and (not isinstance(question["instructions"], str)
                                            or not question["instructions"].strip()):
            raise ValueError("instructions 必须是非空文本。")
        criteria = question.get("criteria")
        if kind == "choice":
            if (not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255
                    or not all(_label(key) and isinstance(text, str) and text.strip()
                               for key, text in criteria.items())):
                raise ValueError("choice.criteria 必须包含 1–255 个选项及其非空描述。")
        elif kind == "score":
            if (not isinstance(criteria, list) or not 2 <= len(criteria) <= 255
                    or not all(isinstance(text, str) and text.strip() for text in criteria)):
                raise ValueError("score.criteria 必须是包含 2–255 个等级描述的数组。")
        elif "criteria" in question:
            if (not isinstance(criteria, dict) or not criteria or set(criteria) - {"true", "false"}
                    or not all(isinstance(text, str) and text.strip() for text in criteria.values())):
                raise ValueError("noul.criteria 仅支持 true、false 的非空描述。")
    return value


def _number(value: Any, minimum: float, maximum: float | None = None) -> bool:
    try:
        return (type(value) in (int, float) and math.isfinite(value) and value >= minimum
                and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def _response(data: Any, questions: dict[str, Any]) -> dict[str, Any]:
    def invalid() -> None:
        raise ProviderError("决策模型返回了无效的结构化结果。", category="response")

    if not isinstance(data, dict) or data.get("error") or data.get("model", DEFAULT_MODEL) != DEFAULT_MODEL:
        invalid()
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        invalid()
    clean: dict[str, Any] = {"model": DEFAULT_MODEL, "answers": {}}
    for name, question in questions.items():
        answer = answers[name]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            invalid()
        item: dict[str, Any] = {"type": kind}
        if kind == "noul":
            if not _number(answer.get("noul"), 0, 1):
                invalid()
            item["noul"] = answer["noul"]
        else:
            options = (set(question["criteria"]) if kind == "choice"
                       else {str(index) for index in range(len(question["criteria"]))})
            probabilities = answer.get("probabilities")
            if (not isinstance(probabilities, dict) or set(probabilities) != options
                    or not all(_number(value, 0, 1) for value in probabilities.values())
                    or not math.isclose(sum(probabilities.values()), 1, rel_tol=0, abs_tol=0.0011)
                    or not _number(answer.get("confidence"), 0, 1)):
                invalid()
            item.update(probabilities=dict(probabilities), confidence=answer["confidence"])
            if kind == "choice":
                choice = answer.get("choice")
                if not isinstance(choice, str) or choice not in options:
                    invalid()
                item["choice"] = choice
            else:
                if not _number(answer.get("score"), 0, len(options) - 1):
                    invalid()
                item["score"] = answer["score"]
                if "legend" in answer:
                    legend = {str(index): text for index, text in enumerate(question["criteria"])}
                    if answer["legend"] != legend:
                        invalid()
                    item["legend"] = legend
        clean["answers"][name] = item
    if "usage" in data:
        usage = data["usage"]
        if not isinstance(usage, dict):
            invalid()
        clean["usage"] = {}
        if "input_tokens" in usage:
            count = usage["input_tokens"]
            if type(count) is not int or not 0 <= count <= 1_000_000_000:
                invalid()
            clean["usage"]["input_tokens"] = count
    if "latency_ms" in data:
        if not _number(data["latency_ms"], 0):
            invalid()
        clean["latency_ms"] = data["latency_ms"]
    if "request_id" in data:
        identifier = data["request_id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identifier):
            invalid()
        clean["request_id"] = identifier
    return clean


class DecisionClient:
    def __init__(self, settings: dict[str, Any]):
        self.settings = _settings(settings)
        self._provider = HTTPProvider({"provider": "openai", **self.settings})

    def _credential(self) -> tuple[str, str]:
        try:
            key, source = self._provider.credential()
        except (ValueError, OSError, RuntimeError):
            raise ProviderError("决策模型凭据不可用，请检查私有凭据配置。", category="credentials") from None
        if not key:
            raise ProviderError("决策模型缺少与当前网关匹配的凭据。", category="credentials")
        return key, source

    def credential_source(self) -> str:
        """Read and validate credentials without exposing the key or using a network."""
        return self._credential()[1]

    async def evaluate(self, state: Any, questions: Any) -> dict[str, Any]:
        if not isinstance(state, (str, dict, list)):
            raise ValueError("state 必须是文本、对象或数组。")
        payload = _json_copy({"model": self.settings["model"], "state": state, "questions": questions})
        validated = _questions(payload["questions"])
        key, _ = self._credential()
        try:
            data = await asyncio.wait_for(asyncio.to_thread(
                _post_json, self.settings["base_url"] + "/systemone", payload,
                {"Authorization": "Bearer " + key}, self.settings["timeout_seconds"]),
                timeout=self.settings["timeout_seconds"])
        except TimeoutError:
            raise ProviderError("决策模型请求超时；本轮不会自动重试。", category="timeout") from None
        except ProviderError as exc:
            # ToolRegistry surfaces exception text, so always use its safe mapping.
            raise ProviderError(exc.shared_message, category=exc.category) from None
        except Exception:
            raise ProviderError("决策模型请求失败，请检查服务端配置。", category="network") from None
        return _response(data, validated)


def register_decision(registry: ToolRegistry, settings: dict[str, Any] | None) -> None:
    if settings is None:
        return
    client = DecisionClient(settings)
    registry.register(ToolSpec(
        "decision_evaluate",
        "调用独立决策模型分类、评分或判断是非，返回概率和置信度。只用于结构化判断；不会生成文本或执行动作。"
        "questions 为问题 ID 到问题对象的映射，type 为 choice/score/noul，instructions 为问题描述。"
        "choice.criteria 为选项名到描述的对象；score.criteria 为从低到高的描述数组；noul.criteria 可省略。",
        {"type": "object", "properties": {
            "state": {"type": ["string", "object", "array"],
                      "description": '填写 JSON，例如 {"content":"支付失败"} 或带引号的文本 "支付失败"'},
            "questions": {"type": "object", "description":
                          '填写 1–16 个问题的 JSON 对象，例如 '
                          '{"department":{"type":"choice","instructions":"由哪个团队处理？",'
                          '"criteria":{"billing":"支付、退款和账单","technical":"软件故障"}}}'},
        }, "required": ["state", "questions"], "additionalProperties": False}), client.evaluate)
