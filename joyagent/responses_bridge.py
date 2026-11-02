"""Stateless, text-only Responses <-> Chat Completions compatibility bridge.

This is a protocol adapter for the external Codex CLI, not a second agent loop.
The caller owns authentication, model selection, request limits and transport.
Only complete Chat responses are accepted; SSE below describes a buffered result,
so it does not claim to forward upstream token streaming. No state is retained.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any


class ResponsesBridgeError(ValueError):
    """An input cannot be represented by the supported protocol subset."""


def _text(value: Any, field: str, *, empty: bool = True) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ResponsesBridgeError(f"{field} must be a string")
    return value


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ResponsesBridgeError(f"{field} must be an object")
    return value


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResponsesBridgeError("value is not valid JSON") from exc


def _arguments(value: Any) -> str:
    value = _text(value, "function arguments")

    def invalid_constant(_: str) -> None:
        raise ValueError("invalid JSON constant")

    try:
        decoded = json.loads(value, parse_constant=invalid_constant)
        _json(decoded)
    except (ValueError, RecursionError) as exc:
        raise ResponsesBridgeError("function arguments must contain a JSON object") from exc
    if not isinstance(decoded, dict):
        raise ResponsesBridgeError("function arguments must contain a JSON object")
    return value


def _content(value: Any, field: str = "content") -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ResponsesBridgeError(f"{field} must be text or text blocks")
    parts = []
    for block in value:
        block = _object(block, field)
        if block.get("type") not in {"input_text", "output_text", "text"}:
            raise ResponsesBridgeError("only text content is supported by this bridge")
        parts.append(_text(block.get("text"), "text block"))
    return "\n".join(parts)


def _tools(request: dict) -> tuple[list[dict], dict[str, dict]]:
    raw = request.get("tools", [])
    if not isinstance(raw, list):
        raise ResponsesBridgeError("tools must be an array")
    converted: list[dict] = []
    mapping: dict[str, dict] = {}

    def add(tool: dict, namespace: str | None = None) -> None:
        tool = _object(tool, "tool")
        kind = tool.get("type")
        name = _text(tool.get("name"), "tool name", empty=False)
        if kind == "namespace":
            if namespace is not None:
                raise ResponsesBridgeError("nested tool namespaces are unsupported")
            children = tool.get("tools")
            if not isinstance(children, list):
                raise ResponsesBridgeError("namespace tools must be an array")
            for child in children:
                add(child, name)
            return
        if kind not in {"function", "custom"}:
            raise ResponsesBridgeError("unsupported Responses tool type")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or (
            namespace is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", namespace)
        ):
            raise ResponsesBridgeError("tool names must use letters, digits, underscores or hyphens")
        wire_name = f"{namespace}__{name}" if namespace else name
        if len(wire_name) > 64:
            wire_name = wire_name[:43] + "_" + hashlib.sha256(wire_name.encode()).hexdigest()[:20]
        if wire_name in mapping:
            raise ResponsesBridgeError("duplicate or colliding tool names")
        description = _text(tool.get("description", ""), "tool description")
        if kind == "custom":
            description += "\nPut the complete free-form tool input in the input string."
            tool_format = tool.get("format")
            if tool_format:
                description += "\nInput format: " + _json(tool_format)
            parameters = {"type": "object", "properties": {"input": {"type": "string"}},
                          "required": ["input"], "additionalProperties": False}
        else:
            parameters = deepcopy(_object(tool.get("parameters", {
                "type": "object", "properties": {},
            }), "tool parameters"))
        function = {"name": wire_name, "description": description, "parameters": parameters}
        converted.append({"type": "function", "function": function})
        mapping[wire_name] = {"name": name, "namespace": namespace, "type": kind}

    for tool in raw:
        add(tool)
    return converted, mapping


def _wire_name(item: dict, mapping: dict[str, dict]) -> str:
    name = _text(item.get("name"), "tool call name", empty=False)
    namespace = item.get("namespace")
    for wire_name, metadata in mapping.items():
        if metadata["name"] == name and metadata["namespace"] == namespace:
            return wire_name
    raise ResponsesBridgeError("tool call does not match a declared tool")


def to_chat_request(request: dict, *, model: str | None = None) -> dict:
    """Convert a complete, stateless Codex request to non-streaming Chat JSON.

    Namespaces are flattened deterministically. Custom free-form tools use one
    string parameter, then are restored when converting the model's tool call.
    The host must override model and token limits before sending to its provider.
    """
    request = _object(request, "request")
    if request.get("previous_response_id") or request.get("conversation"):
        raise ResponsesBridgeError("stateful Responses continuation is unsupported; replay input")
    if request.get("background"):
        raise ResponsesBridgeError("background Responses are unsupported")
    tools, mapping = _tools(request)
    messages: list[dict] = []
    instructions = request.get("instructions")
    if instructions is not None:
        messages.append({"role": "system", "content": _text(instructions, "instructions")})
    inputs = request.get("input", [])
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": inputs}]
    if not isinstance(inputs, list):
        raise ResponsesBridgeError("input must be text or an array")
    pending: set[str] = set()
    seen: set[str] = set()
    for item in inputs:
        item = _object(item, "input item")
        kind = item.get("type", "message")
        if kind == "message":
            if pending:
                raise ResponsesBridgeError("tool calls need matching outputs before the next message")
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise ResponsesBridgeError("unsupported message role")
            messages.append({"role": "system" if role == "developer" else role,
                             "content": _content(item.get("content"))})
        elif kind in {"function_call", "custom_tool_call"}:
            call_id = _text(item.get("call_id"), "call_id", empty=False)
            if call_id in seen:
                raise ResponsesBridgeError("duplicate tool call id")
            seen.add(call_id)
            pending.add(call_id)
            name = _wire_name(item, mapping)
            expected = "custom" if kind == "custom_tool_call" else "function"
            if mapping[name]["type"] != expected:
                raise ResponsesBridgeError("tool call type does not match its declaration")
            arguments = (_json({"input": _text(item.get("input"), "custom tool input")})
                         if kind == "custom_tool_call" else _arguments(item.get("arguments")))
            call = {"id": call_id, "type": "function", "function": {
                "name": name, "arguments": arguments,
            }}
            if not messages or messages[-1]["role"] != "assistant":
                messages.append({"role": "assistant", "content": None, "tool_calls": []})
            messages[-1].setdefault("tool_calls", []).append(call)
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = _text(item.get("call_id"), "call_id", empty=False)
            if call_id not in pending:
                raise ResponsesBridgeError("tool output has no matching pending call")
            pending.remove(call_id)
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": _content(item.get("output"), "tool output")})
        else:
            raise ResponsesBridgeError("unsupported Responses input item")
    if pending:
        raise ResponsesBridgeError("tool calls are missing outputs")
    if not messages:
        raise ResponsesBridgeError("request has no messages")
    payload = {"model": _text(model if model is not None else request.get("model"),
                             "model", empty=False), "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
        choice = request.get("tool_choice", "auto")
        if isinstance(choice, dict):
            if choice.get("type") not in {"function", "custom"}:
                raise ResponsesBridgeError("unsupported tool_choice")
            choice = {"type": "function", "function": {"name": _wire_name(choice, mapping)}}
        elif choice not in {"auto", "none", "required"}:
            raise ResponsesBridgeError("unsupported tool_choice")
        payload["tool_choice"] = choice
        if "parallel_tool_calls" in request:
            if not isinstance(request["parallel_tool_calls"], bool):
                raise ResponsesBridgeError("parallel_tool_calls must be boolean")
            payload["parallel_tool_calls"] = request["parallel_tool_calls"]
    for source, target in (("temperature", "temperature"), ("top_p", "top_p"),
                           ("max_output_tokens", "max_tokens")):
        if source in request:
            payload[target] = request[source]
    _json(payload)
    return payload


def from_chat_response(completion: dict, *, request: dict | None = None,
                       response_id: str | None = None) -> dict:
    """Restore a complete Chat response to the supported Responses object."""
    completion = _object(completion, "completion")
    request = {} if request is None else _object(request, "request")
    _, mapping = _tools(request)
    choices = completion.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ResponsesBridgeError("expected exactly one Chat completion choice")
    choice = _object(choices[0], "choice")
    message = _object(choice.get("message"), "assistant message")
    finish = choice.get("finish_reason")
    if finish not in {"stop", "tool_calls", "length", "content_filter"}:
        raise ResponsesBridgeError("unsupported or missing completion finish_reason")
    response_id = response_id or "resp_" + hashlib.sha256(_json(completion).encode()).hexdigest()[:24]
    _text(response_id, "response_id", empty=False)
    output = []
    content = message.get("content")
    if content is not None:
        content = _content(content)
        if content:
            output.append({"id": f"{response_id}_msg", "type": "message", "role": "assistant",
                           "status": "completed", "content": [
                               {"type": "output_text", "text": content, "annotations": []},
                           ]})
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list):
        raise ResponsesBridgeError("tool_calls must be an array")
    seen = set()
    for index, call in enumerate(calls):
        call = _object(call, "tool call")
        function = _object(call.get("function"), "function call")
        metadata = mapping.get(function.get("name"))
        if call.get("type") != "function" or metadata is None:
            raise ResponsesBridgeError("model called an undeclared tool")
        call_id = _text(call.get("id"), "call id", empty=False)
        if call_id in seen:
            raise ResponsesBridgeError("duplicate model tool call id")
        seen.add(call_id)
        item = {"id": f"{response_id}_call_{index}", "type": "function_call",
                "status": "completed", "call_id": call_id, "name": metadata["name"]}
        if metadata["namespace"] is not None:
            item["namespace"] = metadata["namespace"]
        arguments = _arguments(function.get("arguments"))
        if metadata["type"] == "custom":
            values = json.loads(arguments)
            if set(values) != {"input"}:
                raise ResponsesBridgeError("custom tool arguments must contain only input")
            item.update(type="custom_tool_call", input=_text(values["input"], "custom input"))
        else:
            item["arguments"] = arguments
        output.append(item)
    if finish == "tool_calls" and not calls:
        raise ResponsesBridgeError("tool_calls finish_reason without a tool call")
    usage = _object(completion.get("usage") or {}, "usage")

    def tokens(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    input_tokens = tokens(usage.get("prompt_tokens"))
    output_tokens = tokens(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details") or {}
    output_details = usage.get("completion_tokens_details") or {}
    status = "incomplete" if finish in {"length", "content_filter"} else "completed"
    return {"id": response_id, "object": "response", "created_at": tokens(completion.get("created")),
            "status": status, "error": None,
            "incomplete_details": ({"reason": "max_output_tokens" if finish == "length" else "content_filter"}
                                   if status == "incomplete" else None),
            "model": completion.get("model", request.get("model", "")), "output": output,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                      "total_tokens": input_tokens + output_tokens,
                      "input_tokens_details": {"cached_tokens": tokens(details.get("cached_tokens"))
                                               if isinstance(details, dict) else 0},
                      "output_tokens_details": {"reasoning_tokens": tokens(output_details.get("reasoning_tokens"))
                                                if isinstance(output_details, dict) else 0}}}


def sse_events(response: dict) -> list[dict]:
    """Build an ordered SSE event sequence from an already buffered response."""
    response = deepcopy(_object(response, "response"))
    if response.get("status") not in {"completed", "incomplete"}:
        raise ResponsesBridgeError("SSE requires a terminal response")
    events: list[dict] = []

    def emit(kind: str, **fields: Any) -> None:
        events.append({"type": kind, "sequence_number": len(events), **deepcopy(fields)})

    initial = {**response, "status": "in_progress", "output": [], "usage": None,
               "incomplete_details": None}
    emit("response.created", response=initial)
    emit("response.in_progress", response=initial)
    for index, item in enumerate(response["output"]):
        item = _object(item, "output item")
        kind = item.get("type")
        if kind == "message":
            emit("response.output_item.added", output_index=index,
                 item={**item, "status": "in_progress", "content": []})
            for content_index, part in enumerate(item["content"]):
                fields = {"item_id": item["id"], "output_index": index, "content_index": content_index}
                emit("response.content_part.added", **fields, part={**part, "text": ""})
                emit("response.output_text.delta", **fields, delta=part["text"])
                emit("response.output_text.done", **fields, text=part["text"])
                emit("response.content_part.done", **fields, part=part)
        elif kind in {"function_call", "custom_tool_call"}:
            field = "arguments" if kind == "function_call" else "input"
            event_name = "function_call_arguments" if kind == "function_call" else "custom_tool_call_input"
            emit("response.output_item.added", output_index=index,
                 item={**item, "status": "in_progress", field: ""})
            fields = {"item_id": item["id"], "output_index": index}
            emit(f"response.{event_name}.delta", **fields, delta=item[field])
            emit(f"response.{event_name}.done", **fields, **{field: item[field]})
        else:
            raise ResponsesBridgeError("unsupported output item")
        emit("response.output_item.done", output_index=index, item=item)
    emit("response." + response["status"], response=response)
    return events


def encode_sse(response: dict) -> bytes:
    """Encode buffered events with JSON escaping and unambiguous LF framing."""
    return "".join(f"event: {event['type']}\ndata: {_json(event)}\n\n"
                   for event in sse_events(response)).encode("utf-8")
