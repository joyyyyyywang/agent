"""Private Telegram conversations with an explicit numeric user allowlist."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from typing import Any

from .common import MAX_MESSAGE, read_state, wait_or_stop, write_state


class TelegramChannel:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        config = runtime.config
        raw_ids = config.get("telegram", "allowed_user_ids", [])
        if not isinstance(raw_ids, list) or not raw_ids or any(type(item) is not int or item <= 0 for item in raw_ids):
            raise ValueError("[telegram].allowed_user_ids 必须配置非空的正整数用户 ID 列表")
        self.allowed_user_ids = set(raw_ids)
        token_env = config.get("telegram", "token_env", "TELEGRAM_BOT_TOKEN")
        self.token = os.environ.get(token_env, "").strip()
        if not self.token or any(character.isspace() for character in self.token) or "/" in self.token:
            raise ValueError(f"请设置 Telegram token 环境变量 {token_env}")
        self.poll_timeout = int(config.get("telegram", "poll_timeout", 20))
        if not 1 <= self.poll_timeout <= 50:
            raise ValueError("[telegram].poll_timeout 必须在 1–50 秒之间")
        self.state_path = config.data_dir / "telegram-cursor.json"
        self.state = read_state(self.state_path)
        self.offset = int(self.state.get("offset", 0))

    async def _persist(self, state: dict[str, Any]) -> None:
        await asyncio.to_thread(write_state, self.state_path, state)
        self.state = state

    def _request(self, method: str, data: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        # Never include the token-bearing URL in errors or logs.
        try:
            with urllib.request.urlopen(request, timeout=self.poll_timeout + 10) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("Telegram 响应过大")
            result = json.loads(raw)
            if not isinstance(result, dict) or not result.get("ok"):
                raise ValueError("Telegram API 返回失败状态")
            return result["result"]
        except Exception as exc:
            raise RuntimeError(f"Telegram API 请求失败（{type(exc).__name__}）") from None

    async def handle_update(self, update: dict[str, Any]) -> bool:
        """Return False for ignored updates; only allowed private text is run."""
        message = update.get("message", {})
        if not isinstance(message, dict):
            return False
        sender, chat = message.get("from", {}), message.get("chat", {})
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return False
        user_id, chat_id = sender.get("id"), chat.get("id")
        if (type(user_id) is not int or type(chat_id) is not int or user_id not in self.allowed_user_ids
                or chat.get("type") != "private" or chat_id != user_id or sender.get("is_bot")):
            return False
        text = message.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE:
            return False
        update_id = update.get("update_id")
        if type(update_id) is not int:
            return False
        pending = self.state.get("pending")
        if pending is not None:
            if pending.get("update_id") != update_id or pending.get("chat_id") != chat_id:
                raise RuntimeError("Telegram 存在其他尚未提交的回复，请检查本地游标")
        else:
            result = await self.runtime.chat(text, session_id=f"telegram:{user_id}:{chat_id}")
            pending = {"update_id": update_id, "chat_id": chat_id,
                       "text": (result.text or "（本轮没有文本回复）")[:100_000], "next_chunk": 0}
            # Persist the prepared reply BEFORE contacting Telegram. A send
            # retry must not rerun the model and repeat completed tool effects.
            await self._persist({**self.state, "offset": self.offset, "pending": pending})
        answer = pending["text"]
        # Telegram counts UTF-16 characters; 1800 Python characters are safe
        # even if every character is a supplementary Unicode code point.
        for index in range(pending["next_chunk"], len(answer), 1800):
            await asyncio.to_thread(self._request, "sendMessage", {"chat_id": chat_id, "text": answer[index:index + 1800]})
            pending = {**pending, "next_chunk": index + 1800}
            await self._persist({**self.state, "pending": pending})
        return True

    async def poll_once(self) -> None:
        updates = await asyncio.to_thread(self._request, "getUpdates", {
            "offset": self.offset, "timeout": self.poll_timeout,
            "allowed_updates": ["message"], "limit": 50,
        })
        if not isinstance(updates, list):
            raise ValueError("Telegram 更新格式错误")
        for update in updates:
            if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                continue
            next_offset = update["update_id"] + 1
            if next_offset <= self.offset:
                continue
            await self.handle_update(update)
            # A failed model request or reply leaves the cursor unchanged.
            await self._persist({"offset": next_offset})
            self.offset = next_offset

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        print("Telegram 通道已启动，仅处理允许用户的私聊。Ctrl+C 停止。", flush=True)
        while not stop.is_set():
            try:
                await self.poll_once()
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"Telegram 本轮失败，将重试：{exc}", file=sys.stderr, flush=True)
                await wait_or_stop(stop, 3)
