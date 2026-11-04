"""Conversation metadata is durable, isolated and audited without deleting history."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
import warnings

from joyagent.memory import MemoryStore
from joyagent.types import Message


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.store = MemoryStore(self.directory)
        self.addCleanup(lambda: self.store.close())

    def restart(self):
        self.store.close()
        self.store = MemoryStore(self.directory)

    def events(self):
        return [json.loads(line) for line in self.store.audit_path.read_text().splitlines()]

    def test_empty_conversation_creation_persists_without_inventing_messages(self):
        self.assertIsNone(self.store.get_conversation("web:new"))
        created = self.store.create_conversation("web:new")
        self.assertEqual(set(created), {"session_id", "title", "archived", "created_at", "updated_at", "message_count"})
        self.assertEqual(created["session_id"], "web:new")
        self.assertEqual(created["title"], "新会话")
        self.assertIs(created["archived"], False)
        self.assertEqual(created["message_count"], 0)
        self.assertEqual(created["created_at"], created["updated_at"])
        self.assertEqual(self.store.history("web:new"), [])
        self.assertEqual(self.store.list_sessions(), [])
        before = self.events()
        self.assertEqual([event["type"] for event in before], ["conversation.created"])
        self.assertEqual(before[0]["conversation"], created)
        with self.assertRaises(ValueError):
            self.store.create_conversation("web:new")
        self.restart()
        self.assertEqual(self.store.get_conversation("web:new"), created)
        self.assertEqual(self.store.list_conversations(), [created])
        self.assertEqual(self.events(), before)

    def test_automatic_metadata_title_and_counts_track_all_message_roles(self):
        first = "  第一行\n\t第二行\r\n" + "长" * 70
        messages = [Message("system", "owner policy"), Message("assistant", "greeting"),
                    Message("user", first), Message("tool", "not a title"), Message("assistant", "answer")]
        self.store.append_messages("web:auto", messages)
        row = self.store.get_conversation("web:auto")
        self.assertEqual(row["title"], ("第一行 第二行 " + "长" * 70)[:48])
        self.assertEqual(len(row["title"]), 48)
        self.assertEqual(row["message_count"], 5)
        self.assertEqual(len(self.events()), 5)
        self.assertTrue(all(event["type"] == "message.appended" for event in self.events()))
        self.store.append_messages("web:auto", [Message("user", "later title must not replace first"), Message("assistant", "next")])
        latest = self.store.get_conversation("web:auto")
        self.assertEqual(latest["title"], row["title"])
        self.assertEqual(latest["created_at"], row["created_at"])
        self.assertGreaterEqual(latest["updated_at"], row["updated_at"])
        self.assertEqual(latest["message_count"], 7)
        self.assertEqual(self.store.list_sessions()[0], {
            "session_id": "web:auto", "message_count": 7, "updated_at": latest["updated_at"],
        })

    def test_explicit_empty_title_updates_on_first_user_but_never_later_users(self):
        self.store.create_conversation("web:title")
        self.store.append_message("web:title", Message("assistant", "greeting"))
        self.assertEqual(self.store.get_conversation("web:title")["title"], "新会话")
        self.store.append_message("web:title", Message("user", "\x00first\x1b\x7f\x85\u2028\tsecond"))
        self.assertEqual(self.store.get_conversation("web:title")["title"], "first second")
        self.store.append_message("web:title", Message("user", "another question"))
        self.assertEqual(self.store.get_conversation("web:title")["title"], "first second")

    def test_first_user_with_default_title_or_empty_text_is_still_first(self):
        for session, content in (("web:default", "新会话"), ("web:blank", " \t\n ")):
            with self.subTest(session=session):
                self.store.append_message(session, Message("user", content))
                self.store.append_message(session, Message("user", "must not become title"))
                self.assertEqual(self.store.get_conversation(session)["title"], "新会话")
                self.assertEqual(self.store.get_conversation(session)["message_count"], 2)

    def test_archive_restore_are_idempotent_and_preserve_history_and_original_events(self):
        messages = [Message("user", "important conversation"), Message("assistant", "saved answer")]
        self.store.append_messages("web:saved", messages)
        original_events = self.events()
        archived = self.store.set_conversation_archived("web:saved", True)
        self.assertIs(archived["archived"], True)
        self.assertEqual(self.store.list_conversations(), [])
        self.assertEqual(self.store.list_conversations(archived=True), [archived])
        self.assertEqual(self.store.set_conversation_archived("web:saved", True), archived)
        self.assertEqual(len(self.events()), len(original_events) + 1)
        self.assertEqual(self.store.history("web:saved"), messages)
        self.restart()
        self.assertEqual(self.store.get_conversation("web:saved"), archived)
        restored = self.store.set_conversation_archived("web:saved", False)
        self.assertIs(restored["archived"], False)
        self.assertEqual(self.store.set_conversation_archived("web:saved", False), restored)
        self.assertEqual(self.store.list_conversations(), [restored])
        self.assertEqual(self.store.list_conversations(archived=True), [])
        self.assertEqual(self.store.history("web:saved"), messages)
        events = self.events()
        self.assertEqual(events[:len(original_events)], original_events)
        self.assertEqual([event["type"] for event in events[len(original_events):]],
                         ["conversation.archived", "conversation.restored"])
        self.assertEqual(events[-1]["conversation"], restored)
        self.assertEqual(self.store.list_sessions()[0]["message_count"], 2)

    def test_appending_to_archived_conversation_keeps_archive_and_title(self):
        self.store.append_message("web:archived", Message("user", "initial"))
        self.store.set_conversation_archived("web:archived", True)
        self.store.append_messages("web:archived", [Message("user", "later"), Message("assistant", "answer")])
        row = self.store.get_conversation("web:archived")
        self.assertEqual((row["title"], row["message_count"]), ("initial", 3))
        self.assertIs(row["archived"], True)
        self.assertEqual(self.store.list_conversations(), [])

    def test_literal_prefix_filter_and_archive_filter_precede_limit(self):
        for session in ("web:first", "web:second", "cli:newer", "telegram:newest", "WEB:uppercase",
                        "web_%:literal", "webAX:other", "web:'quoted", "web:archived"):
            self.store.create_conversation(session)
        self.store.set_conversation_archived("web:archived", True)
        self.assertEqual([row["session_id"] for row in self.store.list_conversations(limit=1)], ["web:'quoted"])
        self.assertEqual([row["session_id"] for row in self.store.list_conversations(prefix="web_%:")], ["web_%:literal"])
        self.assertEqual([row["session_id"] for row in self.store.list_conversations(prefix="web:'")], ["web:'quoted"])
        self.assertEqual(self.store.list_conversations(prefix="' OR 1=1 --"), [])
        self.assertEqual([row["session_id"] for row in self.store.list_conversations(archived=True)], ["web:archived"])
        self.assertEqual(len(self.store.list_conversations(prefix="")), 8)

    def test_migration_from_messages_is_idempotent_preserves_timestamps_and_audit(self):
        self.store.append_messages("web:old", [Message("assistant", "greeting"), Message("user", "Old\n title"),
                                                Message("assistant", "answer"), Message("user", "later")])
        self.store.append_message("cli:old", Message("user", "CLI title"))
        expected = {row["session_id"]: row for row in self.store.list_conversations(prefix="")}
        audit = self.events()
        history = self.store.history("web:old")
        self.store.close()
        with closing(sqlite3.connect(self.directory / "memory.sqlite3")) as db, db:
            db.execute("DROP TABLE conversations")
        self.store = MemoryStore(self.directory)
        self.assertEqual({row["session_id"]: row for row in self.store.list_conversations(prefix="")}, expected)
        self.assertEqual(self.store.history("web:old"), history)
        self.assertEqual(self.events(), audit)
        self.restart()
        self.assertEqual({row["session_id"]: row for row in self.store.list_conversations(prefix="")}, expected)
        self.assertEqual(self.events(), audit)

    def test_migration_only_fills_missing_metadata_and_preserves_archived_and_empty_rows(self):
        self.store.append_message("web:old", Message("user", "old"))
        self.store.append_message("web:archived", Message("user", "archive me"))
        archived = self.store.set_conversation_archived("web:archived", True)
        empty = self.store.create_conversation("web:empty")
        self.store.close()
        with closing(sqlite3.connect(self.directory / "memory.sqlite3")) as db, db:
            db.execute("DELETE FROM conversations WHERE session_id=?", ("web:old",))
        self.store = MemoryStore(self.directory)
        self.assertEqual(self.store.get_conversation("web:old")["title"], "old")
        self.assertEqual(self.store.get_conversation("web:archived"), archived)
        self.assertEqual(self.store.get_conversation("web:empty"), empty)

    def test_explicit_create_and_archive_roll_back_when_outbox_insert_fails(self):
        with patch.object(self.store, "_event", side_effect=RuntimeError("outbox failed")):
            with self.assertRaises(RuntimeError):
                self.store.create_conversation("web:rolled-back")
        self.assertIsNone(self.store.get_conversation("web:rolled-back"))
        self.assertEqual(self.events(), [])
        original = self.store.create_conversation("web:existing")
        audit = self.events()
        with patch.object(self.store, "_event", side_effect=RuntimeError("outbox failed")):
            with self.assertRaises(RuntimeError):
                self.store.set_conversation_archived("web:existing", True)
        self.assertEqual(self.store.get_conversation("web:existing"), original)
        self.assertEqual(self.events(), audit)

    def test_message_metadata_and_message_audit_commit_or_roll_back_together(self):
        with patch.object(self.store, "_first_user_title", side_effect=RuntimeError("metadata failed")):
            with self.assertRaises(RuntimeError):
                self.store.append_messages("web:atomic", [Message("user", "question"), Message("assistant", "answer")])
        self.assertEqual(self.store.history("web:atomic"), [])
        self.assertIsNone(self.store.get_conversation("web:atomic"))
        self.assertEqual(self.events(), [])
        self.store.create_conversation("web:atomic")
        before = self.store.get_conversation("web:atomic")
        audit = self.events()
        with patch.object(self.store, "_event", side_effect=RuntimeError("outbox failed")):
            with self.assertRaises(RuntimeError):
                self.store.append_message("web:atomic", Message("user", "question"))
        self.assertEqual(self.store.get_conversation("web:atomic"), before)
        self.assertEqual(self.store.history("web:atomic"), [])
        self.assertEqual(self.events(), audit)

    def test_archival_survives_mirror_failure_and_replays_after_restart(self):
        self.store.append_message("web:durable", Message("user", "persist"))
        with patch.object(Path, "open", side_effect=OSError("mirror unavailable")):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = self.store.set_conversation_archived("web:durable", True)
        self.assertTrue(caught)
        self.assertTrue(self.store.get_conversation("web:durable")["archived"])
        # Simulate a crash before another operation can deliver the outbox.
        self.store._db.close()
        self.store._closed = True
        self.store = MemoryStore(self.directory)
        self.assertEqual(self.store.get_conversation("web:durable"), result)
        self.assertEqual(self.events()[-1]["type"], "conversation.archived")
        self.assertEqual(self.store.history("web:durable")[0].content, "persist")

    def test_two_connections_concurrent_writes_keep_count_first_title_and_archive(self):
        self.store.create_conversation("web:concurrent")
        self.store.set_conversation_archived("web:concurrent", True)
        other = MemoryStore(self.directory)
        self.addCleanup(other.close)
        barrier = Barrier(2)

        def write(store, label):
            barrier.wait(timeout=5)
            for index in range(10):
                store.append_messages("web:concurrent", [Message("user", f"{label}-{index}"), Message("assistant", "answer")])

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(write, store, label) for store, label in ((self.store, "a"), (other, "b"))]
            for future in futures:
                future.result(timeout=10)
        row = self.store.get_conversation("web:concurrent")
        self.assertEqual(row, other.get_conversation("web:concurrent"))
        self.assertEqual(row["message_count"], 40)
        self.assertEqual(row["title"], self.store.history("web:concurrent", limit=100)[0].content)
        self.assertIs(row["archived"], True)
        self.assertEqual(len(self.events()), 42)

    def test_input_validation_unknown_ids_and_empty_append_have_no_side_effects(self):
        self.store.append_messages("web:none", [])
        self.assertIsNone(self.store.get_conversation("web:none"))
        for value in (None, "", " ", 1, "x" * 257):
            with self.subTest(session=value):
                for operation in (self.store.get_conversation, self.store.create_conversation):
                    with self.assertRaises(ValueError):
                        operation(value)
        with self.assertRaises(ValueError):
            self.store.set_conversation_archived("web:missing", True)
        for value in (None, 0, 1, "false", []):
            with self.subTest(archived=value):
                with self.assertRaises(ValueError):
                    self.store.set_conversation_archived("web:missing", value)
                with self.assertRaises(ValueError):
                    self.store.list_conversations(archived=value)
        for value in (None, 1, [], "x" * 257, "\0"):
            with self.assertRaises(ValueError):
                self.store.list_conversations(prefix=value)
        for value in (0, -1, 1001, True, "1"):
            with self.assertRaises(ValueError):
                self.store.list_conversations(limit=value)
        self.assertEqual(self.store.list_conversations(), [])
        self.assertEqual(self.events(), [])

    def test_custom_title_survives_first_messages_archive_restart_and_repeated_rename(self):
        for index, title in enumerate(("我的项目", "新会话")):
            session = f"web:rename-{index}"
            original = self.store.create_conversation(session)
            renamed = self.store.rename_conversation(session, "  " + title + "  ")
            self.assertEqual(renamed["title"], title)
            self.assertEqual(set(renamed), set(original))
            self.assertEqual(renamed["created_at"], original["created_at"])
            self.assertEqual(renamed["message_count"], 0)
            self.assertEqual(self.store.history(session), [])
            events = self.events()
            self.assertEqual(events[-1]["type"], "conversation.renamed")
            self.assertEqual(events[-1]["conversation"], renamed)
            self.assertEqual(self.store.rename_conversation(session, title), renamed)
            self.assertEqual(self.events(), events)
            self.restart()
            messages = [Message("user", "不要覆盖手动名称"), Message("assistant", "answer")]
            self.store.append_messages(session, messages)
            self.assertEqual(self.store.get_conversation(session)["title"], title)
            self.store.set_conversation_archived(session, True)
            renamed = self.store.rename_conversation(session, "归档新名称")
            self.assertTrue(renamed["archived"])
            self.assertEqual(renamed["message_count"], 2)
            self.assertEqual(self.store.history(session), messages)
            self.restart()
            self.assertEqual(self.store.get_conversation(session), renamed)

    def test_rename_validation_is_single_line_bounded_and_does_not_create_or_resurrect(self):
        original = self.store.create_conversation("web:rename")
        audit = self.events()
        for title in (None, 1, True, [], {}, "", "  ", "x" * 81, "😀" * 81,
                      "\nname", "name\n", "na\rme", "na\tme", "na\0me", "na\x7fme",
                      "na\x85me", "na\u2028me", "na\u2029me", "na\ud800me"):
            with self.subTest(title=repr(title)), self.assertRaises(ValueError):
                self.store.rename_conversation("web:rename", title)
        self.assertEqual(self.store.get_conversation("web:rename"), original)
        self.assertEqual(self.events(), audit)
        self.assertEqual(self.store.rename_conversation("web:rename", "😀" * 80)["title"], "😀" * 80)
        literal = "<script>alert('name')</script>"
        self.assertEqual(self.store.rename_conversation("web:rename", literal)["title"], literal)
        with self.assertRaises(ValueError):
            self.store.rename_conversation("web:unknown", "not created")
        self.assertIsNone(self.store.get_conversation("web:unknown"))
        self.store.delete_conversation("web:rename")
        with self.assertRaises(ValueError):
            self.store.rename_conversation("web:rename", "not restored")
        self.assertIsNone(self.store.get_conversation("web:rename"))
        self.assertNotIn(literal, self.store.audit_path.read_text())
        self.assertEqual(self.events(), [])

    def test_rename_migration_preserves_old_metadata_and_auto_title_behavior(self):
        old = self.store.create_conversation("web:legacy")
        archived = self.store.create_conversation("web:archived")
        archived = self.store.set_conversation_archived("web:archived", True)
        before = self.events()
        self.store.close()
        with closing(sqlite3.connect(self.directory / "memory.sqlite3")) as db, db:
            db.execute("ALTER TABLE conversations RENAME TO old_conversations")
            db.execute("""CREATE TABLE conversations (
                session_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0 CHECK(message_count >= 0))""")
            columns = "session_id, title, archived, created_at, updated_at, message_count"
            db.execute(f"INSERT INTO conversations({columns}) SELECT {columns} FROM old_conversations")
            db.execute("DROP TABLE old_conversations")
        self.store = MemoryStore(self.directory)
        self.assertEqual(self.store.get_conversation("web:legacy"), old)
        self.assertEqual(self.store.get_conversation("web:archived"), archived)
        self.assertEqual(self.events(), before)
        self.store.append_message("web:legacy", Message("user", "自动标题仍可使用"))
        self.assertEqual(self.store.get_conversation("web:legacy")["title"], "自动标题仍可使用")
        self.store.rename_conversation("web:archived", "新会话")
        self.restart()
        self.store.append_message("web:archived", Message("user", "不得覆盖"))
        self.assertEqual(self.store.get_conversation("web:archived")["title"], "新会话")

    def test_rename_outbox_failure_rolls_back_and_mirror_failure_recovers(self):
        original = self.store.create_conversation("web:atomic-rename")
        with patch.object(self.store, "_event", side_effect=RuntimeError("outbox failed")):
            with self.assertRaises(RuntimeError):
                self.store.rename_conversation("web:atomic-rename", "renamed")
        self.assertEqual(self.store.get_conversation("web:atomic-rename"), original)
        self.assertEqual(self.store._db.execute("SELECT title_custom FROM conversations").fetchone()[0], 0)
        with patch.object(Path, "open", side_effect=OSError("mirror unavailable")):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                renamed = self.store.rename_conversation("web:atomic-rename", "saved")
        self.assertTrue(caught)
        self.store._db.close()
        self.store._closed = True
        self.store = MemoryStore(self.directory)
        self.assertEqual(self.store.get_conversation("web:atomic-rename"), renamed)
        self.assertEqual(self.events()[-1]["type"], "conversation.renamed")
        self.assertEqual(self.events()[-1]["conversation"], renamed)

    def test_concurrent_rename_and_append_preserve_custom_title_and_all_messages(self):
        self.store.create_conversation("web:concurrent-rename")
        other = MemoryStore(self.directory)
        self.addCleanup(other.close)
        barrier = Barrier(2)

        def append():
            barrier.wait(timeout=5)
            for index in range(20):
                other.append_message("web:concurrent-rename", Message("user", f"question {index}"))

        def rename():
            barrier.wait(timeout=5)
            return self.store.rename_conversation("web:concurrent-rename", "新会话")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(append), executor.submit(rename)]
            for future in futures:
                future.result(timeout=10)
        self.restart()
        row = self.store.get_conversation("web:concurrent-rename")
        self.assertEqual(row["title"], "新会话")
        self.assertEqual(row["message_count"], 20)
        self.assertEqual(len(self.store.history("web:concurrent-rename", limit=100)), 20)
        self.assertEqual(len(self.events()), 22)


if __name__ == "__main__":
    unittest.main()
