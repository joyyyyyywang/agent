"""Decision-tool wiring uses fake transport and owner-controlled temporary files."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from joyagent.channels.web import serve_web
from joyagent.channels.web_tenants import SharedRuntimeLoop
from joyagent.cli import doctor, initialize
from joyagent.config import Config
from joyagent.runtime import build_runtime
from joyagent.types import ProviderError


KEY_ENV = "JOYAGENT_TEST_DECISION_KEY"
FAKE_KEY = "fake-decision-key-never-persist"
BASE_URL = "https://decision.example.invalid/v1"
ARGUMENTS = {
    "state": "payment failed",
    "questions": {"department": {
        "type": "choice", "instructions": "Choose team",
        "criteria": {"billing": "Payment issues", "technical": "Software issues"},
    }},
}
RESPONSE = {
    "model": "decision-model-preview",
    "answers": {"department": {
        "type": "choice", "choice": "billing", "confidence": 0.9,
        "probabilities": {"billing": 0.9, "technical": 0.1},
    }},
}


class DecisionFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "SOUL.md").write_text("Use actual tool results.", encoding="utf-8")
        (self.root / "policy.toml").write_text('[tools]\nallow=["*"]\n', encoding="utf-8")
        (self.root / "skills").mkdir()
        self.config = Config(self.root / "agent.toml", {
            "model": {"provider": "demo", "model": "demo"},
            "agent": {"data_dir": "state", "workspace": "workspace"},
        })
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.chat_provider = Mock()
        self.chat_provider.complete = AsyncMock(side_effect=AssertionError("direct tools must not call the chat model"))

    def enable(self, *, environment=True):
        self.config.data["decision"] = {
            "enabled": True, "base_url": BASE_URL, "model": "decision-model-preview",
            "api_key_env": KEY_ENV, "timeout_seconds": 5,
        }
        if environment:
            os.environ[KEY_ENV] = FAKE_KEY

    def credentials(self, relative="private-decision.toml"):
        path = self.root / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(f'[openai]\nbase_url = "{BASE_URL}"\napi_key = "{FAKE_KEY}"\n', encoding="utf-8")
        path.chmod(0o600)
        self.config.data["decision"]["credentials_file"] = relative
        return path


class DecisionRuntimeTests(DecisionFixture, unittest.IsolatedAsyncioTestCase):
    async def runtime(self):
        with patch("joyagent.runtime.create_provider", return_value=self.chat_provider):
            runtime = await build_runtime(self.config)
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def test_omitted_or_explicitly_disabled_tool_does_not_require_credentials(self):
        for decision in (None, {"enabled": False}):
            with self.subTest(decision=decision), patch("joyagent.decisions._post_json") as post:
                if decision is not None:
                    self.config.data["decision"] = decision
                runtime = await self.runtime()
                self.assertNotIn("decision_evaluate", {spec.name for spec in runtime.agent.tools.specs()})
                result = await runtime.chat("/tools")
                self.assertNotIn("decision_evaluate", result.text)
                post.assert_not_called()
        self.chat_provider.complete.assert_not_called()

    async def test_enabled_tool_is_discoverable_and_direct_chat_uses_only_decision_transport(self):
        self.enable()
        with patch("joyagent.decisions._post_json", return_value=RESPONSE) as post:
            runtime = await self.runtime()
            self.assertIn("decision_evaluate", {spec.name for spec in runtime.agent.tools.specs()})
            post.assert_not_called()
            result = await runtime.chat("/tool decision_evaluate " + json.dumps(ARGUMENTS), "decision")
        self.assertFalse(result.stopped, result.text)
        self.assertEqual(result.steps, 0)
        self.assertEqual(len(result.tool_events), 1)
        self.assertTrue(result.tool_events[0]["ok"])
        self.assertIn("billing", result.text)
        post.assert_called_once()
        self.chat_provider.complete.assert_not_called()
        self.assertNotIn(FAKE_KEY, result.text)
        history = json.dumps([message.to_dict() for message in runtime.memory.history("decision")])
        self.assertNotIn(FAKE_KEY, history)

    async def test_owner_policy_hides_and_denies_enabled_tool_without_calling_service(self):
        self.enable()
        (self.root / "policy.toml").write_text('[tools]\nallow=["*"]\ndeny=["decision_evaluate"]\n')
        with patch("joyagent.decisions._post_json") as post:
            runtime = await self.runtime()
            self.assertNotIn("decision_evaluate", {spec.name for spec in runtime.agent.tools.specs()})
            result = await runtime.chat("/tool decision_evaluate " + json.dumps(ARGUMENTS))
        self.assertTrue(result.stopped)
        self.assertEqual(result.tool_events[0]["result"]["code"], "permission_denied")
        post.assert_not_called()
        self.chat_provider.complete.assert_not_called()

    async def test_workspace_rejects_decision_credential_and_its_hardlink(self):
        self.enable(environment=False)
        credential = self.credentials("workspace/private-decision.toml")
        with patch("joyagent.decisions._post_json") as post:
            runtime = await self.runtime()
            self.assertIn(credential, runtime.workspace.protected)
            os.link(credential, self.config.workspace / "ordinary-note.txt")
            for name in ("private-decision.toml", "ordinary-note.txt"):
                with self.subTest(path=name):
                    result = await runtime.chat('/tool workspace_read ' + json.dumps({"path": name}))
                    self.assertTrue(result.stopped)
                    self.assertFalse(result.tool_events[0]["ok"])
                    self.assertNotIn(FAKE_KEY, result.text)
                    self.assertNotIn(FAKE_KEY, json.dumps(result.tool_events))
            post.assert_not_called()
        self.chat_provider.complete.assert_not_called()

    async def test_runtime_rejects_missing_credentials_before_creating_provider_or_state(self):
        self.enable(environment=False)
        with patch("joyagent.runtime.create_provider") as provider, \
                patch("joyagent.decisions._post_json") as post, self.assertRaises(ProviderError):
            await build_runtime(self.config)
        provider.assert_not_called()
        post.assert_not_called()
        self.assertFalse(self.config.data_dir.exists())
        self.assertFalse(self.config.workspace.exists())


class DecisionStartupTests(DecisionFixture, unittest.TestCase):
    def test_shared_anonymous_tenant_discovers_and_executes_tool_with_owner_relative_credentials(self):
        self.enable(environment=False)
        credential = self.credentials()
        self.config.data["web"] = {"mode": "shared"}
        with patch("joyagent.runtime.create_provider", return_value=self.chat_provider), \
                patch("joyagent.decisions._post_json", return_value=RESPONSE) as post:
            manager = SharedRuntimeLoop(self.config)
            try:
                visitor = "a" * 32
                catalog = manager.call(manager.tool_catalog(visitor))
                self.assertIn("decision_evaluate", {tool["name"] for tool in catalog})
                self.assertIn(credential, manager._runtimes[visitor].workspace.protected)
                post.assert_not_called()
                result = manager.call(manager.chat(visitor, "default", "/tool decision_evaluate " + json.dumps(ARGUMENTS)))
                self.assertFalse(result.stopped, result.text)
                self.assertTrue(result.tool_events[0]["ok"])
                self.assertIn("billing", result.text)
                post.assert_called_once()
                other_history = manager.call(manager.history("b" * 32, "default"))
                self.assertEqual(other_history, [])
                self.chat_provider.complete.assert_not_called()
            finally:
                manager.close()

    def test_shared_manager_rejects_missing_credentials_before_allocating_thread_or_state(self):
        self.enable(environment=False)
        with patch("joyagent.channels.web_tenants.asyncio.new_event_loop") as loop, \
                patch("joyagent.decisions._post_json") as post, self.assertRaises(ProviderError):
            SharedRuntimeLoop(self.config)
        loop.assert_not_called()
        post.assert_not_called()
        self.assertFalse(self.config.data_dir.exists())

    def test_web_rejects_invalid_decision_config_or_credentials_before_sessions_and_server(self):
        for mode in ("local", "shared"):
            for invalid in ("configuration", "credentials"):
                with self.subTest(mode=mode, invalid=invalid):
                    self.enable(environment=False)
                    self.config.data["web"] = {"mode": mode}
                    if invalid == "configuration":
                        self.config.data["decision"]["enabled"] = "true"
                    with patch("joyagent.channels.web.RuntimeLoop") as runtime, \
                            patch("joyagent.channels.web_tenants.SharedRuntimeLoop") as tenants, \
                            patch("joyagent.channels.web_sessions.VisitorStore") as visitors, \
                            patch("joyagent.channels.web.AgentHTTPServer") as server, \
                            patch("joyagent.decisions._post_json") as post, \
                            self.assertRaises((ValueError, ProviderError)):
                        serve_web(self.config)
                    for operation in (runtime, tenants, visitors, server, post):
                        operation.assert_not_called()

    def test_doctor_validates_private_file_and_reports_missing_key_without_network_or_secret(self):
        self.enable(environment=False)
        credential = self.credentials()
        for present in (True, False):
            with self.subTest(present=present):
                if not present:
                    credential.unlink()
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch("joyagent.decisions._post_json") as decision_post, \
                        patch("joyagent.providers._post_json") as chat_post:
                    status = doctor(self.config)
                self.assertEqual(status, 0 if present else 1)
                self.assertIn("决策工具", output.getvalue())
                self.assertIn("未访问网络", output.getvalue())
                self.assertNotIn(FAKE_KEY, output.getvalue())
                decision_post.assert_not_called()
                chat_post.assert_not_called()

    def test_doctor_default_disabled_and_initialize_explain_current_provider_and_offline_mode(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("joyagent.decisions._post_json") as post:
            self.assertEqual(doctor(self.config), 0)
            self.assertEqual(initialize(str(self.root / "initialized")), 0)
        post.assert_not_called()
        text = output.getvalue()
        self.assertIn("OFF  决策工具未启用", text)
        self.assertIn("决策工具同样使用百炼 Token Plan", text)
        self.assertNotIn("QWENCLOUD_API_KEY", text)
        self.assertIn("[decision] enabled = false", text)
        self.assertIn("BAILIAN_TOKEN_PLAN_API_KEY", text)

