"""Relay authenticated loopback HTTP to one fixed private Unix socket.

The bridge runs inside a private network or outside a shared-network workload.
The standalone helper still starts its child inside the filesystem namespace.
It never knows a model credential or an upstream hostname.
"""
from __future__ import annotations

import http.client
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import subprocess
import sys
import threading


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=125)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class BridgeServer(ThreadingHTTPServer):
    """Bound local clients, including those stalled in HTTP header parsing."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(4)
        self.connection_lock = threading.Lock()
        self.connections = set()
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self.connection_lock:
            self.connections.add(request)
        try:
            super().process_request(request, address)
        except BaseException:
            with self.connection_lock:
                self.connections.discard(request)
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self.connection_lock:
                self.connections.discard(request)
            self.slots.release()

    def close_connections(self):
        with self.connection_lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def handle_error(self, request, address):
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        connection = None
        try:
            authorization = self.headers.get_all("Authorization", [])
            expected = getattr(self.server, "token", None)
            if (not isinstance(expected, str) or not expected or len(authorization) != 1
                    or not hmac.compare_digest(authorization[0], "Bearer " + expected)):
                raise PermissionError("invalid relay credential")
            lease = getattr(self.server, "lease", False)
            if lease is None:
                raise ValueError("Agent is idle")
            lengths = self.headers.get_all("Content-Length", [])
            if (self.path not in {"/v1/chat/completions", "/v1/responses"}
                    or len(lengths) != 1 or self.headers.get("Transfer-Encoding")):
                raise ValueError("invalid route")
            length = int(lengths[0])
            if not 0 < length <= 1024 * 1024:
                raise ValueError("request too large")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete request")
            connection = UnixHTTPConnection(self.server.socket_path)
            headers = {"Content-Type": "application/json"}
            if lease is not False:
                headers["X-JoyAgent-Lease"] = lease
            connection.request("POST", self.path, raw, headers)
            response = connection.getresponse()
            output = response.read(8 * 1024 * 1024 + 1)
            if len(output) > 8 * 1024 * 1024:
                raise ValueError("response too large")
            status, kind = response.status, response.getheader("Content-Type", "application/json")
        except PermissionError:
            status, kind, output = 401, "application/json", b'{"error":{"message":"Agent gateway unavailable"}}'
        except Exception:
            status, kind, output = 400, "application/json", b'{"error":{"message":"Agent gateway unavailable"}}'
        finally:
            if connection:
                connection.close()
        try:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(output)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(output)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


class LoopbackBridge:
    """An authenticated bridge on an atomically allocated host loopback port."""

    def __init__(self, socket_path, token, *, resident=False):
        self.server = BridgeServer(("127.0.0.1", 0), Handler)
        self.server.socket_path, self.server.token = str(socket_path), token
        self.server.lease = None if resident else False
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .05}, daemon=True)
        self.closed = False
        try:
            self.thread.start()
        except BaseException:
            self.closed = True
            self.server.server_close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.server.lease = None
        self.server.shutdown()
        self.server.close_connections()
        self.server.server_close()
        self.thread.join(timeout=2)


def limit_initial_cpus() -> None:
    # Bun sizes worker pools from sched_getaffinity. The host may expose hundreds
    # of CPUs, so start this disposable job on two of its already allowed CPUs.
    # This is a startup sizing aid; the independent process-tree guard remains
    # responsible for enforcing the thread/RSS limits.
    if hasattr(os, "sched_getaffinity"):
        allowed = sorted(os.sched_getaffinity(0))
        if not allowed:
            raise RuntimeError("No CPU is available for this Agent")
        os.sched_setaffinity(0, allowed[:2])


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as source:
        plan = json.load(source)
    limit_initial_cpus()
    server = thread = None
    if not plan.get("external_bridge", False):
        server = BridgeServer(("127.0.0.1", plan["port"]), Handler)
        server.socket_path, server.token = plan["socket"], plan["env"]["JOYAGENT_BROKER_TOKEN"]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
    child = None
    try:
        child = subprocess.Popen(plan["argv"], env={**os.environ, **plan["env"]},
                                 cwd="/workspace", stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
        code = child.wait()
        # Preserve conventional signal exit status instead of Python's -signal
        # becoming an unrelated unsigned exit code at the outer process boundary.
        return code if code >= 0 else 128 - code
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()
        if server is not None:
            server.shutdown()
            server.close_connections()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
