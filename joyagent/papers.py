"""Anonymous papers collection with optional cached, offline abstract translation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Callable
from urllib.error import HTTPError
import urllib.request

from .news import FETCH_TIMEOUT, MAX_BYTES, NewsService, _SourceRedirect, _iso, _timestamp

PAPERS_SOURCE = {"id": "huggingface", "name": "Hugging Face Papers", "url": "https://huggingface.co/papers"}
PAPERS_API = "https://huggingface.co/api/daily_papers?sort=publishedAt&limit=100"
ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?")
AGENTS_SOURCE = {"id": "agents", "name": "Agents 智能体", "url": "https://huggingface.co/collections/Tempo14/agents"}
AGENTS_SLUG = "Tempo14/agents-65c69e9e1d61ffef8641acd0"
AGENTS_API = "https://huggingface.co/api/collections/" + AGENTS_SLUG
AGENT_TITLE = re.compile(r"\bagents?\b|\bagentic\b", re.IGNORECASE)
AGENT_LIMIT = 20


def fetch_papers(url: str, *, cache: dict | None = None) -> str:
    """Only the fixed public API is fetched; never follow paper or PDF links."""
    if url != PAPERS_SOURCE["url"]:
        raise ValueError("unknown papers source")
    return _fetch_paper_json(PAPERS_API, cache=cache)


def _fetch_paper_json(url: str, *, cache: dict | None = None) -> str:
    prefix = "https://huggingface.co/api/papers/"
    if url not in {PAPERS_API, AGENTS_API} and not (url.startswith(prefix) and ARXIV_ID.fullmatch(url[len(prefix):])):
        raise ValueError("unknown papers API")
    headers = {"User-Agent": "JoyAgent-Papers/1.0", "Accept": "application/json", "Accept-Encoding": "identity"}
    previous = cache.get(url) if cache is not None else None
    if previous is not None:
        headers["If-None-Match"] = previous[0]
    request = urllib.request.Request(url, headers=headers)
    deadline = time.monotonic() + FETCH_TIMEOUT
    try:
        response = urllib.request.build_opener(_SourceRedirect()).open(request, timeout=FETCH_TIMEOUT)
    except HTTPError as exc:
        if exc.code == 304 and previous is not None:
            exc.close()
            return previous[1]
        raise
    with response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise ValueError("unexpected papers response")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ValueError("unexpected compressed response")
        chunks, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("papers response deadline exceeded")
            chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("papers response exceeds limit")
        document = b"".join(chunks).decode("utf-8")
        if cache is not None:
            etag = response.headers.get("ETag")
            if etag and len(etag) <= 1024 and all(32 <= ord(char) < 127 for char in etag):
                cache[url] = (etag, document)
            else:
                cache.pop(url, None)
        return document


def fetch_agent_papers(url: str, *, cache: dict | None = None) -> str:
    """Prefer the supplied collection; fill to 20 with explicitly agent-titled papers.

    All requests use fixed anonymous HF read APIs. No classifier/model API and
    no requests to any of the other thirteen collection links.
    """
    if url != AGENTS_SOURCE["url"]:
        raise ValueError("unknown agents source")
    collection = json.loads(_fetch_paper_json(AGENTS_API, cache=cache))
    if (not isinstance(collection, dict) or collection.get("slug") != AGENTS_SLUG
            or collection.get("private") is not False
            or not isinstance(collection.get("items"), list) or len(collection["items"]) > 1000):
        raise ValueError("invalid agents collection")
    candidates, seen = [], set()
    for item in collection["items"]:
        if (not isinstance(item, dict) or item.get("type") != "paper"
                or not isinstance(item.get("id"), str) or not ARXIV_ID.fullmatch(item["id"])
                or item["id"] in seen):
            continue
        candidates.append(item)
        seen.add(item["id"])
    candidates.sort(key=lambda item: _timestamp(item.get("publishedAt")) or 0, reverse=True)
    selected = [{"paper": item, "selection_source": "collection"} for item in candidates[:AGENT_LIMIT]]
    if len(selected) < AGENT_LIMIT:
        daily = json.loads(fetch_papers(PAPERS_SOURCE["url"], cache=cache))
        if not isinstance(daily, list) or len(daily) > 1000:
            raise ValueError("invalid supplementary papers")
        daily = sorted((row["paper"] for row in daily if isinstance(row, dict) and isinstance(row.get("paper"), dict)),
                       key=lambda item: _timestamp(item.get("submittedOnDailyAt")) or 0, reverse=True)
        for item in daily:
            identifier, title = item.get("id"), item.get("title")
            if (not isinstance(identifier, str) or not ARXIV_ID.fullmatch(identifier) or identifier in seen
                    or not isinstance(title, str) or not AGENT_TITLE.search(title)
                    or _timestamp(item.get("publishedAt")) is None):
                continue
            selected.append({"paper": item, "selection_source": "daily"})
            seen.add(identifier)
            if len(selected) == AGENT_LIMIT:
                break
    if len(selected) != AGENT_LIMIT:
        # Keep the last complete selection if sources become temporarily sparse.
        raise ValueError("not enough agent papers for a complete selection")
    # The collection API contains titles but no abstracts. Fetch only the
    # selected paper details, then bound the conditional-request cache as well.
    keep = {AGENTS_API, PAPERS_API}
    for row in selected:
        if row["selection_source"] == "collection":
            identifier = row["paper"]["id"]
            endpoint = "https://huggingface.co/api/papers/" + identifier
            keep.add(endpoint)
            detail = json.loads(_fetch_paper_json(endpoint, cache=cache))
            if not isinstance(detail, dict) or detail.get("id") != identifier:
                raise ValueError("paper detail identity mismatch")
            row["paper"] = detail
    if cache is not None:
        for key in list(cache):
            if key not in keep:
                del cache[key]
    return json.dumps({"kind": "agent-selection-v1", "items": selected}, ensure_ascii=False)


def parse_agent_papers(source_id: str, document: str) -> list[dict]:
    if source_id != AGENTS_SOURCE["id"] or not isinstance(document, str) or len(document.encode()) > MAX_BYTES:
        raise ValueError("invalid agents document")
    value = json.loads(document)
    if (not isinstance(value, dict) or value.get("kind") != "agent-selection-v1"
            or not isinstance(value.get("items"), list) or len(value["items"]) != AGENT_LIMIT):
        raise ValueError("invalid agents selection")
    items, seen = [], set()
    for rank, row in enumerate(value["items"]):
        paper = row.get("paper") if isinstance(row, dict) else None
        if not isinstance(paper, dict) or row.get("selection_source") not in {"collection", "daily"}:
            raise ValueError("invalid selected paper")
        identifier, title, summary = paper.get("id"), paper.get("title"), paper.get("summary", "")
        published = _timestamp(paper.get("publishedAt"))
        if (not isinstance(identifier, str) or not ARXIV_ID.fullmatch(identifier) or identifier in seen
                or not isinstance(title, str) or not title.strip() or len(title) > 1000
                or not isinstance(summary, str) or len(summary) > 20000 or published is None
                or row["selection_source"] == "daily" and not AGENT_TITLE.search(title)):
            raise ValueError("invalid selected paper metadata")
        items.append({
            "id": hashlib.sha256((source_id + "\0" + identifier).encode()).hexdigest()[:24],
            "arxiv_id": identifier, "title": " ".join(title.split()), "summary": summary.strip(),
            "url": f"https://arxiv.org/abs/{identifier}", "pdf_url": f"https://arxiv.org/pdf/{identifier}",
            "discussion_url": f"https://huggingface.co/papers/{identifier}",
            "source_id": source_id, "source_name": AGENTS_SOURCE["name"],
            "published_at": _iso(published), "paper_published_at": _iso(published),
            "selection_source": row["selection_source"], "selection_rank": rank,
        })
        seen.add(identifier)
    return items


def parse_papers(source_id: str, document: str) -> list[dict]:
    if source_id != PAPERS_SOURCE["id"] or not isinstance(document, str) or len(document.encode("utf-8")) > MAX_BYTES:
        raise ValueError("invalid papers document")
    rows = json.loads(document)
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("invalid papers list")
    items, seen = [], set()
    for row in rows:
        paper = row.get("paper") if isinstance(row, dict) else None
        if not isinstance(paper, dict):
            continue
        arxiv_id, title = paper.get("id"), paper.get("title")
        if (not isinstance(arxiv_id, str) or not ARXIV_ID.fullmatch(arxiv_id) or arxiv_id in seen
                or not isinstance(title, str)):
            continue
        title = " ".join(title.split())
        # The API's outer publishedAt is not its Daily Papers selection date.
        # Keep the actual arXiv publication and the HF collection date distinct.
        collected = _timestamp(paper.get("submittedOnDailyAt"))
        published = _timestamp(paper.get("publishedAt"))
        if not title or len(title) > 1000 or collected is None:
            continue
        summary = paper.get("summary")
        summary = summary.strip()[:20000] if isinstance(summary, str) else ""
        url = f"https://arxiv.org/abs/{arxiv_id}"
        items.append({
            "id": hashlib.sha256((source_id + "\0" + arxiv_id).encode()).hexdigest()[:24],
            "arxiv_id": arxiv_id, "title": title, "summary": summary, "url": url,
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
            "discussion_url": f"https://huggingface.co/papers/{arxiv_id}",
            "source_id": source_id, "source_name": PAPERS_SOURCE["name"],
            "published_at": _iso(collected), "paper_published_at": _iso(published),
        })
        seen.add(arxiv_id)
    if not items:
        raise ValueError("no usable papers")
    return sorted(items, key=lambda item: item["published_at"], reverse=True)


class PapersService(NewsService):
    """Reuse the bounded scheduler/history cache with its own SQLite database."""

    worker_name = "joyagent-papers"

    def __init__(self, path: Path, *, interval_seconds: int = 7200,
                 fetcher: Callable[[str], str] | None = None, clock: Callable[[], float] = time.time,
                 translator=None, sources=None, parser=parse_papers):
        cache: dict = {}
        super().__init__(path, interval_seconds=interval_seconds, clock=clock,
                         fetcher=fetcher or (lambda url: fetch_papers(url, cache=cache)),
                         sources=sources or (PAPERS_SOURCE,), parser=parser)
        self.translator = translator
        self._translation_lock = threading.Lock()
        self._translating = False
        self._translation_error = None
        with self._database() as database:
            database.execute("CREATE TABLE IF NOT EXISTS translations (key TEXT PRIMARY KEY, summary_zh TEXT NOT NULL)")

    def _translation_key(self, summary: str) -> str:
        return hashlib.sha256((self.translator.version + "\0" + summary).encode()).hexdigest()

    def snapshot(self) -> dict:
        result = super().snapshot()
        with self._lock, self._database() as database:
            translations = dict(database.execute("SELECT key,summary_zh FROM translations")) if self.translator else {}
            for item in result["items"]:
                if self.translator and item.get("summary"):
                    translated = translations.get(self._translation_key(item["summary"]))
                    if translated:
                        item["summary_zh"] = translated
            result["translation"] = {
                "enabled": self.translator is not None, "running": self._translating,
                "completed": sum(bool(item.get("summary_zh")) for item in result["items"]),
                "total": sum(bool(item.get("summary")) for item in result["items"]),
                "error": self._translation_error,
            }
        return result

    def refresh(self) -> bool:
        refreshed = super().refresh()
        # Run on startup too, even when fetching is not yet due. HTTP snapshots
        # never enter this path. A restart resumes from individually saved rows.
        if self.translator is None or self._stop.is_set() or not self._translation_lock.acquire(blocking=False):
            return refreshed
        try:
            items = super().snapshot()["items"]
            summaries = {self._translation_key(item["summary"]): item["summary"]
                         for item in items if item.get("summary")}
            with self._database() as database:
                known = {row[0] for row in database.execute("SELECT key FROM translations")}
                # Bound the cache to the currently retained abstracts. A changed
                # summary/engine has a different key; changing a title does not.
                database.executemany("DELETE FROM translations WHERE key=?", [(key,) for key in known - summaries.keys()])
            pending = [{"id": key, "summary": summary} for key, summary in summaries.items() if key not in known]
            with self._lock:
                self._translation_error = None
                self._translating = bool(pending)
            if pending:
                remaining = {row["id"] for row in pending}
                for row in self.translator.translate(pending, self._stop):
                    if self._stop.is_set():
                        break
                    key, text = row.get("id"), row.get("summary_zh")
                    if key not in remaining or not isinstance(text, str) or not 0 < len(text.strip()) <= 40000:
                        raise ValueError("invalid translation result")
                    with self._database() as database:
                        database.execute("INSERT OR REPLACE INTO translations VALUES(?,?)", (key, text.strip()))
                    remaining.remove(key)
                if remaining and not self._stop.is_set():
                    raise ValueError("incomplete translation batch")
        except Exception:
            with self._lock:
                self._translation_error = "中文摘要暂未完成，保留英文原文，将按计划重试。"
        finally:
            with self._lock:
                self._translating = False
            self._translation_lock.release()
        return refreshed


class AgentPapersService(PapersService):
    """Exactly one 20-paper selection; old unrelated daily papers stay hidden."""

    replace_sources = frozenset({AGENTS_SOURCE["id"]})

    def __init__(self, path: Path, *, interval_seconds: int = 7200, fetcher=None,
                 clock: Callable[[], float] = time.time, translator=None):
        cache: dict = {}
        super().__init__(path, interval_seconds=interval_seconds, clock=clock, translator=translator,
                         fetcher=fetcher or (lambda url: fetch_agent_papers(url, cache=cache)),
                         sources=(AGENTS_SOURCE,), parser=parse_agent_papers)

    def _prune(self, database, observed):
        stored = database.execute("SELECT items FROM sources WHERE id=?", (AGENTS_SOURCE["id"],)).fetchone()[0]
        items = sorted(json.loads(stored), key=lambda item: item["selection_rank"])[:AGENT_LIMIT]
        for rank, item in enumerate(items):
            item["_sort_at"] = AGENT_LIMIT - rank
        database.execute("UPDATE sources SET items=? WHERE id=?", (json.dumps(items, ensure_ascii=False), AGENTS_SOURCE["id"]))
