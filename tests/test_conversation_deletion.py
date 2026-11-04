"""Conversation deletion removes stored content and coordinates audit recovery."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from joyagent.memory import MemoryStore
from joyagent.types import Message, ToolCall


SESSION = "web:erase"
SECRET = "content-that-must-be-erased"


class ConversationDeletionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.store = MemoryStore(self.root)
        self.addCleanup(lambda: self.store.close())

    def events(self):
        return [json.loads(line) for line in self.store.audit_path.read_text().splitlines()]

    def outbox(self):
        return [json.loads(row[0]) for row in self.store._db.execute("SELECT payload FROM audit_outbox ORDER BY seq")]

    def prepare(self):
        self.store.create_conversation(SESSION)
        self.store.append_messages(SESSION, [
            Message("user", SECRET),
            Message("assistant", SECRET, tool_calls=[ToolCall("private-call", "time_now", {"input": SECRET})],
                    provider_data={"reasoning_content": SECRET}),
            Message("tool", SECRET, name="time_now", tool_call_id="private-call"),
            Message("assistant", SECRET, backend="codex", tool_events=[{"name": "native", "result": SECRET}]),
        ])
        self.store.set_conversation_archived(SESSION, True)
        self.store.set_conversation_archived(SESSION, False)

    def another(self):
        other = MemoryStore(self.root)
        self.addCleanup(other.close)
        return other

    def test_delete_really_purges_messages_metadata_outbox_and_jsonl_only_for_target(self):
        self.prepare()
        self.store.append_message(SESSION + "-other", Message("user", "keep other conversation"))
        self.store.remember("shared", "keep this independent memory")
        other_history = self.store.history(SESSION + "-other")
        other_row = self.store.get_conversation(SESSION + "-other")
        memories = self.store.list_memories()
        remaining = [event for event in self.events() if event.get("session_id") != SESSION
                     and event.get("conversation", {}).get("session_id") != SESSION]
        result = self.store.delete_conversation(SESSION)
        self.assertEqual(result, {"session_id": SESSION, "deleted": True})
        self.assertTrue(self.store.is_conversation_deleted(SESSION))
        self.assertEqual(self.store.history(SESSION), [])
        self.assertIsNone(self.store.get_conversation(SESSION))
        self.assertEqual(self.store.list_conversations(), [other_row])
        self.assertEqual(self.store.history(SESSION + "-other"), other_history)
        self.assertEqual(self.store.list_memories(), memories)
        self.assertEqual(self.events(), remaining)
        self.assertEqual(self.outbox(), remaining)
        self.assertNotIn(SECRET, self.store.audit_path.read_text())
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (SESSION,)).fetchone()[0], 0)
        tombstone = dict(self.store._db.execute("SELECT * FROM deleted_conversations WHERE session_id=?", (SESSION,)).fetchone())
        self.assertEqual(set(tombstone), {"session_id", "deleted_at"})
        self.assertEqual(tombstone["session_id"], SESSION)

    def test_empty_and_archived_conversations_delete_and_retry_without_resurrection(self):
        for session in ("web:empty", "web:archived"):
            with self.subTest(session=session):
                self.store.create_conversation(session)
                if session.endswith("archived"):
                    self.store.append_message(session, Message("user", SECRET))
                    self.store.set_conversation_archived(session, True)
                expected = {"session_id": session, "deleted": True}
                self.assertEqual(self.store.delete_conversation(session), expected)
                self.assertEqual(self.store.delete_conversation(session), expected)
                with self.assertRaises(ValueError):
                    self.store.create_conversation(session)
                for messages in ([], [Message("user", "stale client")]):
                    with self.assertRaises(ValueError):
                        self.store.append_messages(session, messages)
                with self.assertRaises(ValueError):
                    self.store.set_conversation_archived(session, False)
        self.store.close()
        self.store = MemoryStore(self.root)
        self.assertEqual(self.store.list_conversations(archived=True), [])
        self.assertEqual(self.store.list_conversations(), [])
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(self.events(), [])
        for session in ("web:empty", "web:archived"):
            self.assertTrue(self.store.is_conversation_deleted(session))
            self.assertEqual(self.store.history(session), [])

    def test_unknown_or_invalid_id_cannot_create_tombstone_or_change_audit(self):
        self.prepare()
        events = self.events()
        generation = self.store._audit_generation()
        self.assertFalse(self.store.is_conversation_deleted("web:unknown"))
        with self.assertRaises(ValueError):
            self.store.delete_conversation("web:unknown")
        for value in (None, "", " ", 1, [], "x" * 257):
            for method in (self.store.delete_conversation, self.store.is_conversation_deleted):
                with self.subTest(value=value, method=method.__name__), self.assertRaises(ValueError):
                    method(value)
        self.assertEqual(self.events(), events)
        self.assertEqual(self.store._audit_generation(), generation)
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM deleted_conversations").fetchone()[0], 0)

    def test_sql_failure_rolls_back_messages_metadata_purge_and_tombstone_together(self):
        self.prepare()
        original_row = self.store.get_conversation(SESSION)
        original_history = self.store.history(SESSION)
        original_audit = self.events()
        generation = self.store._audit_generation()
        self.store._db.executescript("""
            CREATE TRIGGER reject_audit_purge BEFORE DELETE ON audit_outbox
            BEGIN SELECT RAISE(ABORT, 'simulated SQL failure'); END;
        """)
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.delete_conversation(SESSION)
        self.assertEqual(self.store.get_conversation(SESSION), original_row)
        self.assertEqual(self.store.history(SESSION), original_history)
        self.assertEqual(self.events(), original_audit)
        self.assertEqual(self.outbox(), original_audit)
        self.assertFalse(self.store.is_conversation_deleted(SESSION))
        self.assertEqual(self.store._audit_generation(), generation)
        self.store._db.execute("DROP TRIGGER reject_audit_purge")
        self.assertTrue(self.store.delete_conversation(SESSION)["deleted"])

    def test_atomic_mirror_replacement_failure_raises_and_same_id_retry_finishes(self):
        self.prepare()
        old_audit = self.store.audit_path.read_bytes()
        with patch("joyagent.memory.os.replace", side_effect=OSError("mirror replacement unavailable")):
            with self.assertRaises(OSError):
                self.store.delete_conversation(SESSION)
        self.assertTrue(self.store.is_conversation_deleted(SESSION))
        self.assertEqual(self.store.history(SESSION), [])
        self.assertIsNone(self.store.get_conversation(SESSION))
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.store.audit_path.read_bytes(), old_audit)
        self.assertEqual(list(self.root.glob(".events-*.tmp")), [])
        self.assertEqual(self.store.delete_conversation(SESSION), {"session_id": SESSION, "deleted": True})
        self.assertEqual(self.events(), [])
        self.assertEqual(json.loads(self.store._audit_cursor_path.read_text())["generation"], self.store._audit_generation())

    def test_other_live_store_flush_detects_generation_even_if_cursor_sequence_still_exists(self):
        self.prepare()
        self.store.remember("kept-last-event", "independent content")
        other = self.another()
        old_cursor = json.loads(other._audit_cursor_path.read_text())
        self.assertFalse(other._audit_dirty)
        with patch.object(self.store, "_rebuild_audit", side_effect=RuntimeError("mirror failed")):
            with self.assertRaises(RuntimeError):
                self.store.delete_conversation(SESSION)
        self.assertIsNotNone(other._db.execute("SELECT 1 FROM audit_outbox WHERE seq=?", (old_cursor["sequence"],)).fetchone())
        self.assertIn(SECRET, other.audit_path.read_text())
        other.remember("next-event", "another independent memory")
        self.assertNotIn(SECRET, other.audit_path.read_text())
        self.assertEqual(self.events(), self.outbox())
        self.assertEqual(len(self.events()), 2)
        self.assertTrue(other.is_conversation_deleted(SESSION))
        with self.assertRaises(ValueError):
            other.append_message(SESSION, Message("user", SECRET))
        with self.assertRaises(ValueError):
            other.create_conversation(SESSION)

    def test_startup_recovers_committed_delete_when_process_died_before_rebuild(self):
        self.prepare()
        with patch.object(self.store, "_rebuild_audit", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                self.store.delete_conversation(SESSION)
        self.store._db.close()
        self.store._closed = True
        self.store = MemoryStore(self.root)
        self.assertTrue(self.store.is_conversation_deleted(SESSION))
        self.assertIsNone(self.store.get_conversation(SESSION))
        self.assertEqual(self.store.history(SESSION), [])
        self.assertEqual(self.events(), [])

    def test_cursor_write_failure_is_not_reported_as_delete_success(self):
        self.prepare()
        with patch.object(self.store, "_write_audit_cursor", side_effect=OSError("cursor failed")):
            with self.assertRaises(OSError):
                self.store.delete_conversation(SESSION)
        self.assertEqual(self.events(), [])
        self.assertTrue(self.store.is_conversation_deleted(SESSION))
        self.assertEqual(self.store.delete_conversation(SESSION), {"session_id": SESSION, "deleted": True})
        self.assertEqual(self.events(), [])

    def test_delete_waits_for_existing_writer_then_purges_its_full_commit_without_deadlock(self):
        self.prepare()
        other = self.another()
        writer_checked = Event()
        release_writer = Event()
        deleter_locked_mirror = Event()
        reject = self.store._reject_deleted
        audit_lock = other._audit_file_lock

        def hold_writer(session):
            reject(session)
            writer_checked.set()
            if not release_writer.wait(5):
                raise RuntimeError("test writer timeout")

        @contextmanager
        def observe_lock():
            with audit_lock():
                deleter_locked_mirror.set()
                yield

        with patch.object(self.store, "_reject_deleted", side_effect=hold_writer), \
                patch.object(other, "_audit_file_lock", observe_lock), ThreadPoolExecutor(max_workers=2) as executor:
            append = executor.submit(self.store.append_message, SESSION, Message("user", "racing write"))
            self.assertTrue(writer_checked.wait(5))
            delete = executor.submit(other.delete_conversation, SESSION)
            try:
                self.assertTrue(deleter_locked_mirror.wait(5))
            finally:
                release_writer.set()
            self.assertTrue(delete.result(timeout=10)["deleted"])
            append.result(timeout=10)
        self.assertEqual(self.store.history(SESSION), [])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.events(), [])
        self.assertTrue(self.store.is_conversation_deleted(SESSION))

    def test_delete_writer_lock_prevents_append_and_create_passing_stale_tombstone_check(self):
        other = self.another()
        for operation in ("append", "create"):
            with self.subTest(operation=operation):
                session = "web:race-" + operation
                self.store.create_conversation(session)
                deletion_in_transaction = Event()
                writer_started = Event()
                release_deletion = Event()

                def trace(statement):
                    if statement.startswith("DELETE FROM messages"):
                        deletion_in_transaction.set()
                        release_deletion.wait(5)

                def observe_writer(statement):
                    if statement == "BEGIN IMMEDIATE":
                        writer_started.set()

                self.store._db.set_trace_callback(trace)
                other._db.set_trace_callback(observe_writer)
                try:
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        delete = executor.submit(self.store.delete_conversation, session)
                        self.assertTrue(deletion_in_transaction.wait(5))
                        if operation == "append":
                            writer = executor.submit(other.append_message, session, Message("user", "must be rejected"))
                        else:
                            writer = executor.submit(other.create_conversation, session)
                        try:
                            self.assertTrue(writer_started.wait(5))
                        finally:
                            release_deletion.set()
                        self.assertTrue(delete.result(timeout=10)["deleted"])
                        with self.assertRaises(ValueError):
                            writer.result(timeout=10)
                finally:
                    release_deletion.set()
                    self.store._db.set_trace_callback(None)
                    other._db.set_trace_callback(None)
                self.assertEqual(self.store.history(session), [])
                self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
