"""Optional, owner-installed abstract translation. Core runtime stays stdlib-only."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import tempfile
import time

from .execution import SandboxExecutor, execution_settings
from .tools import Workspace


class OfflinePaperTranslator:
    timeout_seconds = 600

    def __init__(self, runtime: Path):
        self.runtime = runtime.resolve(strict=True)
        required = [self.runtime, self.runtime / "venv/bin/python",
                    self.runtime / "package/translate-en_zh-1_9/metadata.json",
                    self.runtime / "package/translate-en_zh-1_9/sentencepiece.model",
                    self.runtime / "package/translate-en_zh-1_9/model/model.bin",
                    self.runtime / "package/translate-en_zh-1_9/model/config.json",
                    self.runtime / "package/translate-en_zh-1_9/model/shared_vocabulary.json"]
        for path in required:
            info = path.stat()
            if info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("论文离线翻译运行库必须由管理员管理，且不可被组或其他用户修改")
        metadata = json.loads(required[2].read_text())
        if (metadata.get("from_code"), metadata.get("to_code"), metadata.get("package_version")) != ("en", "zh", "1.9"):
            raise ValueError("论文摘要翻译需要 Argos en_zh 1.9 模型")
        # Changes to weights/tokenizer or decoding code invalidate cached results.
        digest = hashlib.sha256(b"argos-en-zh-1.9-ct2-4.8.4-abstract-v1")
        for path in required[3:] + [Path(__file__).with_name("paper_translation_worker.py")]:
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    digest.update(block)
        self.version = digest.hexdigest()

    def _command(self, directory: Path):
        # These fixed resource limits are independent of visitor tool settings.
        from types import SimpleNamespace
        settings = execution_settings(SimpleNamespace(data={}))
        settings.update(memory_mb=1024, process_limit=4096)
        executor = SandboxExecutor(settings, Workspace(directory, protected=[self.runtime]))
        worker = Path(__file__).with_name("paper_translation_worker.py")
        command, env = executor.launch(["/translator/venv/bin/python", "-I", "/mcp/server.py"], script=worker)
        position = command.index("--chdir")
        command[position:position] = ["--ro-bind", str(self.runtime), "/translator"]
        env.update(OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
        return command, env

    def translate(self, rows: list[dict], stop):
        """Stream completed abstracts; never inherit credentials or network access."""
        if not rows or stop.is_set():
            return
        expected = {row["id"] for row in rows}
        payload = json.dumps([{key: row[key] for key in ("id", "summary")} for row in rows]).encode()
        if len(rows) > 100 or len(expected) != len(rows) or len(payload) > 8 * 1024 * 1024:
            raise ValueError("invalid translation batch")
        with tempfile.TemporaryDirectory(prefix="joyagent-paper-translation-") as directory:
            command, env = self._command(Path(directory))
            with tempfile.TemporaryFile() as request:
                request.write(payload)
                request.seek(0)
                process = subprocess.Popen(command, stdin=request, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, env=env, cwd="/", start_new_session=True)
                try:
                    deadline, size, pending = time.monotonic() + self.timeout_seconds, 0, b""
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while selector.get_map():
                            if stop.is_set():
                                return
                            if time.monotonic() >= deadline:
                                raise TimeoutError("offline translation timed out")
                            for key, _ in selector.select(timeout=0.1):
                                block = os.read(key.fd, 65536)
                                if not block:
                                    selector.unregister(key.fd)
                                    continue
                                size += len(block)
                                pending += block
                                if size > 16 * 1024 * 1024:
                                    raise ValueError("translation output too large")
                                while b"\n" in pending:
                                    line, pending = pending.split(b"\n", 1)
                                    value = json.loads(line)
                                    if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
                                            or value["id"] not in expected
                                            or not isinstance(value.get("summary_zh"), str)
                                            or not 0 < len(value["summary_zh"].strip()) <= 40000):
                                        raise ValueError("invalid translated abstract")
                                    expected.remove(value["id"])
                                    yield value
                                if len(pending) > 256000:
                                    raise ValueError("translation line too large")
                    while process.poll() is None:
                        if stop.wait(0.1):
                            return
                        if time.monotonic() >= deadline:
                            raise TimeoutError("offline translation timed out")
                    if process.returncode != 0 or pending or expected:
                        raise ValueError("offline translation incomplete")
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    process.stdout.close()


def paper_translator(config):
    value = config.data.get("papers", {}).get("translation_runtime")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ValueError("papers.translation_runtime 必须是已安装离线翻译运行库的绝对路径")
    runtime = Path(value).resolve(strict=True)
    roots = [config.workspace, config.data_dir]
    session_dir = config.get("web", "session_dir")
    if session_dir:
        roots.append(config.resolve(session_dir))
    if any(runtime.is_relative_to(root) or root.is_relative_to(runtime) for root in roots):
        raise ValueError("论文翻译运行库必须与访客工作区和状态目录分离")
    return OfflinePaperTranslator(runtime)
