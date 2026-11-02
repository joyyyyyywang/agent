"""Small JSON-lines transport shared by isolated native protocol adapters."""
from __future__ import annotations

import asyncio
import json


class JsonLines:
    def __init__(self, reader, writer, *, jsonrpc=True):
        self.reader, self.writer = reader, writer
        self.jsonrpc = jsonrpc
        self.pending = {}
        self.sequence = 0
        self.on_event = None
        self.write_lock = asyncio.Lock()
        self._disconnected = False

    def _check_open(self):
        if self._disconnected:
            raise RuntimeError("Native Agent protocol disconnected")

    def _disconnect(self):
        self._disconnected = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Native Agent protocol disconnected"))

    async def send(self, record):
        self._check_open()
        raw = json.dumps(record, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(raw) > 1024 * 1024:
            raise ValueError("Agent protocol request exceeds limit")
        async with self.write_lock:
            self._check_open()
            try:
                self.writer.write(raw)
                await self.writer.drain()
            except (OSError, RuntimeError):
                self._disconnect()
                raise RuntimeError("Native Agent protocol disconnected") from None

    async def exchange(self, record):
        self._check_open()
        self.sequence += 1
        identifier = "joy-" + str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send({**record, "id": identifier})
            return await future
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A failed write can disconnect before exchange awaits the
                # future. Retrieve its error so no orphan exception remains.
                future.exception()

    async def request(self, method, params):
        record = {"method": method, "params": params}
        if self.jsonrpc:
            record["jsonrpc"] = "2.0"
        reply = await self.exchange(record)
        if "error" in reply or "result" not in reply:
            raise RuntimeError("Native Agent request failed")
        return reply["result"]

    async def read(self):
        try:
            self._check_open()
            while raw := await self.reader.readline():
                if len(raw) > 1024 * 1024:
                    raise ValueError("Native Agent response exceeds limit")
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("Invalid native Agent response")
                identifier = record.get("id")
                future = self.pending.get(identifier) if isinstance(identifier, (str, int)) else None
                if future is not None and "method" not in record:
                    if not future.done():
                        future.set_result(record)
                elif self.on_event is not None:
                    await self.on_event(record)
            raise RuntimeError("Native Agent closed its protocol stream")
        finally:
            self._disconnect()
