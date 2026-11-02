"""Exact dashboard counts from temporary SQLite; no models or remote APIs."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from joyagent.memory import MemoryStore
from joyagent.types import Message


class AgentStatusMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = MemoryStore(self.root / "data")
        self.addCleanup(self.memory.close)

    def test_counts_distinct_web_chats_backend_usage_archive_empty_and_delete(self):
        self.memory.create_conversation("web:empty")
        self.memory.append_messages("web:mixed", [Message("user", "PRIVATE PROMPT"),
            Message("assistant", "PRIVATE REPLY"), Message("assistant", "one", backend="codex"),
            Message("assistant", "two", backend="codex"), Message("assistant", "three", backend="pi")])
        self.memory.append_message("web:archived", Message("assistant", "four", backend="pi"))
        self.memory.set_conversation_archived("web:archived", True)
        self.memory.append_message("cli:other", Message("assistant", "five", backend="opencode"))
        stats = self.memory.conversation_stats()
        self.assertEqual(stats, {"total": 3, "archived": 1,
            "by_backend": {"joyagent": 1, "codex": 1, "pi": 2, "opencode": 0, "deepseek": 0}})
        self.assertNotIn("PRIVATE", str(stats))
        self.assertNotIn("mixed", str(stats))
        self.memory.delete_conversation("web:mixed")
        remaining = self.memory.conversation_stats()
        self.assertEqual(remaining["total"], 2)
        self.assertEqual(remaining["by_backend"]["pi"], 1)
        self.assertEqual(remaining["by_backend"]["codex"], 0)
        self.assertEqual(remaining["by_backend"]["joyagent"], 0)

    def test_prefix_is_literal_and_counts_are_not_limited_to_list_page(self):
        # Insert fixture metadata directly to avoid 1001 unrelated audit writes.
        with self.memory._db:
            self.memory._db.executemany("""INSERT INTO conversations
                (session_id,title,created_at,updated_at) VALUES (?,?,?,?)""",
                [(f"web:{i}", "empty", "t", "t") for i in range(1001)])
        self.memory.create_conversation("web_:literal")
        self.assertEqual(self.memory.conversation_stats()["total"], 1001)
        self.assertEqual(self.memory.conversation_stats(prefix="web_:")["total"], 1)
        for invalid in (None, 3, "\0", "a" * 257):
            with self.subTest(prefix=invalid), self.assertRaises(ValueError):
                self.memory.conversation_stats(prefix=invalid)

    def test_readonly_inactive_stats_include_committed_wal_without_audit_changes(self):
        self.memory.append_message("web:wal", Message("assistant", "x", backend="deepseek"))
        audit = self.memory.audit_path.read_bytes()
        before = {p.name for p in self.memory.data_dir.iterdir()}
        self.assertEqual(MemoryStore.read_conversation_stats(self.memory.data_dir),
                         self.memory.conversation_stats())
        self.assertEqual(self.memory.audit_path.read_bytes(), audit)
        self.assertEqual({p.name for p in self.memory.data_dir.iterdir()}, before)
        absent = self.root / "absent"
        self.assertEqual(MemoryStore.read_conversation_stats(absent)["total"], 0)
        self.assertFalse(absent.exists())
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(MemoryStore.read_conversation_stats(empty)["total"], 0)
        self.assertEqual(list(empty.iterdir()), [])

    def test_readonly_rejects_links_permissions_and_corrupt_database(self):
        alias = self.root / "alias"
        alias.symlink_to(self.memory.data_dir, target_is_directory=True)
        with self.assertRaises(ValueError):
            MemoryStore.read_conversation_stats(alias)
        alternate = self.root / "alternate"
        alternate.mkdir()
        target = alternate / "memory.sqlite3"
        target.symlink_to(self.memory.db_path)
        with self.assertRaises(ValueError):
            MemoryStore.read_conversation_stats(alternate)
        target.unlink()
        os.link(self.memory.db_path, target)
        with self.assertRaises(ValueError):
            MemoryStore.read_conversation_stats(alternate)
        target.unlink()
        target.write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            MemoryStore.read_conversation_stats(alternate)
        alternate.chmod(0o777)
        with self.assertRaises(ValueError):
            MemoryStore.read_conversation_stats(alternate)
        alternate.chmod(0o700)

    def test_unknown_or_malformed_backend_does_not_create_dashboard_rows(self):
        self.memory.append_message("web:known", Message("assistant", "safe"))
        with self.memory._db:
            self.memory._db.execute("INSERT INTO messages(session_id,role,payload,created_at) VALUES (?,?,?,?)",
                                   ("web:known", "assistant", '{"backend":"private-unknown"}', "t"))
            self.memory._db.execute("INSERT INTO messages(session_id,role,payload,created_at) VALUES (?,?,?,?)",
                                   ("web:known", "assistant", "broken", "t"))
        stats = self.memory.conversation_stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_backend"]["joyagent"], 1)
        self.assertNotIn("private-unknown", stats["by_backend"])
