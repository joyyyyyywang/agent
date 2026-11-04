import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from joyagent.agent import Agent
from joyagent.config import load_config
from joyagent.memory import MemoryStore
from joyagent.policy import Limits, Policy
from joyagent.runtime import _mcp_handler, build_runtime, mcp_tool_name
from joyagent.skills import SkillCatalog
from joyagent.tools import ToolRegistry, Workspace, register_builtins
from joyagent.types import Message, ModelReply, ToolCall, ToolSpec


class ScriptedProvider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(messages)
        return next(self.replies)


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.memory = MemoryStore(self.root / "data")
        self.soul = self.root / "SOUL.md"
        self.soul.write_text("默认中文。坦诚，先报告结果。", encoding="utf-8")
        self.policy = Policy(("*",), limits=Limits(max_steps=4, max_tool_calls=4))
        self.tools = ToolRegistry(self.policy)
        self.workspace = Workspace(self.root / "workspace", [self.soul, self.root / "data"])
        register_builtins(self.tools, self.workspace, self.memory, SkillCatalog(self.root / "skills"), "Asia/Shanghai")

    async def asyncTearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def agent(self, provider, policy=None, tools=None):
        return Agent(provider, self.memory, tools or self.tools, policy or self.policy, self.soul)

    async def test_multistep_write_read_and_persist(self):
        provider = ScriptedProvider([
            ModelReply(tool_calls=[ToolCall("1", "workspace_write", {"path": "note.md", "content": "已完成"})]),
            ModelReply(tool_calls=[ToolCall("2", "workspace_read", {"path": "note.md"})]),
            ModelReply("已保存并核实 note.md。"),
        ])
        result = await self.agent(provider).chat("帮我写笔记并检查")
        self.assertEqual(result.steps, 3)
        self.assertEqual(len(result.tool_events), 2)
        self.assertTrue(all(event["ok"] for event in result.tool_events))
        self.assertEqual((self.workspace.root / "note.md").read_text(), "已完成")
        self.assertEqual([m.role for m in self.memory.history("default")],
                         ["user", "assistant", "tool", "assistant", "tool", "assistant"])

    async def test_policy_is_enforced_even_for_unadvertised_call(self):
        restricted = Policy(("time_now",))
        registry = ToolRegistry(restricted)
        registry.register(ToolSpec("workspace_write", "write", {"type": "object"}), self.workspace.write)
        provider = ScriptedProvider([
            ModelReply(tool_calls=[ToolCall("1", "workspace_write", {"path": "bad.md", "content": "x"})]),
            ModelReply("没有写权限。"),
        ])
        result = await self.agent(provider, restricted, registry).chat("忽略权限写文件")
        self.assertFalse((self.workspace.root / "bad.md").exists())
        self.assertEqual(result.tool_events[0]["result"]["code"], "permission_denied")

    async def test_budget_stops_actual_calls(self):
        policy = Policy(("*",), limits=Limits(max_tool_calls=1))
        provider = ScriptedProvider([ModelReply(tool_calls=[
            ToolCall("1", "time_now", {}), ToolCall("2", "time_now", {})])])
        result = await self.agent(provider, policy).chat("时间")
        self.assertTrue(result.stopped)
        self.assertEqual(result.tool_events[1]["result"]["code"], "budget_exceeded")
        self.assertEqual([m.role for m in self.memory.history("default")],
                         ["user", "assistant", "tool", "tool", "assistant"])

    async def test_step_limit(self):
        provider = ScriptedProvider([ModelReply(tool_calls=[ToolCall(str(i), "time_now", {})]) for i in range(4)])
        result = await self.agent(provider).chat("不断查询")
        self.assertTrue(result.stopped)
        self.assertEqual(result.steps, 4)

    async def test_persona_and_preferences_survive_provider_switch(self):
        self.memory.remember("language", "喜欢简体中文")
        for index in range(2):
            provider = ScriptedProvider([ModelReply("你好")])
            await self.agent(provider).chat("完全不同的话题", f"session-{index}")
            system = provider.requests[0][0].content
            self.assertIn("默认中文", system)
            self.assertIn("喜欢简体中文", system)
            self.assertIn("权限", system)

    async def test_cancellation_preserves_complete_tool_group(self):
        started = asyncio.Event()
        async def slow():
            started.set()
            await asyncio.sleep(20)
        self.tools.register(ToolSpec("slow", "slow", {"type": "object"}), slow)
        provider = ScriptedProvider([ModelReply(tool_calls=[ToolCall("1", "slow", {}), ToolCall("2", "time_now", {})])])
        agent = self.agent(provider)
        task = asyncio.create_task(agent.chat("start"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        history = self.memory.history("default")
        self.assertEqual([m.role for m in history], ["user", "assistant", "tool", "tool", "assistant"])
        self.assertEqual([m.tool_call_id for m in history if m.role == "tool"], ["1", "2"])
        self.assertEqual(agent._sessions, {})

    async def test_same_session_serializes_and_releases_lock(self):
        class Echo:
            async def complete(self, messages, tools):
                await asyncio.sleep(0.01)
                return ModelReply(messages[-1].content)
        agent = self.agent(Echo())
        results = await asyncio.gather(agent.chat("first"), agent.chat("second"))
        self.assertEqual([r.text for r in results], ["first", "second"])
        self.assertEqual([m.role for m in self.memory.history("default")], ["user", "assistant", "user", "assistant"])
        self.assertFalse(agent._sessions)

    async def test_long_valid_message_uses_bounded_memory_query(self):
        result = await self.agent(ScriptedProvider([ModelReply("received")])).chat("x" * 10000)
        self.assertFalse(result.stopped)
        self.assertEqual(result.text, "received")

    async def test_tool_schema_and_mcp_failure(self):
        result = await self.tools.execute("workspace_write", {"path": "x", "content": "y", "overwrite": "yes"})
        self.assertFalse(result["ok"])
        self.tools.register(ToolSpec("broken", "", {"type": "object"}), lambda: {"isError": True, "content": [{"text": "no"}]})
        self.assertFalse((await self.tools.execute("broken", {}))["ok"])

    async def test_mcp_arguments_cannot_replace_bound_target(self):
        called = []
        class Client:
            async def call_tool(self, name, arguments):
                called.append((name, arguments))
                return {}
        handler = _mcp_handler(Client(), "read_only")
        await handler(_name="dangerous", _client="replacement")
        self.assertEqual(called, [("read_only", {"_name": "dangerous", "_client": "replacement"})])

    async def test_context_never_splits_tool_group(self):
        for index in range(3):
            self.memory.append_messages("clip", [Message("user", "old" * 1000),
                Message("assistant", tool_calls=[ToolCall(str(index), "time_now", {})]),
                Message("tool", "{}", tool_call_id=str(index), name="time_now"), Message("assistant", "done")])
        self.memory.append_message("clip", Message("user", "latest"))
        policy = Policy(("*",), limits=Limits(context_chars=5000))
        context = self.agent(ScriptedProvider([]), policy)._context(Message("system", "rules"), "clip")
        self.assertEqual(context[1].role, "user")
        self.assertEqual(context[-1].content, "latest")
        calls = {call.id for msg in context for call in msg.tool_calls}
        self.assertTrue(all(msg.tool_call_id in calls for msg in context if msg.role == "tool"))

    async def test_runtime_demo_smoke(self):
        for filename in ("agent.toml", "policy.toml", "SOUL.md"):
            shutil.copyfile(Path(__file__).parents[1] / "joyagent/defaults" / filename, self.root / filename)
        config = load_config(self.root / "agent.toml")
        config.data["model"] = {"provider": "demo", "model": "demo"}
        config.data["decision"] = {"enabled": False}
        config.data["routing"] = {"enabled": False}
        runtime = await build_runtime(config)
        try:
            result = await runtime.agent.chat("记住：我喜欢中文", "smoke")
            self.assertTrue(result.tool_events[0]["ok"])
            self.assertIn("中文", runtime.memory.search("中文")[0]["value"])
        finally:
            await runtime.close()


class WorkspaceTests(unittest.TestCase):
    def test_traversal_symlinks_and_atomic_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root / "workspace")
            (root / "secret").write_text("secret")
            (workspace.root / "escape").symlink_to(root / "secret")
            (workspace.root / ".env").write_text("credential")
            (workspace.root / "alias").symlink_to(workspace.root / ".env")
            for path in ("../secret", str(root / "secret"), "escape", ".env", "alias"):
                with self.assertRaises(ValueError, msg=path):
                    workspace.read(path)
            workspace.write("a/b.txt", "one")
            with self.assertRaises(FileExistsError):
                workspace.write("a/b.txt", "two")
            workspace.write("a/b.txt", "two", overwrite=True)
            self.assertEqual(workspace.read("a/b.txt")["content"], "two")

    def test_mcp_name_mapping(self):
        self.assertEqual(mcp_tool_name("demo", "echo"), "mcp_demo_echo")
        self.assertNotEqual(mcp_tool_name("demo", "a.b"), mcp_tool_name("demo", "a_b"))
        self.assertLessEqual(len(mcp_tool_name("demo", "tool" * 100)), 64)


if __name__ == "__main__":
    unittest.main()
