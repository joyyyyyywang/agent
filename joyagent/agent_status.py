"""Read host PIDs for owned native workers using bounded process metadata only."""
from __future__ import annotations

import os
from pathlib import Path


def _metadata(directory):
    with (directory / "status").open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("process status exceeds limit")
    values = {}
    for line in raw.splitlines():
        key, _, value = line.partition(b":")
        if key in {b"PPid", b"NSpid", b"State"}:
            if key in values:
                raise ValueError("duplicate process metadata")
            values[key] = value.split()
    parent, namespaces = values[b"PPid"], values[b"NSpid"]
    if len(parent) != 1 or not namespaces or not all(v.isdigit() for v in [*parent, *namespaces]):
        raise ValueError("invalid process metadata")
    if values[b"State"][0] in {b"Z", b"X", b"x"}:
        raise ValueError("process exited")
    # Start time distinguishes a process from a reused PID. stat is kernel
    # metadata; no command line, environment, or native state is inspected.
    with (directory / "stat").open("rb") as stream:
        raw_stat = stream.read(65537)
    if len(raw_stat) > 65536:
        raise ValueError("process stat exceeds limit")
    started = int(raw_stat[raw_stat.rfind(b")") + 2:].split()[19])
    return int(parent[0]), tuple(map(int, namespaces)), started


def native_host_pids(requests, *, proc_root=None):
    """Map backend -> host PID from (bwrap PID, inner native PID) requests.

    Native helpers report a PID in their own namespace. Matching only the last
    NSpid value would mix visitors; require both ancestry and exact namespace
    depth. Missing/ambiguous metadata produces no PID instead of a guess.
    """
    if not requests:
        return {}
    root = Path("/proc") if proc_root is None else Path(proc_root)
    statuses = {}
    try:
        with os.scandir(root) as entries:
            pids = []
            for entry in entries:
                if entry.name.isascii() and entry.name.isdigit():
                    pids.append(int(entry.name))
                    if len(pids) > 10000:
                        return {}
        for pid in pids:
            try:
                statuses[pid] = _metadata(root / str(pid))
            except (OSError, ValueError, KeyError, IndexError):
                continue
    except OSError:
        return {}
    matches = {}
    for backend, (manager_pid, native_pid) in requests.items():
        if (type(manager_pid) is not int or manager_pid <= 0 or type(native_pid) is not int
                or native_pid <= 0 or manager_pid not in statuses):
            continue
        _, root_ns, _ = statuses[manager_pid]
        candidates = []
        for pid, (parent, namespaces, _) in statuses.items():
            if len(namespaces) != len(root_ns) + 1 or namespaces[-1] != native_pid:
                continue
            seen = {pid}
            for _ in range(128):
                if parent == manager_pid:
                    candidates.append(pid)
                    break
                if parent in seen or parent not in statuses:
                    break
                seen.add(parent)
                parent = statuses[parent][0]
        if len(candidates) == 1:
            candidate = candidates[0]
            try:
                if (_metadata(root / str(manager_pid)) == statuses[manager_pid]
                        and _metadata(root / str(candidate)) == statuses[candidate]):
                    matches[backend] = candidate
            except (OSError, ValueError, KeyError, IndexError):
                pass
    return matches
