"""Runtime panel snapshots use fake processes/models and temporary state only."""
import asyncio
from collections import OrderedDict
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from joyagent.agent_status import native_host_pids
from joyagent.config import Config
from joyagent.resident_backend import ResidentManager
from joyagent.runtime import agent_status_snapshot, build_runtime
from joyagent.types import Message


BACKENDS = ("joyagent", "codex", "pi", "deepseek")


def configuration(root):
    (root / "SOUL.md").write_text("Use actual tool results.")
    (root / "policy.toml").write_text('[tools]\nallow=["*"]\n')
    return Config(root / "agent.toml", {
        "agent": {"workspace": "workspace", "data_dir": "data"},
        "model": {"provider": "demo", "model": "demo"},
        "routing": {"enabled": False}, "decision": {"enabled": False},
        "backends": {name: {"enabled": True} for name in BACKENDS[1:]},
    })


class AgentStatusRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.config = configuration(self.root)
        self.runtime = await build_runtime(self.config)
        self.addAsyncCleanup(self.runtime.close)
        self.catalog = [{"id": name, "label": self.config.backend_label(name), "available": True, "reason": ""}
                        for name in BACKENDS]
        self.enterContext(patch.object(self.runtime, "backend_catalog", return_value=self.catalog))

    async def test_stopped_snapshot_preserves_counts_without_creating_workers(self):
        store = self.runtime.memory
        store.create_conversation("web:empty")
        store.append_messages("web:both", [Message("assistant", "private body"),
                                          Message("assistant", "native answer", backend="codex")])
        store.set_conversation_archived("web:both", True)
        store.append_message("cli:hidden", Message("assistant", "not Web", backend="pi"))
        before = sorted(self.config.data_dir.iterdir())
        with patch.object(self.runtime, "_get_resident_manager", side_effect=AssertionError("must remain lazy")):
            result = self.runtime.agent_status()
        self.assertEqual(result["conversation_count"], 2)
        self.assertEqual(result["archived_conversation_count"], 1)
        self.assertEqual(result["resident_session_count"], 0)
        self.assertEqual(result["running_agent_count"], 1)
        rows = {row["id"]: row for row in result["agents"]}
        self.assertEqual(rows["joyagent"]["pid"], os.getpid())
        self.assertIsNone(rows["joyagent"]["resident_session_count"])
        self.assertEqual(rows["joyagent"]["status"], "ready")
        self.assertEqual(rows["joyagent"]["session_count"], 1)
        self.assertEqual(rows["codex"]["session_count"], 1)
        self.assertEqual(rows["pi"]["session_count"], 0)
        for name in BACKENDS[1:]:
            self.assertEqual(rows[name]["status"], "stopped")
            self.assertIsNone(rows[name]["pid"])
            self.assertIsNone(rows[name]["manager_pid"])
        self.assertIsNone(self.runtime._resident_manager)
        self.assertEqual(sorted(self.config.data_dir.iterdir()), before)
        for private in ("private body", "native answer", "web:both", str(self.root)):
            self.assertNotIn(private, json.dumps(result))

    async def test_active_turn_is_visible_before_process_start_and_cancel_clears_it(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()

        with patch.object(self.runtime, "_chat", side_effect=blocked):
            task = asyncio.create_task(self.runtime.chat("question", "web:active", backend="codex"))
            await entered.wait()
            result = self.runtime.agent_status()
            rows = {row["id"]: row for row in result["agents"]}
            self.assertEqual(rows["codex"]["status"], "starting")
            self.assertEqual(result["running_agent_count"], 1)
            self.assertIsNone(self.runtime._resident_manager)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.runtime._active_backends, {})
        self.assertEqual(self.runtime._active_sessions, {})
        self.assertEqual(self.runtime.agent_status()["agents"][1]["status"], "stopped")

    async def test_explicit_tool_busy_is_joyagent_and_failure_resets_backend_count(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("fake failure")

        with patch.object(self.runtime, "_chat", side_effect=blocked):
            task = asyncio.create_task(self.runtime.chat("/tool time_now {}", "web:tool", backend="pi"))
            await entered.wait()
            rows = {row["id"]: row for row in self.runtime.agent_status()["agents"]}
            self.assertEqual(rows["joyagent"]["status"], "busy")
            self.assertEqual(rows["pi"]["status"], "stopped")
            release.set()
            with self.assertRaisesRegex(RuntimeError, "fake failure"):
                await task
        self.assertEqual(self.runtime._active_backends, {})
        self.assertEqual(self.runtime.agent_status()["agents"][0]["status"], "ready")

    async def test_queued_turn_counts_do_not_clear_busy_before_last_turn_ends(self):
        entered = [asyncio.Event(), asyncio.Event()]
        released = [asyncio.Event(), asyncio.Event()]

        async def blocked(text, *args, **kwargs):
            number = int(text)
            entered[number].set()
            await released[number].wait()

        with patch.object(self.runtime, "_chat", side_effect=blocked):
            tasks = [asyncio.create_task(self.runtime.chat(str(i), "web:queue", backend="joyagent")) for i in range(2)]
            await asyncio.gather(*(event.wait() for event in entered))
            released[0].set()
            await tasks[0]
            self.assertEqual(self.runtime.agent_status()["agents"][0]["status"], "busy")
            released[1].set()
            await tasks[1]
        self.assertEqual(self.runtime.agent_status()["agents"][0]["status"], "ready")

    async def test_non_web_work_does_not_enter_web_active_counters(self):
        async def inspect(*args, **kwargs):
            self.assertEqual(self.runtime._active_backends, {})
        with patch.object(self.runtime, "_chat", side_effect=inspect):
            await self.runtime.chat("CLI task", "cli:one", backend="joyagent")

    async def test_unavailable_and_live_worker_rows_preserve_public_contract(self):
        self.catalog[2].update(available=False, reason="管理员尚未安装")
        snapshot = {"codex": {"status": "idle", "pid": 4321, "manager_pid": 4000,
                              "resident_session_count": 2, "reason": "", "running": True},
                    "deepseek": {"status": "error", "pid": None, "manager_pid": None,
                                 "resident_session_count": 0, "reason": "连接已断开", "running": False}}
        fake = SimpleNamespace(snapshot=Mock(return_value=snapshot), close=AsyncMock())
        self.runtime._resident_manager = fake
        self.runtime._active_backends["codex"] = 1
        result = self.runtime.agent_status()
        self.assertEqual(result["running_agent_count"], 2)
        self.assertEqual(result["resident_session_count"], 2)
        rows = {row["id"]: row for row in result["agents"]}
        self.assertEqual(rows["codex"]["status"], "busy")
        self.assertEqual(rows["codex"]["pid"], 4321)
        self.assertEqual(rows["pi"]["status"], "unavailable")
        self.assertNotIn("opencode", rows)
        self.assertEqual(rows["deepseek"]["status"], "error")
        self.assertNotIn("running", rows["codex"])
        self.assertEqual(snapshot["codex"]["status"], "idle", "projection must not mutate worker snapshot")

    async def test_uncached_projection_does_not_need_a_runtime(self):
        result = agent_status_snapshot(self.config, {"total": 9, "archived": 3, "by_backend": {"pi": 4}})
        self.assertEqual(result["conversation_count"], 9)
        self.assertEqual(result["archived_conversation_count"], 3)
        rows = {row["id"]: row for row in result["agents"]}
        self.assertEqual(rows["pi"]["session_count"], 4)
        self.assertEqual(rows["pi"]["status"], "stopped")


class ResidentStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.config = configuration(self.root)
        self.manager = ResidentManager(self.config, None, None, None)

    def worker(self, *, pid=100, native=3, busy=False, exited=False, failed=False):
        process = None if pid is None else SimpleNamespace(pid=pid, returncode=0 if exited else None)
        return SimpleNamespace(process=process, native_pid=native, busy=busy,
                               tasks=[Mock(done=Mock(return_value=failed))], _termination_requested=False,
                               _close_task=None, sessions={"web:a": object(), "web:b": object(), "cli:other": object()})

    def test_snapshot_never_creates_registry_or_state(self):
        with patch.object(self.manager, "_registry", side_effect=AssertionError("no pool")), \
                patch("joyagent.resident_backend.native_host_pids", side_effect=AssertionError("no proc scan")):
            self.assertEqual(self.manager.snapshot(), {})
        self.assertIsNone(self.manager._pool)
        self.assertIsNone(self.manager.state_lock)
        self.assertFalse(self.manager.root.exists())

    def test_snapshot_is_visitor_scoped_counts_only_web_and_keeps_lru_order(self):
        stranger = object()
        workers = OrderedDict([((stranger, "codex"), self.worker(pid=999)),
                               ((self.manager, "codex"), self.worker()),
                               ((self.manager, "pi"), self.worker(pid=200, busy=True))])
        self.manager._pool = SimpleNamespace(workers=workers)
        order = list(workers)
        with patch("joyagent.resident_backend.native_host_pids", return_value={"codex": 110, "pi": 210}) as resolve:
            result = self.manager.snapshot()
        self.assertEqual(set(result), {"codex", "pi"})
        self.assertEqual(result["codex"]["status"], "idle")
        self.assertEqual(result["pi"]["status"], "busy")
        self.assertEqual(result["codex"]["resident_session_count"], 2)
        self.assertEqual(result["codex"]["pid"], 110)
        self.assertEqual(result["codex"]["manager_pid"], 100)
        resolve.assert_called_once_with({"codex": (100, 3), "pi": (200, 3)})
        self.assertEqual(list(workers), order)
        self.assertFalse(self.manager.root.exists())

    def test_starting_dead_or_failed_process_never_reports_a_native_pid(self):
        for worker, status, running in ((self.worker(pid=None, native=None), "starting", False),
                                        (self.worker(native=None), "starting", True),
                                        (self.worker(exited=True), "error", False),
                                        (self.worker(failed=True), "error", True)):
            with self.subTest(status=status, running=running):
                self.manager._pool = SimpleNamespace(workers={(self.manager, "codex"): worker})
                with patch("joyagent.resident_backend.native_host_pids", return_value={}):
                    row = self.manager.snapshot()["codex"]
                self.assertEqual(row["status"], status)
                self.assertEqual(row["running"], running)
                self.assertIsNone(row["pid"])
                self.assertEqual(row["resident_session_count"], 0)


class HostPidTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def process(self, pid, parent, namespaces, *, start=1, state="S"):
        path = self.root / str(pid)
        path.mkdir(exist_ok=True)
        (path / "status").write_text(f"Name:\tfixture\nState:\t{state} (fixture)\nPPid:\t{parent}\n"
                                    + "NSpid:\t" + "\t".join(map(str, namespaces)) + "\n")
        (path / "stat").write_text(f"{pid} (fixture with spaces) " + " ".join([state, *("0" for _ in range(18)), str(start)]))

    def test_same_inner_pid_in_other_visitor_or_nested_namespace_is_not_selected(self):
        self.process(100, 1, [100])
        self.process(101, 100, [101, 1])
        self.process(103, 101, [103, 3])
        self.process(104, 103, [104, 4, 3])
        self.process(200, 1, [200])
        self.process(203, 200, [203, 3])
        result = native_host_pids({"codex": (100, 3), "pi": (200, 3)}, proc_root=self.root)
        self.assertEqual(result, {"codex": 103, "pi": 203})
        self.assertFalse((self.root / "103/cmdline").exists())
        self.assertFalse((self.root / "103/environ").exists())

    def test_missing_ambiguous_dead_or_invalid_metadata_returns_no_guessed_pid(self):
        self.process(100, 1, [100])
        self.process(103, 100, [103, 3])
        self.process(104, 100, [104, 3])
        self.assertEqual(native_host_pids({"codex": (100, 3)}, proc_root=self.root), {})
        self.process(104, 100, [104, 3], state="Z")
        self.assertEqual(native_host_pids({"codex": (100, 3)}, proc_root=self.root), {"codex": 103})
        (self.root / "103/status").write_text("PPid: bad\nNSpid: 103 3\nState: S\n")
        self.assertEqual(native_host_pids({"codex": (100, 3)}, proc_root=self.root), {})
        self.assertEqual(native_host_pids({"codex": (100, 3)}, proc_root=self.root / "missing"), {})
        self.assertEqual(native_host_pids({"codex": (True, 3)}, proc_root=self.root), {})

    def test_reused_pid_is_rejected_by_start_time_recheck(self):
        self.process(100, 1, [100])
        self.process(103, 100, [103, 3])
        from joyagent import agent_status
        original = agent_status._metadata
        reads = {}

        def replaced(path):
            reads[path.name] = reads.get(path.name, 0) + 1
            if path.name == "103" and reads[path.name] > 1:
                self.process(103, 100, [103, 3], start=2)
            return original(path)

        with patch("joyagent.agent_status._metadata", side_effect=replaced):
            self.assertEqual(native_host_pids({"codex": (100, 3)}, proc_root=self.root), {})

