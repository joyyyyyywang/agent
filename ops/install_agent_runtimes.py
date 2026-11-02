#!/usr/bin/env python3
"""Explicit, private installation of the pinned optional Agent CLIs.

This script is never called by the Agent or its Web startup. It does not read
existing CLI auth or npm user configuration, and verifies versions only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


PACKAGES = {"node": "node", "codex": "@openai/codex",
            "pi": "@earendil-works/pi-coding-agent", "dsh": "@deepseek-ai/dsh"}
REGISTRY = "https://registry.anpm.alibaba-inc.com"


def absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("--prefix must be an absolute path without '..'")
    return path


def manifest(source: Path) -> tuple[dict, bytes, bytes]:
    package = (source / "package.json").read_bytes()
    lock = (source / "package-lock.json").read_bytes()
    data, locked = json.loads(package), json.loads(lock)
    dependencies = data.get("dependencies", {})
    if not isinstance(dependencies, dict) or set(dependencies) != set(PACKAGES.values()):
        raise ValueError("manifest must contain exactly the four pinned runtime packages")
    if not all(isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version)
               for version in dependencies.values()):
        raise ValueError("runtime package versions must be exact")
    if locked.get("lockfileVersion") != 3 or locked.get("packages", {}).get("", {}).get("dependencies") != dependencies:
        raise ValueError("package-lock.json does not match package.json")
    for entry in locked["packages"].values():
        if "resolved" in entry:
            url = urlsplit(entry["resolved"])
            if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
                raise ValueError("lockfile contains a non-public download URL")
    return data, package, lock


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("installation directory must be a real directory owned by the current user")
    if info.st_mode & 0o022:
        raise ValueError("installation directory cannot be writable by group or others")


def environment(home: Path, npm: str | None = None, prefix: Path | None = None) -> dict[str, str]:
    """Use an allowlist, not os.environ: auth and user npm settings stay private."""
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("npm-user.conf", "npm-global.conf"):
        path = home / name
        path.write_text("", encoding="utf-8")
        path.chmod(0o600)
    paths = ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]
    if npm:
        paths.insert(0, str(Path(npm).parent))
    if prefix:
        paths.insert(0, str(prefix / "node_modules" / ".bin"))
    return {"PATH": os.pathsep.join(paths), "HOME": str(home), "LANG": "C.UTF-8", "CI": "1",
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"), "CODEX_HOME": str(home / "codex"),
            "PI_CODING_AGENT_DIR": str(home / "pi"), "DSH_CONFIG_DIR": str(home / "dsh"),
            "npm_config_userconfig": str(home / "npm-user.conf"), "npm_config_globalconfig": str(home / "npm-global.conf"),
            "npm_config_cache": str(home / "npm-cache"), "npm_config_registry": REGISTRY,
            "npm_config_update_notifier": "false", "npm_config_audit": "false", "npm_config_fund": "false"}


def wrappers(prefix: Path) -> None:
    target = prefix / "bin"
    target.mkdir(mode=0o700)
    for name in PACKAGES:
        script = ("#!/bin/sh\n"
                  'agent_runtime_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1\n'
                  'PATH="$agent_runtime_dir/node_modules/.bin:$PATH"\nexport PATH\n'
                  f'exec "$agent_runtime_dir/node_modules/.bin/{name}" "$@"\n')
        path = target / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o700)


def verify(prefix: Path, dependencies: dict[str, str]) -> dict[str, str]:
    versions = {}
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=prefix) as directory:
        env = environment(Path(directory), prefix=prefix)
        for name, package in PACKAGES.items():
            executable = prefix / "bin" / name
            result = subprocess.run([str(executable), "--version"], cwd=directory, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=45, check=False)
            output = result.stdout.decode("utf-8", errors="replace").strip()
            expected = dependencies[package]
            if result.returncode or not re.search(r"(?<![\d.])" + re.escape(expected) + r"(?![\d.])", output):
                raise RuntimeError(f"{name} version check failed (expected {expected}, exit {result.returncode})")
            versions[name] = expected
    return versions


def _owned_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o022):
        raise ValueError("runtime support files must be regular, owner-managed files")


def write_record(prefix: Path, record: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".installation-", suffix=".json", dir=prefix)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, ensure_ascii=False, indent=2)
            output.write("\n")
        temporary.replace(prefix / "installation.json")
    finally:
        temporary.unlink(missing_ok=True)


def install(prefix: Path, source: Path, *, timeout: int = 1800) -> dict:
    if not prefix.is_absolute() or ".." in prefix.parts or prefix == Path(prefix.anchor):
        raise ValueError("prefix must name a dedicated absolute installation directory")
    data, package, lock = manifest(source)
    digest = hashlib.sha256(lock).hexdigest()
    if prefix.exists() or prefix.is_symlink():
        private_directory(prefix)
        stamp = prefix / "installation.json"
        if stamp.is_symlink() or not stamp.is_file():
            raise ValueError("prefix already exists and is not a completed managed installation; choose a new prefix")
        _owned_file(stamp)
        record = json.loads(stamp.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("installation metadata must be an object")
        if record.get("lock_sha256") != digest:
            raise ValueError("prefix contains different pinned packages; choose a new version directory")
        record["versions"] = verify(prefix, data["dependencies"])
        write_record(prefix, record)
        return record
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("npm is required for this explicit installation step")
    private_directory(prefix.parent)
    staging = Path(tempfile.mkdtemp(prefix=f".{prefix.name}-install-", dir=prefix.parent))
    staging.chmod(0o700)
    try:
        (staging / "package.json").write_bytes(package)
        (staging / "package-lock.json").write_bytes(lock)
        for name in ("package.json", "package-lock.json"):
            (staging / name).chmod(0o600)
        installer_home = staging / ".install-home"
        env = environment(installer_home, npm=npm)
        log_path = staging / "install.log"
        with log_path.open("wb") as log:
            log_path.chmod(0o600)
            process = subprocess.Popen([npm, "ci", "--no-audit", "--no-fund"], cwd=staging, env=env,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            started, next_progress = time.monotonic(), 0.0
            try:
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if elapsed > timeout:
                        raise TimeoutError("npm installation exceeded its deadline")
                    if elapsed >= next_progress:
                        print(f"Installing pinned Agent runtimes: {int(elapsed)}s elapsed; log={log_path}", flush=True)
                        next_progress = elapsed + 15
                    time.sleep(0.25)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=10)
            if process.returncode:
                raise RuntimeError(f"npm ci failed (exit {process.returncode}); inspect private log {log_path}")
        wrappers(staging)
        versions = verify(staging, data["dependencies"])
        shutil.rmtree(installer_home)
        record = {"lock_sha256": digest, "versions": versions, "prefix": str(prefix),
                  "executables": {name: str(prefix / "bin" / name) for name in PACKAGES}}
        write_record(staging, record)
        if prefix.exists() or prefix.is_symlink():
            raise ValueError("prefix appeared during installation; refusing to replace it")
        staging.rename(prefix)
        return record
    except BaseException:
        # Preserve the private staging log for an explicit retry/diagnosis. Never
        # remove or replace an existing runtime when a new installation fails.
        print(f"Installation incomplete; private diagnostics retained at {staging}", file=sys.stderr)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, type=absolute)
    parser.add_argument("--source", type=absolute, default=Path(__file__).resolve().parent / "agent-runtimes")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.prefix, args.source), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        print(f"Runtime installation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
