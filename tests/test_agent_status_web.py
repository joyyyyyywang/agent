"""Agent status is authenticated, tenant-local and observational during turns."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from joyagent.channels.web import AgentHTTPServer, RuntimeLoop
from joyagent.channels.web_sessions import VisitorStore
from joyagent.channels.web_tenants import SharedRuntimeLoop
from joyagent.config import Config
from joyagent.types import ModelReply


ORIGIN = "https://agent-status.example.invalid"
ROUTE = "/api/agents/status"
PUBLIC = {"scope", "conversation_count", "archived_conversation_count", "resident_session_count",
          "running_agent_count", "agents"}
ROW_PUBLIC = {"id", "label", "status", "pid", "manager_pid", "session_count", "resident_session_count", "reason"}


class FakeProvider:
    def __init__(self):
        self.calls = 0
        self.block = False
        self.entered = threading.Event()
        self.release = None

    async def complete(self, messages, tools):
        self.calls += 1
        if self.block:
            self.release = asyncio.Event()
            self.entered.set()
            await self.release.wait()
        return ModelReply("fake reply")


class AgentStatusWebTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.provider = FakeProvider()
        self.enterContext(patch("joyagent.runtime.create_provider", return_value=self.provider))

    @contextmanager
    def service(self, name, shared=True, max_runtimes=32):
        root = self.root / name
        root.mkdir()
        (root / "SOUL.md").write_text("Use actual results.")
        (root / "policy.toml").write_text('[tools]\nallow=["time_now"]\n')
        config = Config(root / "agent.toml", {
            "agent": {"workspace": "workspace", "data_dir": "data"},
            "model": {"provider": "demo", "model": "demo"},
            "decision": {"enabled": False}, "routing": {"enabled": False},
            "web": {"mode": "shared" if shared else "local", "public_origin": ORIGIN},
        })
        bridge = SharedRuntimeLoop(config, max_runtimes=max_runtimes) if shared else RuntimeLoop(config)
        sessions = VisitorStore(root / "sessions") if shared else None
        server = AgentHTTPServer(("127.0.0.1", 0), bridge, sessions=sessions)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        service = {"root": root, "bridge": bridge, "server": server, "shared": shared,
                   "origin": ORIGIN if shared else f"http://127.0.0.1:{server.server_port}"}
        try:
            yield service
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            bridge.close()
            if sessions:
                sessions.close()

    def request(self, service, path=ROUTE, browser=None, *, method="GET", body=None, headers=None):
        values = {"Host": urlsplit(service["origin"]).netloc, "Origin": service["origin"],
                  "Content-Type": "application/json"}
        values.update(browser or {})
        values.update(headers or {})
        values = {key: value for key, value in values.items() if value is not None}
        connection = http.client.HTTPConnection("127.0.0.1", service["server"].server_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=values)
            response = connection.getresponse()
            raw = response.read().decode()
            result = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, dict(response.headers), result
        finally:
            connection.close()

    def browser(self, service):
        status, headers, page = self.request(service, "/")
        self.assertEqual(status, 200)
        result = {"X-CSRF-Token": re.search(r'name="csrf-token" content="([^"]+)"', page)[1]}
        if service["shared"]:
            result["Cookie"] = headers["Set-Cookie"].split(";", 1)[0]
        return result

    def create(self, service, browser):
        status, _, body = self.request(service, "/api/conversations", browser, method="POST", body={})
        self.assertEqual(status, 201)
        return body["conversation"]["session_id"]

    def test_local_and_unused_shared_status_starts_no_model_native_or_mcp_work(self):
        for shared in (False, True):
            with self.subTest(shared=shared), self.service(str(shared), shared) as service:
                browser = self.browser(service)
                with patch("joyagent.runtime.Runtime._get_resident_manager") as resident, \
                        patch("joyagent.channels.web_tenants.build_runtime") as build, \
                        patch("joyagent.runtime.MCPClient") as mcp:
                    status, headers, result = self.request(service, browser=browser)
                self.assertEqual(status, 200, result)
                self.assertEqual(set(result), PUBLIC)
                self.assertEqual(result["scope"], "visitor" if shared else "local")
                self.assertEqual(result["conversation_count"], 0)
                self.assertEqual(result["running_agent_count"], 1)
                self.assertEqual(result["resident_session_count"], 0)
                self.assertEqual(set(result["agents"][0]), ROW_PUBLIC)
                self.assertEqual(result["agents"][0]["pid"], os.getpid())
                self.assertEqual(result["agents"][0]["status"], "ready")
                self.assertEqual(headers["Cache-Control"], "no-store")
                resident.assert_not_called()
                build.assert_not_called()
                mcp.assert_not_called()
                if shared:
                    self.assertFalse(service["bridge"]._runtimes)
                    self.assertEqual(list(service["bridge"].users_root.iterdir()), [])
        self.assertEqual(self.provider.calls, 0)

    def test_shared_status_requires_cookie_csrf_and_same_origin_without_allocating_identity(self):
        with self.service("auth") as service:
            browser = self.browser(service)
            cases = [({}, 401), ({"Cookie": "__Host-joyagent_session=forged"}, 401),
                     ({"Cookie": browser["Cookie"]}, 403), ({**browser, "X-CSRF-Token": "wrong"}, 403)]
            for credentials, expected in cases:
                with self.subTest(credentials=credentials):
                    status, headers, _ = self.request(service, browser=credentials)
                    self.assertEqual(status, expected)
                    self.assertNotIn("Set-Cookie", headers)
            for headers in ({"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}):
                self.assertEqual(self.request(service, browser=browser, headers=headers)[0], 403)
            self.assertFalse(service["bridge"]._runtimes)

    def test_local_status_requires_csrf_and_rejects_selectors_and_post(self):
        for shared in (False, True):
            with self.subTest(shared=shared), self.service("query-" + str(shared), shared) as service:
                browser = self.browser(service)
                if not shared:
                    self.assertEqual(self.request(service)[0], 403)
                for suffix in ("?user_id=other", "?workspace=/other", "?pid=1", "?session_id=other", "?a=1&a=2"):
                    status, _, body = self.request(service, ROUTE + suffix, browser)
                    self.assertEqual(status, 400)
                    self.assertNotIn("other", str(body))
                self.assertEqual(self.request(service, browser=browser, method="POST", body={})[0], 404)
                self.assertEqual(self.provider.calls, 0)

    def test_shared_counts_follow_only_own_conversations_archive_and_delete(self):
        with self.service("counts") as service:
            alice, bob = self.browser(service), self.browser(service)
            first, second = self.create(service, alice), self.create(service, alice)
            self.create(service, bob)
            self.request(service, "/api/conversations/archive", alice, method="POST",
                         body={"session_id": first, "archived": True})
            a = self.request(service, browser=alice)[2]
            b = self.request(service, browser=bob)[2]
            self.assertEqual((a["conversation_count"], a["archived_conversation_count"]), (2, 1))
            self.assertEqual((b["conversation_count"], b["archived_conversation_count"]), (1, 0))
            self.assertNotIn(first, json.dumps(a))
            self.assertNotIn(second, json.dumps(a))
            status, _, _ = self.request(service, "/api/conversations/delete", alice, method="POST",
                                         body={"session_id": first, "confirm": True})
            self.assertEqual(status, 200)
            a = self.request(service, browser=alice)[2]
            self.assertEqual((a["conversation_count"], a["archived_conversation_count"]), (1, 0))
            self.assertEqual(self.request(service, browser=bob)[2]["conversation_count"], 1)

    def test_status_poll_does_not_change_runtime_lru_or_recreate_evicted_runtime(self):
        with self.service("lru", max_runtimes=2) as service:
            bridge = service["bridge"]
            alice, bob, carol = self.browser(service), self.browser(service), self.browser(service)
            self.create(service, alice)
            self.create(service, bob)
            before = list(bridge._runtimes)
            self.assertEqual(self.request(service, browser=alice)[2]["conversation_count"], 1)
            self.assertEqual(list(bridge._runtimes), before)
            self.assertFalse(bridge._leases)
            self.create(service, carol)
            self.assertNotIn(before[0], bridge._runtimes)
            after = list(bridge._runtimes)
            with patch("joyagent.channels.web_tenants.build_runtime") as build, \
                    patch("joyagent.runtime.MCPClient") as mcp:
                self.assertEqual(self.request(service, browser=alice)[2]["conversation_count"], 1)
            self.assertEqual(list(bridge._runtimes), after)
            build.assert_not_called()
            mcp.assert_not_called()
            self.assertFalse(bridge._leases)

    def test_status_is_available_during_a_model_turn_and_busy_resets_after_completion(self):
        for shared in (False, True):
            with self.subTest(shared=shared), self.service("busy-" + str(shared), shared) as service:
                browser = self.browser(service)
                session = self.create(service, browser)
                self.provider.block = True
                self.provider.entered.clear()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.request, service, "/api/chat", browser, method="POST",
                                         body={"session_id": session, "message": "fake blocking turn"})
                    try:
                        self.assertTrue(self.provider.entered.wait(2))
                        status, _, data = self.request(service, browser=browser)
                        self.assertEqual(status, 200)
                        self.assertEqual(data["agents"][0]["status"], "busy")
                    finally:
                        if self.provider.release:
                            service["bridge"].loop.call_soon_threadsafe(self.provider.release.set)
                    self.assertEqual(future.result(timeout=3)[0], 200)
                data = self.request(service, browser=browser)[2]
                self.assertEqual(data["agents"][0]["status"], "ready")
                self.assertEqual(data["agents"][0]["session_count"], 1)
                self.provider.block = False

    def test_runtime_failures_are_generic_in_both_modes(self):
        for shared in (False, True):
            with self.subTest(shared=shared), self.service("error-" + str(shared), shared) as service:
                browser = self.browser(service)
                with patch("joyagent.runtime.Runtime.agent_status", side_effect=ValueError("private-token-and-path")):
                    self.create(service, browser)
                    status, _, body = self.request(service, browser=browser)
                self.assertEqual(status, 500)
                self.assertEqual(body, {"error": "读取 Agent 运行状态失败，请稍后重试"})

    def test_inactive_tenant_symlink_and_sqlite_sidecars_are_rejected_without_opening(self):
        for kind in ("tenant", "wal", "shm"):
            with self.subTest(kind=kind), self.service("unsafe-" + kind, max_runtimes=1) as service:
                bridge = service["bridge"]
                alice, bob = self.browser(service), self.browser(service)
                self.create(service, alice)
                user_id = next(iter(bridge._runtimes))
                self.create(service, bob)
                tenant = bridge.users_root / user_id
                if kind == "tenant":
                    moved = tenant.with_name(user_id + "-moved")
                    tenant.rename(moved)
                    tenant.symlink_to(moved, target_is_directory=True)
                else:
                    path = tenant / "data" / ("memory.sqlite3-" + kind)
                    path.unlink(missing_ok=True)
                    path.symlink_to(tenant / "data" / "memory.sqlite3")
                with patch("joyagent.channels.web_tenants.MemoryStore.read_conversation_stats") as read:
                    self.assertEqual(self.request(service, browser=alice)[0], 500)
                    read.assert_not_called()
