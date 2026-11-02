"""Signed, anonymous browser sessions without account or password storage."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import time

SESSION_SECONDS = 7 * 24 * 60 * 60
_TOKEN = re.compile(r"[0-9a-f]{112}\Z", re.ASCII)
_SIGNING_DOMAIN = b"joyagent:visitor-session:v1\x00"
_CSRF_DOMAIN = b"joyagent:visitor-csrf:v1\x00"
_KEY_FILE = "signing.key"


@dataclass(frozen=True)
class VisitorSession:
    token: str
    csrf: str
    user_id: str


def _private_key(directory: Path) -> bytes:
    """Read an owner-only key; publish new keys atomically across server starts.

    As with shared tenant storage, ancestors are administrator-controlled. The
    configured directory itself and the key must not be substituted by symlinks.
    Descriptor-relative operations keep key access inside the checked directory.
    """
    directory_fd = None
    temporary = None
    try:
        directory = directory.parent.resolve(strict=True) / directory.name
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(directory_fd)
        if (not stat.S_ISDIR(info.st_mode)
                or os.name == "posix" and (info.st_uid != os.geteuid()
                                          or stat.S_IMODE(info.st_mode) != 0o700)):
            raise ValueError("匿名会话目录必须属于服务用户，且权限必须为 700。")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(_KEY_FILE, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            temporary = f".signing-{secrets.token_hex(16)}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # A competing startup may already have published its own key.
                os.link(temporary, _KEY_FILE, src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd, follow_symlinks=False)
            except FileExistsError:
                pass
            os.unlink(temporary, dir_fd=directory_fd)
            temporary = None
            descriptor = os.open(_KEY_FILE, flags, dir_fd=directory_fd)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode)
                    or os.name == "posix" and (info.st_uid != os.geteuid()
                                              or stat.S_IMODE(info.st_mode) != 0o600)):
                raise ValueError("匿名会话密钥必须是服务用户拥有的普通文件，权限必须为 600。")
            key = stream.read(33)
        if len(key) != 32:
            raise ValueError("匿名会话密钥格式无效，请联系服务管理员。")
        return key
    except (OSError, RuntimeError):
        raise ValueError("无法读取匿名会话配置，请检查目录及文件权限。") from None
    finally:
        if directory_fd is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)


class VisitorStore:
    """Issue stateless signed identities; only the signing key is persisted.

    A cookie is the capability for one isolated visitor's data. It expires after
    seven days, remains valid across restarts, and contains no identifying account
    information. Clearing the cookie creates a new visitor rather than recovering
    a prior identity. Issuing a cookie does not create a tenant runtime or files.
    """

    def __init__(self, directory: Path):
        self._key = _private_key(Path(directory))

    def _signature(self, payload: str) -> str:
        return hmac.new(self._key, _SIGNING_DOMAIN + payload.encode("ascii"), hashlib.sha256).hexdigest()

    def _session(self, token: str) -> VisitorSession:
        csrf = hmac.new(self._key, _CSRF_DOMAIN + token.encode("ascii"), hashlib.sha256).hexdigest()
        return VisitorSession(token, csrf, token[:32])

    def create(self) -> VisitorSession:
        payload = secrets.token_hex(16) + f"{int(time.time()) + SESSION_SECONDS:016x}"
        return self._session(payload + self._signature(payload))

    def authenticate(self, token: str) -> VisitorSession | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        payload, signature = token[:48], token[48:]
        if not hmac.compare_digest(signature, self._signature(payload)):
            return None
        if int(token[32:48], 16) <= time.time():
            return None
        return self._session(token)

    def close(self) -> None:
        """No connections or per-visitor records are held by this store."""
