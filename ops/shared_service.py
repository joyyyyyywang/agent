#!/usr/bin/env python3
"""Linux user-space supervisor; all deployment paths must be absolute.

  python3 ops/shared_service.py start --app-dir /opt/joyagent --config /etc/joyagent/agent.toml --state-dir /var/lib/joyagent/service
  python3 ops/shared_service.py status --state-dir /var/lib/joyagent/service
  python3 ops/shared_service.py stop --state-dir /var/lib/joyagent/service

Start detaches from the terminal; stdout/stderr append to private service.log.
The supervisor retries crashes with 1..30s backoff. Stop gives Web 10s to handle
SIGINT, then 5s for SIGTERM before killing its own child. This does not configure
HTTPS, network access, authentication, or reboot startup; configure those first.
Stop with the currently running release before switching an application symlink;
process identity deliberately includes this script's resolved release path.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time

SCRIPT = str(Path(__file__).resolve())


def absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("deployment paths must be absolute")
    return path


def private_state(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("state directory must be owned by this user with mode 700, without a final symlink")


def private_file(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise ValueError("service files must be owned by this user with mode 600")
    return fd


def identity(pid: int) -> str:
    # /proc/stat's comm may contain spaces and parentheses; fields after it start at 3.
    value = Path(f"/proc/{pid}/stat").read_text()
    return value[value.rfind(")") + 2:].split()[19]


def current(state: Path) -> dict | None:
    try:
        with os.fdopen(private_file(state / "supervisor.json"), "r") as stream:
            record = json.loads(stream.read(4097))
        pid = record["pid"]
        if type(pid) is not int or pid <= 1 or record["started"] != identity(pid):
            return None
        if Path(f"/proc/{pid}").stat().st_uid != os.geteuid():
            return None
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        expected = [SCRIPT.encode(), b"_run", b"--state-dir", str(state).encode()]
        if command[1:5] != expected:
            return None
        return record
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def stop_child(child: subprocess.Popen) -> None:
    for sig, timeout in ((signal.SIGINT, 10), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        if child.poll() is not None:
            return
        child.send_signal(sig)
        try:
            child.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
    raise RuntimeError("Web child did not exit")


def supervise(args: argparse.Namespace) -> int:
    lock = private_file(args.state_dir / "supervisor.lock")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        return 1
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    record = {"pid": os.getpid(), "started": identity(os.getpid())}
    temporary = args.state_dir / f"supervisor.{os.getpid()}.tmp"
    with os.fdopen(private_file(temporary), "w") as stream:
        stream.truncate(0)
        stream.write(json.dumps(record))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(args.state_dir / "supervisor.json")
    child, delay = None, 1
    command = [str(args.python), "-m", "joyagent", "--config", str(args.config), "web",
               "--host", args.host, "--port", str(args.port)]
    try:
        while not stopped.is_set():
            started = time.monotonic()
            try:
                child = subprocess.Popen(command, cwd=args.app_dir, stdin=subprocess.DEVNULL,
                                         env={**os.environ, "PYTHONUNBUFFERED": "1"})
                while child.poll() is None and not stopped.wait(0.25):
                    pass
                if stopped.is_set():
                    break
                print(f"Web exited ({child.returncode}); retrying in {delay}s", flush=True)
            except OSError as exc:
                print(f"Web launch failed ({type(exc).__name__}); retrying in {delay}s", flush=True)
            if time.monotonic() - started >= 60:
                delay = 1
            stopped.wait(delay)
            delay = min(delay * 2, 30)
    finally:
        try:
            if child is not None:
                stop_child(child)
        finally:
            (args.state_dir / "supervisor.json").unlink(missing_ok=True)
            os.close(lock)
    return 0


def control(args: argparse.Namespace) -> int:
    private_state(args.state_dir)
    record = current(args.state_dir)
    if args.action == "status":
        print(f"running supervisor pid={record['pid']}" if record else "stopped")
        return 0 if record else 1
    if args.action == "stop":
        if record is None:
            print("stopped")
            return 0
        # Bind the signal to this process instance; never kill a reused PID.
        descriptor = os.pidfd_open(record["pid"])
        try:
            if current(args.state_dir) != record:
                raise RuntimeError("supervisor identity changed; refusing to signal")
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        finally:
            os.close(descriptor)
        deadline = time.monotonic() + 25
        while current(args.state_dir) == record and time.monotonic() < deadline:
            time.sleep(0.1)
        if current(args.state_dir) == record:
            raise RuntimeError("supervisor did not stop within 25s")
        print("stopped")
        return 0
    if record:
        print(f"already running supervisor pid={record['pid']}")
        return 0
    command = [str(args.python), SCRIPT, "_run", "--state-dir", str(args.state_dir),
               "--app-dir", str(args.app_dir), "--config", str(args.config),
               "--python", str(args.python), "--host", args.host, "--port", str(args.port)]
    with os.fdopen(private_file(args.state_dir / "service.log"), "ab") as log:
        child = subprocess.Popen(command, cwd=args.app_dir, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = current(args.state_dir)
        if record:
            print(f"started supervisor pid={record['pid']}; log={args.state_dir / 'service.log'}")
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.05)
    raise RuntimeError("supervisor did not start; inspect the private service.log")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("start", "stop", "status", "_run"))
    parser.add_argument("--state-dir", type=absolute, required=True)
    parser.add_argument("--app-dir", type=absolute)
    parser.add_argument("--config", type=absolute)
    parser.add_argument("--python", type=absolute, default=Path(sys.executable).resolve())
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("this supervisor requires Linux /proc and pidfd support")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.action in {"start", "_run"} and (args.app_dir is None or args.config is None):
        parser.error("start requires --app-dir and --config")
    try:
        private_state(args.state_dir)
        return supervise(args) if args.action == "_run" else control(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"service error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
