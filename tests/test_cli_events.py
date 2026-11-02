"""Synthetic stdout events: no external executable, model or credential use."""
import json
import unittest

from joyagent.cli_events import CLIEvents


class CLIEventsTests(unittest.TestCase):
    def test_retired_backend_cannot_construct_an_event_parser(self):
        with self.assertRaisesRegex(ValueError, 'unsupported CLI event backend'):
            CLIEvents('opencode')

    def test_codex_warning_tools_and_terminal(self):
        result = CLIEvents("codex")
        for event in [
            {"type": "item.completed", "item": {"type": "error", "message":
                "Model metadata for `qwen3.8-flash` not found. Defaulting to fallback metadata; warning."}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "private thinking"}},
            {"type": "item.completed", "item": {"type": "command_execution", "id": "c1", "command": "pwd",
                "status": "completed", "aggregated_output": "/workspace", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Done"}},
            {"type": "turn.completed"},
        ]:
            result.feed(event)
        self.assertEqual(result.text, "Done")
        self.assertTrue(result.completed)
        self.assertFalse(result.failed)
        self.assertEqual(result.events[0], {"name": "codex_command_execution", "ok": True,
            "result": {"ok": True, "result": {"command": "pwd", "stdout": "/workspace", "exit_code": 0}}})

    def test_codex_fatal_failure_cannot_be_overwritten(self):
        for failure in ({"type": "turn.failed"}, {"type": "error"},
                        {"type": "item.completed", "item": {"type": "error", "message": "real failure"}}):
            result = CLIEvents("codex")
            result.feed(failure)
            result.feed({"type": "turn.completed"})
            self.assertTrue(result.failed)
            self.assertFalse(result.completed)

    def test_codex_tool_failure_can_be_recovered(self):
        result = CLIEvents("codex")
        result.feed({"type": "item.completed", "item": {"type": "command_execution", "id": "c1",
            "status": "failed", "exit_code": 1, "aggregated_output": "no such file"}})
        result.feed({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "m1",
            "server": "test/server", "tool": "echo", "status": "completed", "result": {"content": "ok"}}})
        result.feed({"type": "turn.completed"})
        self.assertEqual([e["ok"] for e in result.events], [False, True])
        self.assertTrue(result.completed)
        self.assertRegex(result.events[1]["name"], r"^[A-Za-z0-9_]+$")

    def test_pi_authoritative_final_and_settled_are_required(self):
        result = CLIEvents("pi")
        result.feed({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "private"}})
        result.feed({"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Checking"}], "stopReason": "toolUse"}})
        result.feed({"type": "tool_execution_end", "toolName": "read", "toolCallId": "one", "isError": False,
                     "result": {"content": [{"type": "text", "text": "file content"}]}})
        result.feed({"type": "agent_end", "willRetry": False})
        self.assertFalse(result.completed)
        result.feed({"type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "private"}, {"type": "text", "text": "Final"}], "stopReason": "stop"}})
        result.feed({"type": "agent_settled", "aborted": False})
        self.assertEqual(result.text, "Final")
        self.assertTrue(result.completed)
        self.assertTrue(result.events[0]["ok"])

    def test_pi_provider_error_is_failed_even_when_cli_exits_zero(self):
        for stop in ("error", "aborted", "length", None):
            result = CLIEvents("pi")
            result.feed({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": stop}})
            result.feed({"type": "agent_settled", "aborted": False})
            self.assertTrue(result.failed)
            self.assertFalse(result.completed)

    def test_deepseek_exact_committed_projection(self):
        result = CLIEvents("deepseek")
        result.feed({"type": "thinking", "text": "private"})
        result.feed({"type": "text", "text": "interim"})
        result.feed({"type": "tool_call", "callId": "one", "tool": "ReadFile", "input": {"path": "a"}})
        result.feed({"type": "tool_result", "callId": "one", "status": "completed", "result": "content"})
        result.feed({"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}})
        self.assertFalse(result.completed)
        result.feed({"type": "final", "text": "Final"})
        self.assertTrue(result.completed)
        self.assertEqual(result.text, "Final")
        self.assertEqual(result.events[0]["name"], "deepseek_ReadFile")

    def test_deepseek_final_after_failed_turn_does_not_claim_success(self):
        result = CLIEvents("deepseek")
        result.feed({"type": "status", "phase": "turn_end", "reason": {"kind": "error"}})
        result.feed({"type": "final", "text": "partial"})
        self.assertTrue(result.failed)
        self.assertFalse(result.completed)
        alone = CLIEvents("deepseek")
        alone.feed({"type": "final", "text": "answer without turn status"})
        self.assertFalse(alone.completed)

    def test_deepseek_unmatched_or_incomplete_tool_call_fails_closed(self):
        result = CLIEvents("deepseek")
        result.feed({"type": "tool_result", "callId": "unknown", "status": "completed", "result": "ok"})
        self.assertTrue(result.failed)
        result = CLIEvents("deepseek")
        result.feed({"type": "tool_call", "callId": "one", "tool": "ReadFile"})
        result.feed({"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}})
        result.feed({"type": "final", "text": "done"})
        self.assertTrue(result.failed)

    def test_total_event_json_budget_text_limit_and_count(self):
        result = CLIEvents("codex", limit=1000)
        for index in range(200):
            result.feed({"type": "item.completed", "item": {"id": str(index), "type": "command_execution",
                "status": "completed", "exit_code": 0, "aggregated_output": '\"\n\\' * 30000}})
        result.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "x" * 10000}})
        self.assertLessEqual(len(result.text), 1000)
        self.assertLessEqual(sum(len(json.dumps(e, ensure_ascii=False, separators=(",", ":"))) for e in result.events), 1000)
        self.assertGreater(len(result.events), 0)
        count = CLIEvents("pi", limit=100000)
        for index in range(100):
            count.feed({"type": "tool_execution_end", "toolCallId": str(index), "toolName": "read", "isError": False, "result": "ok"})
        self.assertEqual(len(count.events), 64)

    def test_tool_output_depth_and_reasoning_are_bounded(self):
        nested = {"thinking": "private", "reasoning_content": "private", "text": "ok"}
        for _ in range(100):
            nested = {"child": nested}
        result = CLIEvents("pi", limit=500)
        result.feed({"type": "tool_execution_end", "toolName": "read", "isError": False, "result": nested})
        self.assertNotIn("private", json.dumps(result.events))
        self.assertLessEqual(len(json.dumps(result.events[0], ensure_ascii=False, separators=(",", ":"))), 500)

    def test_malformed_events_do_not_raise_or_complete(self):
        for backend, event in (("codex", {"type": "item.completed", "item": None}),
                               ("pi", {"type": "message_end", "message": None}),
                               ("deepseek", {"type": "tool_result", "callId": []})):
            result = CLIEvents(backend)
            result.feed(event)
            self.assertTrue(result.failed)
            self.assertFalse(result.completed)


if __name__ == "__main__":
    unittest.main()
