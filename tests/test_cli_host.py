"""The namespace's loopback bridge talks only to its fixed private Unix relay."""

import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from joyagent import cli_host
from joyagent.model_broker import ModelBroker
from joyagent.providers import HTTPProvider


class CliHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jya-host-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.socket_path = self.root / "relay.sock"
        provider = HTTPProvider({"provider": "openai", "model": "owner-model",
                                 "base_url": "https://owner.invalid/v1", "max_tokens": 64})
        provider.credential = Mock(return_value=("fake-private-owner-key", "test"))
        self.transport = Mock(return_value={
            "id": "fake", "model": "owner-model", "choices": [{
                "message": {"role": "assistant", "content": "fake bridge response"},
                "finish_reason": "stop", "index": 0,
            }], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
        self.broker = ModelBroker(self.socket_path, provider, max_requests=8,
                                  max_tool_calls=8, timeout=30, transport=self.transport)
        self.addCleanup(self.broker.close)
        self.server = None
        self.token = "a" * 64

    def start_bridge(self):
        self.server = cli_host.BridgeServer(("127.0.0.1", 0), cli_host.Handler)
        self.server.socket_path, self.server.token = str(self.socket_path), self.token
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

        def close():
            self.server.shutdown()
            self.server.close_connections()
            self.server.server_close()
            self.thread.join(timeout=2)

        self.addCleanup(close)
        return self.server.server_address[1]

    def post(self, body=None, path="/v1/chat/completions", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        try:
            connection.request("POST", path,
                               json.dumps(body or {"messages": [{"role": "user", "content": "fake task"}]}),
                               {"Content-Type": "application/json", "Authorization": "Bearer " + self.token, **(headers or {})})
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def raw_post(self, headers, body):
        with socket.create_connection(self.server.server_address, timeout=3) as connection:
            connection.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n"
                               + b"Authorization: Bearer " + self.token.encode() + b"\r\n"
                               + headers + b"Connection: close\r\n\r\n" + body)
            connection.shutdown(socket.SHUT_WR)
            output = []
            while block := connection.recv(65536):
                output.append(block)
        return b"".join(output)

    def test_loopback_forwards_chat_to_fixed_unix_socket_and_drops_client_auth(self):
        self.start_bridge()
        status, kind, raw = self.post({"model": "client-model", "stream": True,
                                      "messages": [{"role": "user", "content": "fake task"}]},
                                     headers={"Authorization": "Bearer " + self.token,
                                              "X-Endpoint": "http://169.254.169.254"})
        self.assertEqual((status, kind), (200, "text/event-stream"))
        self.assertIn(b"fake bridge response", raw)
        self.assertTrue(raw.endswith(b"data: [DONE]\n\n"))
        self.assertNotIn(b"fake-private-owner-key", raw)
        url, body, headers, _ = self.transport.call_args.args
        self.assertEqual(url, "https://owner.invalid/v1/chat/completions")
        self.assertEqual(body["model"], "owner-model")
        self.assertEqual(headers, {"Authorization": "Bearer fake-private-owner-key"})

    def test_responses_endpoint_uses_same_private_broker(self):
        self.start_bridge()
        status, kind, raw = self.post({"model": "client-model", "input": "fake task", "stream": False},
                                     path="/v1/responses")
        self.assertEqual((status, kind), (200, "application/json"))
        result = json.loads(raw)
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["output"][0]["content"][0]["text"], "fake bridge response")
        self.assertEqual(self.broker.calls, 1)

    def test_wrong_missing_and_duplicated_credentials_do_not_reach_model(self):
        self.start_bridge()
        for authorization in ("", "Bearer " + "b" * 64, "Basic fake", "Bearer joyagent-local-broker"):
            with self.subTest(authorization=authorization):
                self.assertEqual(self.post(headers={"Authorization": authorization})[0], 401)
        raw = self.raw_post(b"Authorization: Bearer " + self.token.encode() + b"\r\nContent-Length: 2\r\n", b"{}")
        self.assertIn(b" 401 ", raw.split(b"\r\n", 1)[0])
        self.transport.assert_not_called()

    def test_host_bridges_coexist_and_tokens_cannot_access_sibling_listener(self):
        first = cli_host.LoopbackBridge(self.socket_path, self.token)
        second = cli_host.LoopbackBridge(self.socket_path, "b" * 64)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertNotEqual(first.endpoint, second.endpoint)
        self.server = first.server
        self.assertEqual(self.post()[0], 200)
        self.server = second.server
        before = self.broker.calls
        self.assertEqual(self.post()[0], 401)
        self.assertEqual(self.broker.calls, before)
        self.assertEqual(self.post(headers={"Authorization": "Bearer " + "b" * 64})[0], 200)
        address = first.server.server_address
        first.close()
        first.close()
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=.5)

    def test_resident_host_bridge_rejects_idle_and_old_turn_leases(self):
        bridge = cli_host.LoopbackBridge(self.socket_path, self.token, resident=True)
        self.addCleanup(bridge.close)
        self.server = bridge.server
        self.broker.lease_required = True
        self.broker.deactivate()
        self.assertEqual(self.post()[0], 400)
        first = self.broker.activate(self.broker.provider, max_requests=8, max_tool_calls=8, timeout=30)
        self.server.lease = first
        self.assertEqual(self.post()[0], 200)
        self.broker.deactivate()
        self.broker.activate(self.broker.provider, max_requests=8, max_tool_calls=8, timeout=30)
        before = self.transport.call_count
        self.assertEqual(self.post()[0], 400)
        self.assertEqual(self.transport.call_count, before)

    def test_arbitrary_routes_cannot_turn_bridge_into_http_proxy(self):
        self.start_bridge()
        for path in ("/v1/models", "/v1/chat/completions?url=other", "https://attacker.invalid/v1/responses",
                     "http://169.254.169.254/latest/meta-data", "/v1/responses/../chat/completions"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path=path)[0], 400)
        self.transport.assert_not_called()

    def test_body_length_framing_is_checked_before_unix_connection(self):
        self.start_bridge()
        valid = b'{"messages":[{"role":"user","content":"fake"}]}'
        for headers, body in (
            (b"Content-Length: 2\r\nContent-Length: 2\r\n", b"{}"),
            (b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n", b"{}"),
            (b"Content-Length: 1048577\r\n", b""),
            (b"Content-Length: 0\r\n", b""),
            (f"Content-Length: {len(valid) + 10}\r\n".encode(), valid),
        ):
            with self.subTest(headers=headers):
                raw = self.raw_post(headers, body)
                self.assertIn(b" 400 ", raw.split(b"\r\n", 1)[0])
                self.assertIn(b"Agent gateway unavailable", raw)
        self.transport.assert_not_called()

    def test_unavailable_unix_broker_is_generic_and_does_not_expose_host_path(self):
        self.start_bridge()
        self.server.socket_path = str(self.root / "private-credential-path.sock")
        status, _, raw = self.post()
        self.assertEqual(status, 400)
        self.assertNotIn(b"private-credential-path", raw)
        self.assertNotIn(str(self.root).encode(), raw)
        self.assertEqual(json.loads(raw)["error"]["message"], "Agent gateway unavailable")
        self.transport.assert_not_called()

    def test_local_connections_are_bounded_before_http_headers_complete(self):
        self.start_bridge()
        clients = []
        try:
            for _ in range(4):
                client = socket.create_connection(self.server.server_address, timeout=1)
                client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n")
                clients.append(client)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with self.server.connection_lock:
                    if len(self.server.connections) == 4:
                        break
                time.sleep(.005)
            else:
                self.fail("bridge did not accept the partial local clients")
            with self.assertRaises((OSError, http.client.HTTPException)):
                self.post()
            self.transport.assert_not_called()
            self.server.close_connections()
            for client in clients:
                try:
                    self.assertEqual(client.recv(100), b"")
                except ConnectionResetError:
                    pass
        finally:
            for client in clients:
                client.close()

    def test_oversize_broker_response_is_refused(self):
        self.start_bridge()
        response = Mock()
        response.read.return_value = b"x" * (8 * 1024 * 1024 + 1)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(cli_host, "UnixHTTPConnection", return_value=connection):
            status, _, raw = self.post()
        self.assertEqual(status, 400)
        self.assertEqual(len(raw), len(b'{"error":{"message":"Agent gateway unavailable"}}'))
        response.read.assert_called_once_with(8 * 1024 * 1024 + 1)
        connection.close.assert_called_once()

    def test_main_runs_real_fake_cli_through_bridge_and_preserves_exit_status(self):
        # Only map the namespace's fixed /workspace cwd to a temporary local
        # directory; the child, HTTP bridge and Unix broker are real processes
        # and sockets. No sandbox or model API is invoked by this test.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        child_script = self.root / "fake_cli.py"
        child_script.write_text(
            "import http.client,json,os,sys\n"
            "assert sys.stdin.read() == 'fake user task'\n"
            "assert os.environ['JOYAGENT_TEST_CHILD'] == 'present'\n"
            f"c=http.client.HTTPConnection('127.0.0.1',{port},timeout=3)\n"
            "c.request('POST','/v1/chat/completions',json.dumps({'messages':[{'role':'user','content':'fake task'}]}),"
            "{'Content-Type':'application/json','Authorization':'Bearer '+os.environ['JOYAGENT_BROKER_TOKEN']})\n"
            "r=c.getresponse(); assert r.status == 200\n"
            "print(json.loads(r.read())['choices'][0]['message']['content'])\n"
            "c.close(); sys.exit(7)\n",
            encoding="utf-8",
        )
        argv = [sys.executable, str(child_script)]
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps({"port": port, "socket": str(self.socket_path),
                                        "argv": argv, "env": {"JOYAGENT_TEST_CHILD": "present", "JOYAGENT_BROKER_TOKEN": self.token}}))
        real_popen = subprocess.Popen

        def spawn(command, **kwargs):
            self.assertEqual(command, argv)
            self.assertEqual(kwargs.pop("cwd"), "/workspace")
            return real_popen(command, cwd=self.root, **kwargs)

        with tempfile.TemporaryFile(mode="w+") as input_file, \
                tempfile.TemporaryFile(mode="w+") as output_file, \
                tempfile.TemporaryFile(mode="w+") as error_file:
            input_file.write("fake user task")
            input_file.seek(0)
            with patch.object(sys, "argv", ["cli_host.py", str(plan_path)]), \
                    patch.object(cli_host, "limit_initial_cpus"), \
                    patch.object(sys, "stdin", input_file), patch.object(sys, "stdout", output_file), \
                    patch.object(sys, "stderr", error_file), patch.object(cli_host.subprocess, "Popen", spawn):
                result = cli_host.main()
            output_file.seek(0)
            self.assertEqual(output_file.read(), "fake bridge response\n")
            error_file.seek(0)
            self.assertEqual(error_file.read(), "")
        self.assertEqual(result, 7)
        self.assertEqual(self.broker.calls, 1)
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=.5):
                pass

    def test_cpu_sizing_only_selects_cpus_already_allowed_to_current_job(self):
        with patch.object(cli_host.os, "sched_getaffinity", return_value={9, 3, 7}, create=True), \
                patch.object(cli_host.os, "sched_setaffinity", create=True) as assign:
            cli_host.limit_initial_cpus()
        assign.assert_called_once_with(0, [3, 7])
