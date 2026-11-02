"""Opt-in IMAP/SMTP channel with exact sender allowlists and durable UID cursor."""

from __future__ import annotations

import asyncio
import email
import email.policy
import hashlib
import hmac
import imaplib
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from typing import Any

from .common import MAX_MESSAGE, read_state, wait_or_stop, write_state

MAX_EMAIL_BYTES = 262_144


def exact_address(value: str) -> str:
    addresses = getaddresses([value])
    if len(addresses) != 1:
        raise ValueError("邮件地址必须恰好包含一个邮箱")
    address = addresses[0][1].lower()
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", address):
        raise ValueError("邮件地址格式不正确")
    return address


class EmailChannel:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        config = runtime.config
        if config.get("email", "enabled", False) is not True:
            raise ValueError("邮件通道默认关闭；请显式设置 [email].enabled = true")
        allowed = config.get("email", "allowed_senders", [])
        if not isinstance(allowed, list) or not allowed or any(not isinstance(value, str) for value in allowed):
            raise ValueError("请配置非空的 [email].allowed_senders 精确邮箱列表")
        self.allowed_senders = {exact_address(value) for value in allowed}
        token_env = config.get("email", "auth_token_env", "AGENT_EMAIL_TOKEN")
        if not isinstance(token_env, str):
            raise ValueError("email.auth_token_env 必须是环境变量名或显式空字符串")
        self.auth_token = os.environ.get(token_env, "") if token_env else ""
        if token_env and not self.auth_token:
            raise ValueError(f"请设置邮件鉴权环境变量 {token_env}；来信须携带 X-JoyAgent-Token 头")
        user_env = config.get("email", "user_env", "AGENT_EMAIL_USER")
        password_env = config.get("email", "password_env", "AGENT_EMAIL_PASSWORD")
        self.user = os.environ.get(user_env, "")
        self.password = os.environ.get(password_env, "")
        if not self.user or not self.password:
            raise ValueError(f"请设置邮件环境变量 {user_env} 和 {password_env}")
        self.address = exact_address(self.user)
        self.imap_host = config.get("email", "imap_host", "")
        self.smtp_host = config.get("email", "smtp_host", "")
        if not isinstance(self.imap_host, str) or not isinstance(self.smtp_host, str) or not self.imap_host or not self.smtp_host:
            raise ValueError("请配置 [email].imap_host 和 smtp_host")
        self.imap_port = int(config.get("email", "imap_port", 993))
        self.smtp_port = int(config.get("email", "smtp_port", 465))
        self.smtp_security = config.get("email", "smtp_security", "ssl")
        if self.smtp_security not in {"ssl", "starttls"}:
            raise ValueError("smtp_security 仅支持 ssl 或 starttls")
        self.mailbox = config.get("email", "mailbox", "INBOX")
        if not isinstance(self.mailbox, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", self.mailbox):
            raise ValueError("mailbox 只能包含字母、数字、下划线、点、斜杠、连字符")
        self.poll_interval = int(config.get("email", "poll_interval", 30))
        if self.poll_interval < 5:
            raise ValueError("邮件 poll_interval 不得小于 5 秒")
        self.initial_backlog = config.get("email", "initial_backlog", False) is True
        self.state_path = config.data_dir / "email-cursor.json"
        self.state = read_state(self.state_path)
        self.context = ssl.create_default_context()

    def allowed_sender(self, message: EmailMessage) -> str | None:
        if len(message.get_all("From", [])) != 1:
            return None
        try:
            sender = exact_address(str(message["From"]))
        except ValueError:
            return None
        if sender not in self.allowed_senders or sender == self.address:
            return None
        headers = message.get_all("X-JoyAgent-Token", []) + message.get_all("X-JYAgent-Token", [])
        # Legacy senders remain compatible, but mixed or repeated headers must
        # never create different interpretations of the authenticated token.
        if len(headers) > 1:
            return None
        if self.auth_token:
            if len(headers) != 1 or not hmac.compare_digest(str(headers[0]).strip().encode(), self.auth_token.encode()):
                return None
        if str(message.get("Auto-Submitted", "no")).strip().lower() != "no":
            return None
        if str(message.get("Precedence", "")).strip().lower() in {"bulk", "junk", "list"}:
            return None
        if any(header in message for header in ("List-Id", "X-JoyAgent-AutoReply", "X-JYAgent-AutoReply", "X-Auto-Response-Suppress")):
            return None
        return sender

    def _message_text(self, message: EmailMessage) -> str | None:
        chunks = []
        parts = message.walk() if message.is_multipart() else [message]
        for part in parts:
            if part.get_content_type() != "text/plain" or part.get_content_disposition() == "attachment":
                continue
            try:
                content = part.get_content()
            except (LookupError, UnicodeError):
                return None
            if isinstance(content, str):
                chunks.append(content)
        text = "\n".join(chunks).strip()
        if not text or len(text) > MAX_MESSAGE:
            return None
        subject = str(message.get("Subject", ""))[:300]
        return f"邮件主题：{subject}\n\n{text}"[:MAX_MESSAGE]

    def _send(self, reply: EmailMessage, recipient: str) -> None:
        # Recipient is derived exclusively from exact allowlisted From, never
        # Reply-To, Return-Path, CC, forwarded headers, or model-generated text.
        if recipient not in self.allowed_senders:
            raise ValueError("收件人不在允许列表中")
        if self.smtp_security == "ssl":
            client = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=self.context, timeout=30)
        else:
            client = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
        with client:
            if self.smtp_security == "starttls":
                client.ehlo()
                client.starttls(context=self.context)
                client.ehlo()
            client.login(self.user, self.password)
            client.send_message(reply, from_addr=self.address, to_addrs=[recipient])

    async def handle_message(self, raw: bytes, delivery_id: str | None = None) -> bool:
        if len(raw) > MAX_EMAIL_BYTES:
            return False
        message = email.message_from_bytes(raw, policy=email.policy.default)
        sender = self.allowed_sender(message)
        if sender is None:
            return False
        text = self._message_text(message)
        if text is None:
            return False
        delivery_id = delivery_id or "direct:" + hashlib.sha256(raw).hexdigest()
        pending = self.state.get("pending")
        if pending is not None:
            if pending.get("delivery_id") != delivery_id or pending.get("recipient") != sender:
                raise RuntimeError("Email 存在其他尚未提交的回复，请检查本地游标")
            if pending.get("sent"):
                return True
            reply = email.message_from_string(pending["message"], policy=email.policy.default)
        else:
            reply = await self._prepare_reply(message, sender, text)
            pending = {"delivery_id": delivery_id, "recipient": sender, "message": reply.as_string(), "sent": False}
            next_state = {**self.state, "pending": pending}
            await asyncio.to_thread(write_state, self.state_path, next_state)
            self.state = next_state
        # Keep the same Message-ID and body on retries. SMTP has no universal
        # idempotency key; acceptance followed by connection loss can duplicate
        # delivery, but never reruns an already prepared agent turn.
        await asyncio.to_thread(self._send, reply, sender)
        next_state = {**self.state, "pending": {**pending, "sent": True}}
        await asyncio.to_thread(write_state, self.state_path, next_state)
        self.state = next_state
        return True

    async def _prepare_reply(self, message: EmailMessage, sender: str, text: str) -> EmailMessage:
        # Sender identities become opaque session keys, avoiding address syntax
        # in the shared session namespace.
        sender_key = hashlib.sha256(sender.encode()).hexdigest()[:24]
        result = await self.runtime.chat(text, session_id=f"email:{sender_key}")
        reply = EmailMessage()
        reply["From"] = self.address
        reply["To"] = sender
        subject = re.sub(r"[\r\n]+", " ", str(message.get("Subject", "")))[:180]
        reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        reply["Message-ID"] = make_msgid()
        reply["Date"] = formatdate(localtime=False)
        message_id = str(message.get("Message-ID", ""))
        if re.fullmatch(r"<[^<>\s]{1,200}>", message_id):
            reply["In-Reply-To"] = message_id
        reply["Auto-Submitted"] = "auto-replied"
        reply["Precedence"] = "bulk"
        reply["X-JoyAgent-AutoReply"] = "yes"
        reply.set_content((result.text or "（本轮没有文本回复）")[:100_000])
        return reply

    def _connect(self) -> Any:
        client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=self.context, timeout=30)
        try:
            client.login(self.user, self.password)
            status, _ = client.select(self.mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError("无法读取邮件文件夹")
            return client
        except BaseException:
            client.logout()
            raise

    def _list(self, client: Any) -> tuple[str, list[int]]:
        _, validity_data = client.response("UIDVALIDITY")
        if not validity_data or not validity_data[0]:
            raise RuntimeError("IMAP 未返回 UIDVALIDITY，拒绝推进游标")
        validity = validity_data[0].decode("ascii")
        # Search is bounded to new UIDs once the initial mailbox baseline exists.
        same = self.state.get("uidvalidity") == validity
        after = int(self.state.get("last_uid", 0)) if same else 0
        status, data = client.uid("search", None, "UID", f"{after + 1}:*")
        if status != "OK":
            raise RuntimeError("IMAP 搜索失败")
        ids = sorted(int(item) for item in (data[0] or b"").split())
        return validity, [uid for uid in ids if uid > after]

    def _fetch(self, client: Any, uid: int) -> bytes | None:
        status, data = client.uid("fetch", str(uid), "(RFC822.SIZE)")
        if status != "OK":
            raise RuntimeError("IMAP 大小查询失败")
        size_data = b" ".join(item for item in data if isinstance(item, bytes))
        match = re.search(rb"RFC822\.SIZE\s+(\d+)", size_data)
        if not match:
            raise RuntimeError("无法确定邮件大小")
        if int(match.group(1)) > MAX_EMAIL_BYTES:
            return None
        status, data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError("IMAP 读取失败")
        for item in data:
            if isinstance(item, tuple) and isinstance(item[1], bytes):
                return item[1]
        raise RuntimeError("IMAP 没有返回邮件正文")

    async def poll_once(self) -> None:
        client = await asyncio.to_thread(self._connect)
        try:
            validity, uids = await asyncio.to_thread(self._list, client)
            if self.state.get("uidvalidity") != validity:
                # A new installation or rebuilt mailbox starts at its current
                # high-water mark unless backlog processing was opted into.
                self.state = {"uidvalidity": validity, "last_uid": 0, "message_ids": []}
                if not self.initial_backlog:
                    self.state["last_uid"] = max(uids, default=0)
                    await asyncio.to_thread(write_state, self.state_path, self.state)
                    return
            for uid in uids[:50]:
                raw = await asyncio.to_thread(self._fetch, client, uid)
                message_id = ""
                known = self.state.get("message_ids", [])
                if raw is not None:
                    parsed = email.message_from_bytes(raw, policy=email.policy.default)
                    # Only allowed messages may reserve a message ID, so an
                    # unauthorized sender cannot poison the dedup cache.
                    if self.allowed_sender(parsed):
                        message_id = str(parsed.get("Message-ID", ""))[:256]
                    if not message_id or message_id not in known:
                        await self.handle_message(raw, delivery_id=f"{validity}:{uid}")
                next_state = {"uidvalidity": validity, "last_uid": uid, "message_ids": (known + [message_id])[-512:] if message_id else known}
                await asyncio.to_thread(write_state, self.state_path, next_state)
                self.state = next_state
        finally:
            try:
                await asyncio.to_thread(client.logout)
            except Exception:
                pass

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        print("Email 通道已启动，仅回复允许的发件人。Ctrl+C 停止。", flush=True)
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                # Provider exceptions can include credentials or message bodies.
                print(f"Email 本轮失败，将重试（{type(exc).__name__}）。", file=sys.stderr, flush=True)
            await wait_or_stop(stop, self.poll_interval)
