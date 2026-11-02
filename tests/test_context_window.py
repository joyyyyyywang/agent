"""Prompt windows retain two user turns without rewriting durable history."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from joyagent.agent import Agent
from joyagent.memory import MemoryStore
from joyagent.policy import Limits, Policy
from joyagent.tools import ToolRegistry
from joyagent.types import Message, ModelReply, ToolCall, ToolSpec


class RecordingProvider:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append(messages)
        return next(self.replies)


class ContextWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.memory = MemoryStore(self.root / "data")
        self.addCleanup(self.memory.close)
        self.soul = self.root / "SOUL.md"
        self.soul.write_text("Owner-controlled instructions.")
        self.policy = Policy(("echo",), limits=Limits(max_steps=6, max_tool_calls=6))
        self.tools = ToolRegistry(self.policy)
        self.tools.register(ToolSpec("echo", "Echo", {"type": "object"}), lambda: "verified")

    def agent(self, provider, **limits):
        policy = replace(self.policy, limits=replace(self.policy.limits, **limits))
        return Agent(provider, self.memory, self.tools, policy, self.soul)

    def prior(self, *, count=5, session="chat", with_tools=False):
        for number in range(count):
            messages = [Message("user", f"old-{number}")]
            if with_tools:
                messages.extend([
                    Message("assistant", tool_calls=[ToolCall(f"prior-{number}", "echo", {})]),
                    Message("tool", "verified", tool_call_id=f"prior-{number}", name="echo"),
                ])
            messages.append(Message("assistant", f"answer-{number}"))
            self.memory.append_messages(session, messages)

    async def test_default_keeps_only_current_and_two_previous_turns_in_same_session(self):
        self.prior()
        self.prior(count=2, session="other")
        self.memory.remember("preference", "Use concise Chinese.")
        provider = RecordingProvider([ModelReply("new answer")])
        agent = self.agent(provider)
        result = await agent.chat("current", "chat")
        self.assertFalse(result.stopped)
        sent = provider.requests[0]
        self.assertEqual([m.content for m in sent if m.role == "user"], ["old-3", "old-4", "current"])
        self.assertEqual([m.content for m in sent[1:]], ["old-3", "answer-3", "old-4", "answer-4", "current"])
        self.assertIn("Owner-controlled instructions.", sent[0].content)
        self.assertIn("Use concise Chinese.", sent[0].content)
        self.assertEqual(len(self.memory.history("chat")), 12)
        self.assertEqual(len(self.memory.history("other")), 4)
        self.assertEqual(agent._context_starts, {})

    async def test_historical_tool_exchanges_are_whole_and_do_not_count_as_extra_rounds(self):
        self.prior(with_tools=True)
        provider = RecordingProvider([ModelReply("done")])
        await self.agent(provider).chat("current", "chat")
        sent = provider.requests[0]
        self.assertEqual([m.content for m in sent if m.role == "user"], ["old-3", "old-4", "current"])
        calls = [call.id for message in sent for call in message.tool_calls]
        results = [message.tool_call_id for message in sent if message.role == "tool"]
        self.assertEqual(calls, ["prior-3", "prior-4"])
        self.assertEqual(results, calls)

    async def test_current_multistep_tools_keep_original_input_and_all_completed_results(self):
        self.prior()
        provider = RecordingProvider([
            *(ModelReply(tool_calls=[ToolCall(f"current-{n}", "echo", {})]) for n in range(4)),
            ModelReply("done"),
        ])
        result = await self.agent(provider).chat("original current task", "chat")
        self.assertFalse(result.stopped)
        self.assertEqual(len(provider.requests), 5)
        sent = provider.requests[-1]
        self.assertEqual([m.content for m in sent if m.role == "user"], ["old-3", "old-4", "original current task"])
        self.assertEqual([m.tool_call_id for m in sent if m.role == "tool"], [f"current-{n}" for n in range(4)])
        self.assertNotIn("old-2", [m.content for m in sent])

    async def test_guidance_keeps_active_original_input_even_with_tiny_history_hint(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Blocked(RecordingProvider):
            async def complete(self, messages, tools):
                self.requests.append(messages)
                if len(self.requests) == 1:
                    entered.set()
                    await release.wait()
                return ModelReply("answer")
        provider = Blocked([])
        agent = self.agent(provider, history_messages=1)
        task = asyncio.create_task(agent.chat("original current task", "chat", turn_id="turn_1234567890123456"))
        await entered.wait()
        for n in range(4):
            agent.steer(f"guide-{n}", "chat", turn_id="turn_1234567890123456", request_id=f"request_{n:016d}")
        release.set()
        result = await task
        self.assertFalse(result.stopped)
        self.assertEqual([m.content for m in provider.requests[1] if m.role == "user"],
                         ["original current task", "guide-0", "guide-1", "guide-2", "guide-3"])
        self.assertTrue(all(row["status"] == "applied" for row in result.steering))

    async def test_context_budget_cannot_drop_original_task_to_fit_later_guidance(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Blocked(RecordingProvider):
            async def complete(self, messages, tools):
                self.requests.append(messages)
                entered.set()
                await release.wait()
                return ModelReply("old answer")
        provider = Blocked([])
        agent = self.agent(provider, context_chars=4000)
        task = asyncio.create_task(agent.chat("original current task", "chat", turn_id="turn_1234567890123456"))
        await entered.wait()
        for n in range(3):
            agent.steer(f"guide-{n} " + "x" * 1400, "chat", turn_id="turn_1234567890123456", request_id=f"request_{n:016d}")
        release.set()
        result = await task
        self.assertTrue(result.stopped)
        self.assertIn("上下文上限", result.text)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.memory.history("chat")[0].content, "original current task")

    async def test_classifier_receives_the_same_two_recent_turns_and_current_input(self):
        self.prior(with_tools=True)
        provider = RecordingProvider([ModelReply("done")])
        selected = []
        async def choose(text, history):
            selected.append((text, history))
            return provider, {}
        await self.agent(provider).chat("current", "chat", provider_selector=choose)
        text, history = selected[0]
        self.assertEqual(text, "current")
        self.assertEqual([m.content for m in history if m.role == "user"], ["old-3", "old-4", "current"])
        self.assertEqual([m.tool_call_id for m in history if m.role == "tool"], ["prior-3", "prior-4"])

    def test_oversized_current_execution_fails_closed_instead_of_losing_its_start(self):
        self.memory.append_messages("chat", [Message("user", "original"),
            *[Message("assistant", f"step-{n}") for n in range(999)]])
        self.memory.append_message("chat", Message("assistant", "one more step"))
        agent = self.agent(RecordingProvider([]))
        agent._context_starts["chat"] = 0
        with self.assertRaisesRegex(ValueError, "上下文上限"):
            agent._context(Message("system", "rules"), "chat")

    def test_context_turns_policy_default_override_and_validation(self):
        path = self.root / "policy.toml"
        path.write_text('[tools]\nallow=[]\n')
        self.assertEqual(Policy.load(path).limits.context_turns, 2)
        path.write_text('[limits]\ncontext_turns=1\n')
        self.assertEqual(Policy.load(path).limits.context_turns, 1)
        for value in ("0", "-1", "true", '"2"', "101"):
            with self.subTest(value=value):
                path.write_text('[limits]\ncontext_turns=' + value + '\n')
                with self.assertRaises(ValueError):
                    Policy.load(path)


if __name__ == "__main__":
    unittest.main()
