"""Optional pinned CLIs: sliding context uses a fake local model, never paid APIs."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from joyagent.config import Config
from joyagent.runtime import build_runtime


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("JOYAGENT_TEST_CLI_PREFIX"),
                     "requires Linux isolation and explicitly installed pinned CLIs")
class ContextCLIIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_all_native_agents_trim_old_turns_without_restarting_shared_process(self):
        for backend in ("codex", "pi", "deepseek"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "SOUL.md").write_text("Reply WINDOW_DONE without using tools.")
                (root / "policy.toml").write_text(
                    '[tools]\nallow=[]\n[limits]\ncontext_turns=2\nturn_timeout_seconds=60\n')
                config = Config(root / "agent.toml", {
                    "agent": {"workspace": "workspace", "data_dir": "data"},
                    "model": {"provider": "openai", "model": "fake-model",
                              "base_url": "http://127.0.0.1:1/v1", "api_key_env": "JOYAGENT_TEST_FAKE_KEY"},
                    "decision": {"enabled": False}, "routing": {"enabled": False},
                    "execution": {"enabled": True},
                    "backends": {"install_dir": os.environ["JOYAGENT_TEST_CLI_PREFIX"], backend: {"enabled": True}},
                })
                requests = []

                def fake(url, body, headers, timeout):
                    requests.append(json.dumps(body["messages"], ensure_ascii=False))
                    return {"id": "fake-context-response", "object": "chat.completion", "created": 1,
                            "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "WINDOW_DONE"}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

                with patch.dict(os.environ, {"JOYAGENT_TEST_FAKE_KEY": "fake-only"}), patch("joyagent.model_broker._post_json", fake):
                    runtime = await build_runtime(config)
                    try:
                        native_ids = []
                        worker = process = native_pid = None
                        other_id = None
                        for turn in range(1, 6):
                            before = len(requests)
                            result = await runtime.chat(f"WINDOW_USER_{turn}: Reply WINDOW_DONE.", "web:window", backend=backend)
                            self.assertFalse(result.stopped, result.text)
                            self.assertEqual(result.text, "WINDOW_DONE")
                            self.assertEqual(len(requests), before + 1)
                            sent = requests[-1]
                            for retained in range(max(1, turn - 2), turn + 1):
                                self.assertIn(f"WINDOW_USER_{retained}", sent)
                            for removed in range(1, max(1, turn - 2)):
                                self.assertNotIn(f"WINDOW_USER_{removed}", sent)
                            self.assertNotIn("OTHER_SESSION_SENTINEL", sent)
                            manager = runtime._resident_manager
                            current = manager._registry().workers[(manager, backend)]
                            if worker is None:
                                worker, process, native_pid = current, current.process, current.native_pid
                            self.assertIs(current, worker)
                            self.assertIs(current.process, process)
                            self.assertEqual(current.native_pid, native_pid)
                            self.assertIsNone(process.returncode)
                            native_ids.append(current.sessions["web:window"].native_session_id)
                            if turn == 2:
                                other = await runtime.chat("OTHER_SESSION_SENTINEL: Reply WINDOW_DONE.", "web:other", backend=backend)
                                self.assertFalse(other.stopped, other.text)
                                other_id = current.sessions["web:other"].native_session_id
                            if other_id is not None:
                                self.assertEqual(current.sessions["web:other"].native_session_id, other_id)
                        self.assertEqual(native_ids[0], native_ids[2])
                        self.assertNotEqual(native_ids[2], native_ids[3])
                        self.assertNotEqual(native_ids[3], native_ids[4])
                        self.assertEqual(len(runtime.memory.history("web:window", limit=80)), 10)
                        self.assertEqual(len(runtime.memory.history("web:other", limit=80)), 2)
                        self.assertFalse(worker.broker.active)
                    finally:
                        await runtime.close()
