from __future__ import annotations

import asyncio
import http.client
import json
import os
import tempfile
import threading
import unittest
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from joyagent.channels.common import MAX_MESSAGE, read_state, validate_input
from joyagent.channels.email import EmailChannel
from joyagent.channels.telegram import TelegramChannel
from joyagent.channels.web import AgentHTTPServer, MAX_BODY_BYTES
from joyagent.channels.web_history import project_history
from joyagent.cli import main, parser
from joyagent.config import Config
from joyagent.types import AgentResult, Message, ToolCall, ToolSpec


class InputTests(unittest.TestCase):
    def test_input_boundaries(self):
        self.assertEqual(validate_input("hello", "work:1"), ("hello", "work:1"))
        for text, session in [(" ", "default"), ("x" * (MAX_MESSAGE + 1), "a"), ("x", "../a"), ("x", "a" * 129), (None, "a")]:
            with self.assertRaises(ValueError):
                validate_input(text, session)

    def test_cli_config_before_and_after_command(self):
        self.assertEqual(parser().parse_args(["--config", "x.toml", "chat"]).config, "x.toml")
        self.assertEqual(parser().parse_args(["chat", "--config", "y.toml"]).config, "y.toml")

    def test_cli_init_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(main(["init", directory]), 0)
            path = Path(directory) / "SOUL.md"
            path.write_text("my custom soul", encoding="utf-8")
            self.assertEqual(main(["init", directory]), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "my custom soul")


class TestBridge:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.runtime = SimpleNamespace(
            config=Config(Path("/tmp/agent.toml"), {"model": {"provider": "demo"}}),
            agent=SimpleNamespace(chat=AsyncMock(return_value=AgentResult("hello", 1, [{"name": "clock"}]))),
            chat=AsyncMock(return_value=AgentResult("hello", 1, [{"name": "clock"}])),
            check_conversation_not_deleted=Mock(),
            memory=SimpleNamespace(history=Mock(return_value=[Message("user", "hi"), Message("tool", "secret"), Message("assistant", "hello")]),
                                   get_conversation=Mock(return_value=None))
        )
        self.runtime.agent.tools = SimpleNamespace(specs=Mock(return_value=[ToolSpec("time_now", "查询时间", {"type": "object"})]))
        self.runtime.backend_catalog = Mock(return_value=[{"id": "joyagent", "label": "JoyAgent", "available": True, "reason": ""}])

    def call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=5)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


class WebTests(unittest.TestCase):
    def setUp(self):
        self.bridge = TestBridge()
        self.server = AgentHTTPServer(("127.0.0.1", 0), self.bridge)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)
        self.bridge.close()

    def request(self, method="POST", path="/api/chat", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        actual_headers = {"Origin": f"http://{self.host}", "X-CSRF-Token": self.server.csrf_token, "Content-Type": "application/json"}
        actual_headers.update(headers or {})
        if body is None and method == "POST":
            body = json.dumps({"message": "hello", "session_id": "project-a"})
        connection.request(method, path, body=body, headers=actual_headers)
        response = connection.getresponse()
        result = response.status, dict(response.headers), response.read()
        connection.close()
        return result

    def test_page_injects_token_and_csp(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.csrf_token.encode(), body)
        self.assertNotIn(b"__CSRF_TOKEN__", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("离线 Demo".encode(), body)
        self.assertIn("配置真实模型后可自由对话".encode(), body)

    def test_provider_label_escapes_model_name(self):
        # The model validator currently excludes HTML. Keep the renderer safe if
        # future providers need a broader model ID syntax.
        model = '<script>alert("x")</script>__CSRF_TOKEN__'
        self.bridge.runtime.config.data["model"] = {"provider": "openai", "model": "safe-model", "api_key": "never-show-this"}
        with patch.object(Config, "model_selection_choices", new_callable=PropertyMock, return_value=(model,)), \
                patch.object(Config, "default_model_selection", new_callable=PropertyMock, return_value=model):
            status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"&lt;script&gt;", body)
        self.assertIn(b'value="&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;__CSRF_TOKEN__" selected', body)
        self.assertNotIn(b"<script>alert", body)
        self.assertNotIn(b"never-show-this", body)

    def test_valid_chat_uses_shared_loop_and_namespace(self):
        status, _, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "hello")
        self.assertEqual(json.loads(body)["model"], "demo")
        self.bridge.runtime.chat.assert_awaited_once_with("hello", session_id="web:project-a", model="demo", backend="joyagent")

    def test_local_chat_passes_selected_model_to_runtime(self):
        self.bridge.runtime.config.data["model"] = {"provider": "openai", "model": "qwen3.8-flash",
                                                     "choices": ["qwen3.8-flash", "qwen3.8-max"]}
        status, _, body = self.request(body=json.dumps({"message": "hello", "model": "qwen3.8-max"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["model"], "qwen3.8-max")
        self.bridge.runtime.chat.assert_awaited_once_with("hello", session_id="web:default", model="qwen3.8-max", backend="joyagent")

    def test_local_chat_rejects_non_allowlisted_and_non_string_models(self):
        for model in (None, 42, [], {}, "unlisted"):
            with self.subTest(model=model):
                status, _, body = self.request(body=json.dumps({"message": "hello", "model": model}))
                self.assertEqual(status, 400)
                self.assertIn("所选模型不可用", json.loads(body)["error"])
        self.bridge.runtime.chat.assert_not_awaited()
        self.bridge.runtime.memory.history.assert_not_called()

    def test_local_backend_catalog_requires_csrf_and_only_returns_public_status(self):
        self.bridge.runtime.config.data["backends"] = {"codex": {"enabled": True}}
        entries = [{"id": "joyagent", "label": "JoyAgent", "available": True, "reason": ""},
                   {"id": "codex", "label": "Codex", "available": False, "reason": "尚未安装"}]
        self.bridge.runtime.backend_catalog.return_value = entries
        self.assertEqual(self.request("GET", "/api/backends", headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.bridge.runtime.backend_catalog.assert_not_called()
        status, _, body = self.request("GET", "/api/backends")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"default": "joyagent", "backends": entries})
        self.bridge.runtime.backend_catalog.assert_called_once_with()
        self.bridge.runtime.chat.assert_not_awaited()

    def test_local_backend_selection_is_allowlisted_and_forwarded_with_same_session(self):
        self.bridge.runtime.config.data["backends"] = {"codex": {"enabled": True}}
        status, _, body = self.request(body=json.dumps({"message": "continue", "session_id": "project-a", "backend": "codex"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["backend"], "codex")
        self.assertEqual(json.loads(body)["backend_label"], "Codex")
        self.bridge.runtime.chat.assert_awaited_once_with("continue", session_id="web:project-a", model="demo", backend="codex")
        self.bridge.runtime.memory.history.assert_not_called()

    def test_local_backend_invalid_values_fail_before_runtime(self):
        for backend in (None, True, 42, [], {}, "", "codex", "not-configured", "joyagent\n", "https://evil.example"):
            with self.subTest(backend=backend):
                status, _, body = self.request(body=json.dumps({"message": "hello", "backend": backend}))
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body), {"error": "所选 Agent 不可用，请刷新页面后选择"})
        self.bridge.runtime.chat.assert_not_awaited()

    def test_local_backend_options_are_escaped_without_exposing_install_directory(self):
        self.bridge.runtime.config.data["backends"] = {"codex": {"enabled": True}, "install_dir": "/private/agent-install"}
        with patch.object(Config, "backend_label", return_value='<script>bad</script>__CSRF_TOKEN__'):
            status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'&lt;script&gt;bad&lt;/script&gt;__CSRF_TOKEN__', body)
        self.assertNotIn(b'<script>bad', body)
        self.assertNotIn(b'/private/agent-install', body)
        self.assertNotIn(b'__BACKEND_OPTIONS__', body)

    def test_csrf_origin_and_host_rejected_before_agent(self):
        for headers in [{"X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example"}, {"Host": "evil.example"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.bridge.runtime.chat.assert_not_awaited()

    def test_payload_limits_and_content_type(self):
        self.assertEqual(self.request(headers={"Content-Length": str(MAX_BODY_BYTES + 1)})[0], 413)
        self.assertEqual(self.request(headers={"Content-Type": "text/plain"})[0], 415)
        for body in ["{bad", "[]", json.dumps({"message": " "}), json.dumps({"message": "x", "session_id": "../bad"}), json.dumps({"message": "x", "session_id": "a" * 125}), json.dumps({"message": "x" * (MAX_MESSAGE + 1)})]:
            with self.subTest(body=body[:80]):
                self.assertEqual(self.request(body=body)[0], 400)
        self.bridge.runtime.chat.assert_not_awaited()

    def test_history_requires_token_and_filters_tools(self):
        self.assertEqual(self.request("GET", "/api/history", headers={"X-CSRF-Token": ""})[0], 403)
        status, _, body = self.request("GET", "/api/history?session_id=project-a")
        self.assertEqual(status, 200)
        self.assertEqual([message["role"] for message in json.loads(body)["messages"]], ["user", "assistant"])
        self.bridge.runtime.memory.history.assert_called_with("web:project-a", limit=80)

    def test_history_does_not_publish_provider_continuation_data(self):
        self.bridge.runtime.memory.history.return_value = [
            Message("assistant", "public answer", provider_data={"reasoning_content": "private protocol state"})]
        status, _, body = self.request("GET", "/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["messages"][0]["content"], "public answer")
        self.assertNotIn(b"provider_data", body)
        self.assertNotIn(b"private protocol state", body)

    def test_local_tool_catalog_requires_csrf_and_contains_only_specs(self):
        self.assertEqual(self.request("GET", "/api/tools", headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.bridge.runtime.agent.tools.specs.assert_not_called()
        status, _, body = self.request("GET", "/api/tools")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"tools": [{"name": "time_now", "description": "查询时间", "parameters": {"type": "object"}}]})
        self.bridge.runtime.agent.tools.specs.assert_called_once_with()
        self.bridge.runtime.chat.assert_not_awaited()

    def test_history_restores_tool_success_and_redacts_stored_failure(self):
        self.bridge.runtime.memory.history.return_value = [
            Message("user", "run"),
            Message("assistant", "private-reasoning", tool_calls=[ToolCall("a", "time_now", {}), ToolCall("b", "workspace_read", {})],
                    provider_data={"reasoning_content": "private-provider-state"}),
            Message("tool", '{"ok":true,"result":{"now":"2026-10-24"}}', tool_call_id="a", name="time_now"),
            Message("tool", '{"ok":false,"code":"tool_error","error":"secret-owner-path","result":"secret-token"}', tool_call_id="b", name="workspace_read"),
            Message("assistant", "done"),
        ]
        status, _, body = self.request("GET", "/api/history")
        self.assertEqual(status, 200)
        result = json.loads(body)["messages"]
        self.assertEqual(len(result), 2)
        self.assertEqual(result[-1]["content"], "done")
        events = result[-1]["tool_events"]
        self.assertEqual([event["ok"] for event in events], [True, False])
        self.assertEqual(events[0]["result"]["result"], {"now": "2026-10-24"})
        for private in (b"private-reasoning", b"private-provider-state", b"provider_data", b"secret-owner-path", b"secret-token", b"duration_ms"):
            self.assertNotIn(private, body)

    def test_refuses_nonloopback_binding(self):
        with self.assertRaises(ValueError):
            AgentHTTPServer(("0.0.0.0", 0), self.bridge)


class WebHistoryTests(unittest.TestCase):
    def history(self, output):
        return project_history([
            Message("user", "run"),
            Message("assistant", "internal", tool_calls=[ToolCall("a", "command_run", {})]),
            Message("tool", json.dumps(output), tool_call_id="a", name="command_run"),
            Message("assistant", "done"),
        ])

    def test_execution_failure_keeps_typed_output_fields_only(self):
        output = {"ok": False, "code": "execution_failed", "error": "private-owner-error", "result": {
            "stdout": "visible stdout", "stderr": "visible stderr", "exit_code": 2,
            "timed_out": False, "truncated": True, "cwd": "notes", "private_path": "/owner/secret",
            "provider_data": {"reasoning_content": "private"},
        }}
        event = self.history(output)[-1]["tool_events"][0]
        self.assertFalse(event["ok"])
        self.assertEqual(event["result"]["result"], {"stdout": "visible stdout", "stderr": "visible stderr",
                         "exit_code": 2, "timed_out": False, "truncated": True, "cwd": "notes"})
        self.assertNotIn("private", str(event))
        self.assertNotIn("duration_ms", event)

    def test_execution_failure_drops_wrong_types_and_unknown_codes(self):
        output = {"ok": False, "code": "execution_failed", "result": {
            "stdout": {"leak": "secret"}, "stderr": ["secret"], "exit_code": True,
            "timed_out": "secret", "truncated": 1, "cwd": ["secret"],
        }}
        event = self.history(output)[-1]["tool_events"][0]
        self.assertEqual(event["result"]["result"], {})
        output["code"] = "private-code"
        event = self.history(output)[-1]["tool_events"][0]
        self.assertEqual(event["result"]["code"], "tool_error")
        self.assertNotIn("result", event["result"])
        output["code"] = "execution_failed"
        output["result"] = {"exit_code": None, "timed_out": True}
        self.assertEqual(self.history(output)[-1]["tool_events"][0]["result"]["result"], {"exit_code": None, "timed_out": True})

    def test_incomplete_turn_retains_results_without_assistant_or_call_internals(self):
        result = project_history([
            Message("tool", '{"ok":true,"result":"orphan-secret"}', tool_call_id="missing"),
            Message("user", "run"),
            Message("assistant", "private-content", tool_calls=[ToolCall("a", "time_now", {"private-argument": "secret"})]),
            Message("tool", "malformed-private-error", tool_call_id="a"),
            Message("user", "next"),
        ])
        self.assertEqual([row["content"] for row in result], ["run", "工具执行记录", "next"])
        self.assertFalse(result[1]["tool_events"][0]["ok"])
        self.assertNotIn("private", str(result))
        self.assertNotIn("secret", str(result))


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = Config(Path(self.directory.name) / "agent.toml", {"telegram": {"allowed_user_ids": [123]}})
        self.runtime = SimpleNamespace(config=self.config, agent=SimpleNamespace(chat=AsyncMock(return_value=AgentResult("reply", 1))))
        self.runtime.chat = self.runtime.agent.chat
        self.channel = TelegramChannel(self.runtime)
        self.channel._request = Mock(return_value={})

    def update(self, user=123, chat_type="private", text="hello", update_id=7):
        return {"update_id": update_id, "message": {"from": {"id": user}, "chat": {"id": user, "type": chat_type}, "text": text}}

    async def test_empty_allowlist_refused(self):
        self.config.data["telegram"]["allowed_user_ids"] = []
        with self.assertRaises(ValueError):
            TelegramChannel(self.runtime)

    async def test_allowed_private_reply_only(self):
        self.assertFalse(await self.channel.handle_update(self.update(user=999)))
        self.assertFalse(await self.channel.handle_update(self.update(chat_type="group")))
        self.assertFalse(await self.channel.handle_update(self.update(text="x" * (MAX_MESSAGE + 1))))
        self.runtime.agent.chat.assert_not_awaited()
        self.channel._request.assert_not_called()
        self.assertTrue(await self.channel.handle_update(self.update()))
        self.runtime.agent.chat.assert_awaited_once_with("hello", session_id="telegram:123:123")
        self.channel._request.assert_called_once_with("sendMessage", {"chat_id": 123, "text": "reply"})

    async def test_cursor_advances_only_after_reply(self):
        self.channel._request.side_effect = [[self.update()], RuntimeError("failed send")]
        with self.assertRaises(RuntimeError):
            await self.channel.poll_once()
        self.assertEqual(self.channel.offset, 0)
        self.assertEqual(read_state(self.channel.state_path)["pending"]["text"], "reply")
        restarted = TelegramChannel(self.runtime)
        restarted._request = Mock(side_effect=[[self.update()], {}])
        await restarted.poll_once()
        self.assertEqual(read_state(self.channel.state_path)["offset"], 8)
        self.assertNotIn("pending", read_state(self.channel.state_path))
        self.assertEqual(TelegramChannel(self.runtime).offset, 8)
        self.runtime.agent.chat.assert_awaited_once()

    async def test_ignored_update_advances_cursor_without_reply(self):
        self.channel._request.return_value = [self.update(user=999)]
        await self.channel.poll_once()
        self.assertEqual(self.channel.offset, 8)
        self.runtime.agent.chat.assert_not_awaited()
        self.assertEqual(self.channel._request.call_count, 1)


def mail_bytes(sender="allowed@example.com", message_id="<first@example.com>", extra=None):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "agent@example.com"
    message["Subject"] = "A task"
    message["Message-ID"] = message_id
    message["X-JoyAgent-Token"] = "test-email-token"
    for key, value in (extra or {}).items():
        message[key] = value
    message.set_content("Please help")
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, messages):
        self.messages = messages
        self.logout = Mock()

    def response(self, code):
        return "UIDVALIDITY", [b"100"]

    def uid(self, method, *arguments):
        if method == "search":
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(self.messages))]
        uid = int(arguments[0])
        raw = self.messages[uid]
        if arguments[1] == "(RFC822.SIZE)":
            return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
        return "OK", [(b"BODY[]", raw), b")"]


class EmailTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"AGENT_EMAIL_USER": "agent@example.com", "AGENT_EMAIL_PASSWORD": "test-password", "AGENT_EMAIL_TOKEN": "test-email-token"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = Config(Path(self.directory.name) / "agent.toml", {"email": {"enabled": True, "allowed_senders": ["allowed@example.com"], "imap_host": "imap.example.com", "smtp_host": "smtp.example.com"}})
        self.runtime = SimpleNamespace(config=self.config, agent=SimpleNamespace(chat=AsyncMock(return_value=AgentResult("reply", 1))))
        self.runtime.chat = self.runtime.agent.chat
        self.channel = EmailChannel(self.runtime)
        self.channel._send = Mock()

    async def test_opt_in_and_allowlist_required(self):
        self.config.data["email"]["enabled"] = False
        with self.assertRaises(ValueError):
            EmailChannel(self.runtime)
        self.config.data["email"]["enabled"] = True
        self.config.data["email"]["allowed_senders"] = []
        with self.assertRaises(ValueError):
            EmailChannel(self.runtime)

    async def test_unauthorized_and_auto_messages_ignored(self):
        for raw in [mail_bytes("bad@example.com"), mail_bytes("agent@example.com"), mail_bytes(extra={"Auto-Submitted": "auto-generated"}), mail_bytes(extra={"Precedence": "bulk"}), mail_bytes(extra={"List-Id": "list.example.com"}), mail_bytes(extra={"X-JoyAgent-AutoReply": "yes"})]:
            self.assertFalse(await self.channel.handle_message(raw))
        self.runtime.agent.chat.assert_not_awaited()
        self.channel._send.assert_not_called()

    async def test_sender_allowlist_cannot_bypass_auth_token(self):
        raw = mail_bytes().replace(b"X-JoyAgent-Token: test-email-token", b"X-JoyAgent-Token: wrong-token")
        self.assertFalse(await self.channel.handle_message(raw))
        raw = mail_bytes().replace(b"X-JoyAgent-Token: test-email-token\n", b"")
        self.assertFalse(await self.channel.handle_message(raw))
        self.runtime.agent.chat.assert_not_awaited()

    async def test_legacy_token_header_remains_authenticated(self):
        legacy = mail_bytes().replace(b"X-JoyAgent-Token:", b"X-JYAgent-Token:")
        wrong = legacy.replace(b"test-email-token", b"wrong-token")
        self.assertFalse(await self.channel.handle_message(wrong))
        self.runtime.agent.chat.assert_not_awaited()
        self.assertTrue(await self.channel.handle_message(legacy))
        self.runtime.agent.chat.assert_awaited_once()
        reply = self.channel._send.call_args.args[0]
        self.assertEqual(reply["X-JoyAgent-AutoReply"], "yes")
        self.assertIsNone(reply["X-JYAgent-AutoReply"])

    async def test_mixed_or_duplicate_token_headers_rejected(self):
        examples = [
            mail_bytes(extra={"X-JYAgent-Token": "test-email-token"}),
            mail_bytes(extra={"X-JoyAgent-Token": "test-email-token"}),
            mail_bytes(extra={"x-joyagent-token": "wrong-token"}),
            mail_bytes(extra={"X-JYAgent-Token": "test-email-token"}).replace(b"X-JoyAgent-Token:", b"X-JYAgent-Token:"),
        ]
        for raw in examples:
            with self.subTest(raw=raw):
                self.assertFalse(await self.channel.handle_message(raw))
        self.runtime.agent.chat.assert_not_awaited()
        self.channel._send.assert_not_called()

    async def test_new_and_legacy_autoreply_headers_block_loops(self):
        for header in ("X-JoyAgent-AutoReply", "X-JYAgent-AutoReply"):
            for value in ("yes", ""):
                with self.subTest(header=header, value=value):
                    self.assertFalse(await self.channel.handle_message(mail_bytes(extra={header: value})))
        self.runtime.agent.chat.assert_not_awaited()
        self.channel._send.assert_not_called()

    async def test_auth_token_disabled_only_when_explicit(self):
        with patch.dict(os.environ, {"AGENT_EMAIL_TOKEN": ""}):
            with self.assertRaises(ValueError):
                EmailChannel(self.runtime)
            self.config.data["email"]["auth_token_env"] = ""
            channel = EmailChannel(self.runtime)
            self.assertEqual(channel.auth_token, "")

    async def test_recipient_never_uses_reply_to_or_cc(self):
        self.assertTrue(await self.channel.handle_message(mail_bytes(extra={"Reply-To": "attacker@example.com", "Cc": "other@example.com"})))
        reply, recipient = self.channel._send.call_args.args
        self.assertEqual(recipient, "allowed@example.com")
        self.assertEqual(str(reply["To"]), "allowed@example.com")
        self.assertIsNone(reply["Cc"])
        self.assertEqual(reply["Auto-Submitted"], "auto-replied")
        self.assertEqual(reply["In-Reply-To"], "<first@example.com>")
        self.assertTrue(self.runtime.agent.chat.call_args.kwargs["session_id"].startswith("email:"))

    async def test_first_poll_skips_backlog_then_processes_new_mail(self):
        client = FakeIMAP({7: mail_bytes()})
        self.channel._connect = Mock(return_value=client)
        await self.channel.poll_once()
        self.assertEqual(self.channel.state["last_uid"], 7)
        self.channel._send.assert_not_called()
        client.messages[8] = mail_bytes(message_id="<second@example.com>")
        await self.channel.poll_once()
        self.channel._send.assert_called_once()
        self.assertEqual(read_state(self.channel.state_path)["last_uid"], 8)

    async def test_failed_send_keeps_durable_cursor(self):
        client = FakeIMAP({7: mail_bytes()})
        self.channel._connect = Mock(return_value=client)
        await self.channel.poll_once()
        client.messages[8] = mail_bytes(message_id="<second@example.com>")
        self.channel._send.side_effect = RuntimeError("send failed")
        with self.assertRaises(RuntimeError):
            await self.channel.poll_once()
        self.assertEqual(read_state(self.channel.state_path)["last_uid"], 7)
        prepared_id = self.channel._send.call_args.args[0]["Message-ID"]
        restarted = EmailChannel(self.runtime)
        restarted._connect = Mock(return_value=client)
        restarted._send = Mock()
        await restarted.poll_once()
        self.assertEqual(restarted._send.call_args.args[0]["Message-ID"], prepared_id)
        self.runtime.agent.chat.assert_awaited_once()
        self.assertEqual(read_state(self.channel.state_path)["last_uid"], 8)
        self.assertNotIn("pending", read_state(self.channel.state_path))

    async def test_duplicate_message_ids_replied_once(self):
        self.channel.initial_backlog = True
        self.channel._connect = Mock(return_value=FakeIMAP({7: mail_bytes(), 8: mail_bytes()}))
        await self.channel.poll_once()
        self.channel._send.assert_called_once()
        self.assertEqual(read_state(self.channel.state_path)["last_uid"], 8)


if __name__ == "__main__":
    unittest.main()
