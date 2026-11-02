"""Small, read-only SKILL.md catalog. Skills are instructions, never Python imports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class SkillCatalog:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("技能名称只允许字母、数字、下划线和连字符")
        path = (self.root / name / "SKILL.md").resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("技能路径越界")
        return path

    def list(self) -> list[dict[str, str]]:
        if not self.root.exists():
            return []
        result = []
        for directory in sorted(self.root.iterdir()):
            try:
                path = self._path(directory.name)
                if not path.is_file():
                    continue
                content = self.read(directory.name)["content"]
                description = next((line.removeprefix("description:").strip().strip('"\'')
                                    for line in content.splitlines()[:20]
                                    if line.startswith("description:")), "")
                result.append({"name": directory.name, "description": description[:240]})
            except (ValueError, OSError, UnicodeError):
                continue
            if len(result) >= 50:
                break
        return result

    def read(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        with path.open("rb") as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError("SKILL.md 超过 32 KiB，请拆分技能")
        return {"name": name, "content": raw.decode("utf-8")}
