"""Isolated service for many native conversations in one Agent process."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import threading
from types import SimpleNamespace

from cli_host import BridgeServer, Handler, limit_initial_cpus
from native_transport import JsonLines


class NativeFailure(RuntimeError):
    """Only static categories and an optional numeric native exit status cross IPC."""
    def __init__(self, code, exit_code=None):
        self.code = code if code in {"native_exit", "output_limit", "protocol_error"} else "protocol_error"
        self.exit_code = (exit_code if self.code == "native_exit" and type(exit_code) is int
                          and -255 <= exit_code <= 255 else None)
        super().__init__("Native Agent is unavailable")


def fatal_record(error):
    failure = error if isinstance(error, NativeFailure) else NativeFailure("protocol_error")
    record = {"type": "fatal", "code": failure.code}
    if failure.exit_code is not None:
        record["exit_code"] = failure.exit_code
    return record


def emit(record):
    raw = json.dumps(record, ensure_ascii=False, allow_nan=False)
    if len(raw.encode()) > 1024 * 1024:
        raise ValueError("Agent result exceeds limit")
    print(raw, flush=True)


async def run(plan, server):
    if plan.get("backend") not in {"codex", "pi", "deepseek"}:
        raise ValueError("Unsupported native Agent")
    incoming_transport = None
    process = None
    tasks = []
    current = None
    volume = 0
    sessions = {}

    def publish(event):
        if current is None:
            raise RuntimeError("Unexpected native action outside a turn")
        emit({"type": "tool", "id": current[0], "session": current[1], "event": event})

    async def discard(reader):
        nonlocal volume
        while chunk := await reader.read(8192):
            volume += len(chunk)
            if volume > 8 * 1024 * 1024:
                raise NativeFailure("output_limit")
        # An exited child must not be mistaken for an idle resident process.
        raise NativeFailure("native_exit", process.returncode)

    def guard_failure():
        # Prefer the specific limit failure to the EOF caused by its cleanup.
        for task in (errors, reader):
            if task.done() and not task.cancelled():
                error = task.exception()
                if isinstance(error, NativeFailure) and error.code != "native_exit":
                    return error
        if exited.done() or process.returncode is not None:
            return NativeFailure("native_exit", process.returncode)
        for task in (errors, reader):
            if task.done() and not task.cancelled() and isinstance(task.exception(), NativeFailure):
                return task.exception()
        return NativeFailure("protocol_error")

    async def guarded(awaitable):
        job = asyncio.create_task(awaitable)
        try:
            done, _ = await asyncio.wait((job, reader, errors, exited), return_when=asyncio.FIRST_COMPLETED)
            if exited in done or reader in done or errors in done:
                raise guard_failure()
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        # Cleanup yields once more after wait's snapshot. A transport/exit
        # guard that fails there must win over an otherwise completed reply.
        if any(task.done() for task in (reader, errors, exited)):
            raise guard_failure()
        return job.result()

    try:
        incoming = asyncio.StreamReader(limit=1024 * 1024 + 1)
        incoming_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(incoming), sys.stdin.buffer)
        process = await asyncio.create_subprocess_exec(*plan["argv"], env={**os.environ, **plan["env"]},
            cwd="/workspace", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=1024 * 1024 + 1)
        errors = asyncio.create_task(discard(process.stderr))
        tasks.append(errors)
        exited = asyncio.create_task(process.wait())
        tasks.append(exited)
        rpc = JsonLines(process.stdout, process.stdin, jsonrpc=plan["backend"] != "codex")
        if plan["backend"] == "deepseek":
            from native_dsh import DSHAdapter
            def make_adapter():
                return DSHAdapter(rpc, publish, output_limit=plan["output_limit"],
                                  state_root=plan["env"]["DSH_HOME"])
        else:
            from native_codex_pi import CodexAdapter, PiAdapter
            def make_adapter():
                if plan["backend"] == "codex":
                    return CodexAdapter(rpc, publish, output_limit=plan["output_limit"],
                                        state_root=plan["env"]["CODEX_HOME"])
                return PiAdapter(rpc, publish, output_limit=plan["output_limit"],
                                 session_root=plan["env"]["JOYAGENT_PI_SESSION_DIR"])
        reader = asyncio.create_task(rpc.read())
        tasks.append(reader)
        emit({"type": "ready", "native_pid": process.pid})
        while True:
            raw = await guarded(incoming.readline())
            if not raw:
                return
            record = json.loads(raw)
            if (not isinstance(record, dict) or record.get("op") not in {"open", "turn", "drop"}
                    or not isinstance(record.get("id"), str) or not 1 <= len(record["id"]) <= 64
                    or not isinstance(record.get("session"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["session"])):
                raise ValueError("Invalid resident Agent command")
            volume = 0
            session = record["session"]
            op = record["op"]
            if op == "open":
                if record.get("model") not in plan["models"]:
                    raise ValueError("Invalid native session model")
                if session not in sessions:
                    if len(sessions) >= plan.get("session_limit", 64):
                        raise ValueError("Native session cache is full")
                    adapter = make_adapter()
                    async with asyncio.timeout(25):
                        await guarded(adapter.start(record["model"]))
                    sessions[session] = adapter
                emit({"type": op, "id": record["id"], "session": session,
                      "native_session_id": sessions[session].native_session_id})
                continue
            adapter = sessions.get(session)
            if adapter is None:
                raise ValueError("Native session is not open")
            if op == "drop":
                identity = adapter.native_session_id
                async with asyncio.timeout(25):
                    await guarded(adapter.drop())
                sessions.pop(session)
                emit({"type": op, "id": record["id"], "session": session, "native_session_id": identity})
                continue
            if (record.get("model") not in plan["models"] or not isinstance(record.get("prompt"), str)
                    or not isinstance(record.get("lease"), str) or len(record["lease"]) != 64):
                raise ValueError("Invalid native Agent turn")
            current = (record["id"], session)
            server.lease = record["lease"]
            try:
                result = await guarded(adapter.turn(record["prompt"], record["model"]))
            finally:
                server.lease = None
            emit({**result, "type": "result", "id": current[0], "session": session, "native_pid": process.pid})
            current = None
    finally:
        server.lease = None
        if incoming_transport is not None:
            incoming_transport.close()
        try:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                await process.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    with open(sys.argv[1], encoding="utf-8") as source:
        plan = json.load(source)
    limit_initial_cpus()
    server = SimpleNamespace(lease=None)
    thread = None
    if not plan.get("external_bridge", False):
        server = BridgeServer(("127.0.0.1", plan["port"]), Handler)
        server.socket_path, server.lease = plan["socket"], None
        server.token = plan["env"]["JOYAGENT_BROKER_TOKEN"]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
    try:
        asyncio.run(run(plan, server))
        return 0
    except BaseException as error:
        with contextlib.suppress(Exception):
            emit(fatal_record(error))
        return 1
    finally:
        if thread is not None:
            server.shutdown()
            server.close_connections()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
