"""Run harmless Python CLI substitutes; no installed agent or model is used."""

import asyncio
from contextlib import ExitStack
from functools import partial
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from joyagent.cli_backend import ExternalProvider
from joyagent.config import Config
from joyagent.execution import SandboxExecutor, execution_settings
from joyagent.model_broker import ModelBroker
from joyagent.policy import Limits, Policy
from joyagent.providers import HTTPProvider
from joyagent.tools import Workspace
from joyagent.types import Message


FINAL = [
    {"type": "item.completed", "item": {"type": "agent_message", "text": "FAKE_OK"}},
    {"type": "turn.completed"},
]
TOOL = {"type": "item.completed", "item": {
    "id": "command-1", "type": "command_execution", "status": "completed",
    "command": "printf fake", "aggregated_output": "fake", "exit_code": 0}}


def emit(events):
    return "\n".join("print(" + repr(json.dumps(event)) + ", flush=True)" for event in events)


class ExternalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jya-life-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = Config(self.root / "agent.toml", {"backends": {"codex": {"enabled": True}}})
        provider = HTTPProvider({"provider": "openai", "model": "fake-model",
                                 "base_url": "http://127.0.0.1:1/v1"})
        provider.credential = Mock(side_effect=AssertionError("No model call is allowed"))
        self.agent = ExternalProvider("codex", config, provider, None,
                                      Policy(("*",), limits=Limits(turn_timeout_seconds=30)))
        self.agent.root = self.root / "not-an-installation"
        self.agent.executor = SimpleNamespace(check=AsyncMock(), settings={"network": "none"})
        self.processes = []
        self.brokers = []
        self.homes = []
        self.started = asyncio.Event()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.agent, "_prepare"))
        self.stack.enter_context(patch("joyagent.cli_backend.watch_process", new_callable=AsyncMock))
        # macOS AF_UNIX paths must remain short, including the random suffix.
        temporary_factory = partial(tempfile.TemporaryDirectory, dir="/tmp")
        self.stack.enter_context(patch("joyagent.cli_backend.tempfile.TemporaryDirectory", temporary_factory))

        def broker_factory(*args, **kwargs):
            broker = ModelBroker(*args, **kwargs)
            self.brokers.append(broker)
            self.addCleanup(broker.close)
            return broker

        self.stack.enter_context(patch("joyagent.cli_backend.ModelBroker", side_effect=broker_factory))
        create_process = asyncio.create_subprocess_exec

        async def capture_process(*args, **kwargs):
            process = await create_process(*args, **kwargs)
            self.processes.append(process)
            self.started.set()
            return process

        self.spawn = self.stack.enter_context(patch("joyagent.cli_backend.asyncio.create_subprocess_exec",
                                                     side_effect=capture_process))

    def launch_script(self, script):
        def launch(home, plan, socket):
            self.homes.append(home)
            self.assertTrue(socket.exists())
            parsed = json.loads(plan.read_text())
            self.assertEqual(parsed["socket"], "/run/joyagent/model.sock")
            self.assertNotIn("api_key", parsed)
            return [sys.executable, "-I", "-c", "import sys,time\nsys.stdin.read()\n" + script], {}
        self.stack.enter_context(patch.object(self.agent, "_launch", side_effect=launch))

    async def run_agent(self):
        return await self.agent.complete([Message("user", "Use only the fake CLI")], [])

    def assert_cleaned(self):
        for process in self.processes:
            self.assertIsNotNone(process.returncode)
        for broker in self.brokers:
            self.assertFalse(broker.active)
            self.assertFalse(broker.thread.is_alive())
            self.assertFalse(broker.path.exists())
        for home in self.homes:
            self.assertFalse(home.parent.exists())
        self.agent.provider.credential.assert_not_called()

    async def test_success_reaps_child_closes_broker_and_removes_private_home(self):
        self.launch_script(emit([TOOL, *FINAL]))
        result = await self.run_agent()
        self.assertEqual(result.content, "FAKE_OK")
        self.assertFalse(result.stopped)
        self.assertEqual(len(result.tool_events), 1)
        self.assertEqual(result.tool_events[0]["name"], "codex_command_execution")
        self.assertEqual(self.processes[0].returncode, 0)
        self.assert_cleaned()

    async def test_nonzero_exit_does_not_trust_final_event_or_expose_stderr(self):
        self.launch_script(emit([TOOL, *FINAL]) + "\nprint('private-stderr-secret', file=sys.stderr)\nsys.exit(7)")
        result = await self.run_agent()
        self.assertTrue(result.stopped)
        self.assertNotIn("FAKE_OK", result.content)
        self.assertNotIn("private-stderr-secret", repr(result))
        self.assertEqual(len(result.tool_events), 1)
        self.assert_cleaned()

    async def test_timeout_kills_process_but_keeps_completed_tool_record(self):
        self.agent.policy = Policy(("*",), limits=Limits(turn_timeout_seconds=2))
        self.launch_script(emit([TOOL]) + "\ntime.sleep(30)")
        result = await self.run_agent()
        self.assertTrue(result.stopped)
        self.assertIn("超时", result.content)
        self.assertEqual(len(result.tool_events), 1)
        self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)
        self.assert_cleaned()

    async def test_cancellation_kills_process_and_closes_broker(self):
        self.launch_script("time.sleep(30)")
        task = asyncio.create_task(self.run_agent())
        await asyncio.wait_for(self.started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)
        self.assert_cleaned()

    async def test_spawn_failure_still_closes_broker(self):
        self.launch_script(emit(FINAL))
        self.spawn.side_effect = OSError("fake-spawn-failure")
        with self.assertRaises(OSError):
            await self.run_agent()
        self.assert_cleaned()

    async def test_host_bridge_is_disposed_after_success_and_spawn_failure(self):
        from joyagent.cli_host import LoopbackBridge
        bridges = []

        def bridge(*args, **kwargs):
            result = LoopbackBridge(*args, **kwargs)
            bridges.append(result)
            return result

        self.agent.executor.settings["network"] = "host"
        self.launch_script(emit(FINAL))
        with patch("joyagent.cli_backend.LoopbackBridge", side_effect=bridge):
            self.assertFalse((await self.run_agent()).stopped)
            self.spawn.side_effect = OSError("fake-spawn-failure")
            with self.assertRaises(OSError):
                await self.run_agent()
        self.assertEqual(len(bridges), 2)
        self.assertTrue(all(item.closed and not item.thread.is_alive() for item in bridges))
        self.assert_cleaned()

    async def test_oversized_unterminated_event_stops_child_and_closes_broker(self):
        self.launch_script("sys.stdout.write('x' * (1024 * 1024 + 8192))\nsys.stdout.flush()\ntime.sleep(30)")
        with self.assertRaisesRegex(ValueError, "Agent 事件超过上限"):
            await self.run_agent()
        self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)
        self.assert_cleaned()

    async def test_repeated_cancellation_during_reap_still_closes_broker(self):
        self.launch_script(emit(FINAL))
        waiting = asyncio.Event()
        reaping = asyncio.Event()
        release = asyncio.Event()
        process = SimpleNamespace(pid=123456, returncode=None, stdout=asyncio.StreamReader(),
                                  stderr=asyncio.StreamReader(),
                                  stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock()))

        async def wait():
            if process.returncode is None:
                waiting.set()
                await asyncio.Future()
            reaping.set()
            await release.wait()
            return process.returncode

        def kill(*_):
            process.returncode = -signal.SIGKILL

        process.wait = wait
        self.processes.append(process)
        self.spawn.side_effect = None
        self.spawn.return_value = process
        self.stack.enter_context(patch("joyagent.cli_backend.os.killpg", side_effect=kill))
        task = asyncio.create_task(self.run_agent())
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            task.cancel()
            await asyncio.wait_for(reaping.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assert_cleaned()
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_launch_keeps_socket_and_profiles_readonly_with_clean_environment(self):
        workspace = Workspace(self.root / "workspace")
        executor = object.__new__(SandboxExecutor)
        executor.settings = execution_settings(SimpleNamespace(data={}))
        executor.workspace = workspace
        self.agent.executor = executor
        plan, socket = self.root / "plan.json", self.root / "model.sock"
        for backend in ("codex", "pi", "deepseek"):
            with self.subTest(backend=backend), patch("joyagent.cli_backend.shutil.which", return_value="/usr/bin/python3"), \
                    patch("joyagent.cli_backend.native_executable", return_value=self.agent.root / "fake-native"):
                self.agent.backend = backend
                home = self.root / backend
                with patch.dict(os.environ, {"IDEALAB_API_TOKEN": "fake-owner-secret",
                                             "HTTP_PROXY": "http://private-proxy.invalid"}):
                    command, env = self.agent._launch(home, plan, socket)
                self.assertIn("--unshare-all", command)
                self.assertIn("--die-with-parent", command)
                self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
                mounts = {command[index + 2]: (argument, command[index + 1])
                          for index, argument in enumerate(command) if argument in {"--ro-bind", "--bind"}}
                self.assertEqual(mounts["/workspace"], ("--bind", str(workspace.root)))
                self.assertEqual(mounts["/run/joyagent/model.sock"], ("--ro-bind", str(socket)))
                self.assertEqual(mounts["/run/joyagent/plan.json"], ("--ro-bind", str(plan)))
                self.assertEqual(mounts[str(self.agent.root)], ("--ro-bind", str(self.agent.root)))
                if backend == "codex":
                    source = Path(mounts["/proc"][1])
                    self.assertEqual(mounts["/proc"][0], "--ro-bind")
                    self.assertNotEqual(str(source), "/proc")
                    self.assertEqual(list(source.iterdir()), [source / "self"])
                    self.assertEqual(os.readlink(source / "self/exe"), str(self.agent.root / "fake-native"))
                if backend == "deepseek":
                    self.assertEqual(mounts[str(home / "dsh")], ("--ro-bind", str(home / "dsh")))
                    self.assertNotIn(str(home / "dsh/joyagent.patch.yml"), mounts)
                elif backend == "pi":
                    self.assertEqual(mounts[str(home / "pi/models.json")],
                                     ("--ro-bind", str(home / "pi/models.json")))
                self.assertEqual(env["HOME"], str(home))
                self.assertNotIn("IDEALAB_API_TOKEN", env)
                self.assertNotIn("HTTP_PROXY", env)
                self.assertNotIn("LD_PRELOAD", env)
                self.assertNotIn("BUN_INSPECT_PRELOAD", env)
                self.assertNotIn("fake-owner-secret", repr((command, env)))
