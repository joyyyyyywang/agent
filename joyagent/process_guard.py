"""Sample one Linux process tree without reading commands or environments.

This is a bounded, best-effort sampler, not a cgroup memory/CPU quota. Short
peaks can occur between samples; an unreadable, previously unknown child cannot
be attributed without its PPid. Once identified, unreadable live children fail
closed. The caller must kill/reap the namespace when this watcher raises.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
from pathlib import Path
from typing import Any


_PROC_ROOT = Path("/proc")
_MAX_PIDS = 10000
_MAX_STATUS_BYTES = 65536


class ProcessGuardError(ValueError):
    """A bounded diagnostic category; never retain sampled process contents."""
    MESSAGES = {
        "memory_limit": "Agent 进程树内存超过上限，已停止执行",
        "thread_limit": "Agent 进程树线程数超过上限，已停止执行",
        "sampling_failed": "无法读取 Agent 进程资源状态，已停止执行",
    }

    def __init__(self, code: str):
        self.code = code if code in self.MESSAGES else "sampling_failed"
        super().__init__(self.MESSAGES[self.code])


@dataclass(frozen=True)
class ProcessUsage:
    pids: frozenset[int]
    threads: int
    rss_bytes: int


def _read_status(path: Path) -> tuple[int, int, int]:
    """Return PPid, Threads and VmRSS bytes from one bounded status file."""
    with path.open("rb") as stream:
        raw = stream.read(_MAX_STATUS_BYTES + 1)
    if len(raw) > _MAX_STATUS_BYTES:
        raise ValueError("进程状态超过采样上限")
    fields = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(b":")
        if not separator or key not in {b"PPid", b"Threads", b"VmRSS"}:
            continue
        if key in fields:
            raise ValueError("进程状态字段重复")
        pieces = value.split()
        expected = 2 if key == b"VmRSS" else 1
        if (len(pieces) != expected or not pieces[0].isdigit()
                or (key == b"VmRSS" and pieces[1] != b"kB")):
            raise ValueError("进程资源状态无效")
        fields[key] = int(pieces[0])
    if b"PPid" not in fields or fields.get(b"Threads", 0) < 1:
        raise ValueError("进程状态缺少 PPid 或 Threads")
    # Zombie/kernel tasks have no resident address space and may omit VmRSS.
    return fields[b"PPid"], fields[b"Threads"], fields.get(b"VmRSS", 0) * 1024


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        # Inaccessibility is not evidence that a known process has exited.
        return True


def sample_process_tree(root_pid: int, *, known_pids: frozenset[int] = frozenset(),
                        proc_root: Path | None = None, max_pids: int = _MAX_PIDS) -> ProcessUsage:
    """Read one /proc snapshot; ``proc_root`` also supports filesystem fixtures.

    Only descendants are charged. Unrelated processes may belong to other users
    and their unreadable status files do not stop this job. ``known_pids`` must
    come from the preceding snapshot of this same root, never an unrelated job.
    """
    if type(root_pid) is not int or root_pid <= 0 or type(max_pids) is not int or max_pids < 1:
        raise ValueError("进程采样参数无效")
    proc_root = _PROC_ROOT if proc_root is None else proc_root
    pids = set()
    try:
        with os.scandir(proc_root) as entries:
            for entry in entries:
                if entry.name.isascii() and entry.name.isdigit():
                    pids.add(int(entry.name))
                    if len(pids) > max_pids:
                        raise ValueError("主机进程数量超过安全采样上限")
    except OSError:
        raise ValueError("无法读取主机进程目录，已停止 Agent") from None

    statuses: dict[int, tuple[int, int, int]] = {}
    unreadable = set()
    for pid in pids:
        directory = proc_root / str(pid)
        try:
            statuses[pid] = _read_status(directory / "status")
        except (OSError, ValueError):
            # /proc disappears as a process exits. A still-present directory
            # with unreadable/malformed status is different from normal exit.
            if _exists(directory):
                unreadable.add(pid)
    if unreadable.intersection(known_pids | {root_pid}):
        raise ValueError("无法读取 Agent 进程资源状态，已停止执行")
    if root_pid not in statuses:
        # The supervisor checks returncode after this snapshot. If the root
        # vanished during exit, the next sample/returncode check settles it.
        return ProcessUsage(frozenset(), 0, 0)

    children: dict[int, list[int]] = {}
    for pid, (parent, _, _) in statuses.items():
        children.setdefault(parent, []).append(pid)
    descendants = set()
    pending = [root_pid]
    threads = rss = 0
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        _, count, resident = statuses[pid]
        threads += count
        rss += resident
        pending.extend(children.get(pid, ()))
    return ProcessUsage(frozenset(descendants), threads, rss)


async def watch_process(process: Any, *, interval: float = .2, max_threads: int = 128,
                        max_rss_mb: int = 768) -> None:
    """Raise ValueError on sampled limits; return once the supervised root exits.

    No signals are sent here. The owning runner handles namespace termination,
    output readers and reaping in its existing cancellation/exception cleanup.
    """
    if (type(interval) not in {int, float} or not math.isfinite(interval) or interval <= 0
            or type(max_threads) is not int or max_threads < 1
            or type(max_rss_mb) is not int or max_rss_mb < 1
            or type(process.pid) is not int or process.pid <= 0):
        raise ValueError("Agent 资源监视参数无效")
    known_pids: frozenset[int] = frozenset()
    while process.returncode is None:
        try:
            usage = await asyncio.to_thread(sample_process_tree, process.pid, known_pids=known_pids)
        except ValueError:
            if process.returncode is not None:
                return
            raise ProcessGuardError("sampling_failed") from None
        if process.returncode is not None:
            return
        if usage.threads > max_threads:
            raise ProcessGuardError("thread_limit")
        if usage.rss_bytes > max_rss_mb * 1024 * 1024:
            raise ProcessGuardError("memory_limit")
        known_pids = usage.pids
        await asyncio.sleep(interval)
