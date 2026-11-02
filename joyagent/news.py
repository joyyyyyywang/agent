"""Read-only public AI news collection, cached independently of conversations."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
import urllib.request

SOURCES = (
    {"id": "aibot", "name": "AI 工具集", "url": "https://ai-bot.cn/daily-ai-news/"},
    {"id": "aiagentstore", "name": "AI Agent Store", "url": "https://aiagentstore.ai/ai-agent-news/this-week"},
    {"id": "aihot", "name": "AIhot 热榜", "url": "https://aihot.news/hot"},
)
AIHOT_API = "https://aihot.news/api/v1/hot-topics"
HOT_ITEMS = 10
MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 100
FETCH_TIMEOUT = 15
FETCH_ERROR = "暂时无法获取此来源。"
PARSE_ERROR = "未找到可用资讯。"


def safe_url(value: object, base: str = "") -> str | None:
    """Validate links as data; only fixed SOURCES are ever fetched server-side."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in unquote(value)):
        return None
    try:
        parsed = urlsplit(urljoin(base, value))
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 80, 443}):
            return None
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if "." not in host or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host):
                return None
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                return None
            if any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-") for part in host.split(".")):
                return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    except (ValueError, UnicodeError):
        return None


class _SourceRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (not safe_url(newurl) or new.scheme != "https"
                or new.hostname.removeprefix("www.") != old.hostname.removeprefix("www.")):
            raise ValueError("news redirect left its source")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_html(url: str, *, cache: dict | None = None) -> str:
    if url not in {source["url"] for source in SOURCES}:
        raise ValueError("unknown news source")
    is_hot = url == "https://aihot.news/hot"
    headers = {
        "User-Agent": "JoyAgent-News/1.0", "Accept": "application/json" if is_hot else "text/html,application/xhtml+xml",
        "Accept-Encoding": "identity",
    }
    previous = cache.get(url) if cache is not None else None
    if previous is not None:
        headers["If-None-Match"] = previous[0]
    request = urllib.request.Request(AIHOT_API if is_hot else url, headers=headers)
    deadline = time.monotonic() + FETCH_TIMEOUT
    try:
        response = urllib.request.build_opener(_SourceRedirect()).open(request, timeout=FETCH_TIMEOUT)
    except HTTPError as exc:
        if exc.code == 304 and previous is not None:
            exc.close()
            return previous[1]
        raise
    with response:
        allowed = {"application/json"} if is_hot else {"text/html", "application/xhtml+xml"}
        if response.status != 200 or response.headers.get_content_type() not in allowed:
            raise ValueError("unexpected news response")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ValueError("unexpected compressed response")
        chunks, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("news response deadline exceeded")
            chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("news response exceeds limit")
        data = b"".join(chunks)
        document = data.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
        if cache is not None:
            etag = response.headers.get("ETag")
            if etag and len(etag) <= 1024 and all(32 <= ord(char) < 127 for char in etag):
                cache[url] = (etag, document)
            else:
                cache.pop(url, None)
        return document


class _NewsHTML(HTMLParser):
    """Extract only AI-Bot article headings and Store's public hydration data."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []
        self.anchors: list[dict] = []
        self.anchor: dict | None = None
        self.date_parts: list[str] | None = None
        self.date_depth = 0
        self.label: str | None = None
        self.script_parts: list[str] | None = None
        self.next_data = ""
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 100_000 or len(self.stack) > 4096:
            raise ValueError("news HTML exceeds structural limit")
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "script" and attributes.get("id") == "__NEXT_DATA__":
            self.script_parts = []
        if "news-date" in classes:
            self.date_parts, self.date_depth = [], len(self.stack)
        if tag == "a" and self.anchor is None and any("news-item" in c for _, c in self.stack) and any(t in {"h2", "h3"} for t, _ in self.stack):
            self.anchor = {"title_parts": [], "url": attributes.get("href"), "published_label": self.label}
        if tag not in self.VOID:
            self.stack.append((tag, classes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.script_parts is not None:
            self.script_parts.append(data)
            return
        if any(tag in {"script", "style", "noscript"} for tag, _ in self.stack):
            return
        if self.anchor is not None:
            self.anchor["title_parts"].append(data)
        if self.date_parts is not None:
            self.date_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script_parts is not None:
            self.next_data = "".join(self.script_parts)
            self.script_parts = None
        if tag == "a" and self.anchor is not None:
            self.anchors.append(self.anchor)
            self.anchor = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break
        if self.date_parts is not None and len(self.stack) <= self.date_depth:
            self.label = " ".join("".join(self.date_parts).split())[:60] or None
            self.date_parts = None


def _published(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def parse_news(source_id: str, document: str) -> list[dict]:
    source = next((item for item in SOURCES if item["id"] == source_id), None)
    if source is None or not isinstance(document, str) or len(document.encode("utf-8")) > MAX_BYTES:
        raise ValueError("invalid news document")
    if source_id == "aihot":
        return _parse_hot(source, document)
    parser = _NewsHTML()
    parser.feed(document)
    if source_id == "aibot":
        candidates = [{"title": "".join(row["title_parts"]), "url": row["url"],
                       "published_label": row["published_label"]} for row in parser.anchors]
    else:
        data = json.loads(parser.next_data)
        groups = data.get("props", {}).get("pageProps", {}).get("newsItems", [])
        if not isinstance(groups, list):
            raise ValueError("invalid news items")
        candidates = [row for group in groups if isinstance(group, dict)
                      and isinstance(group.get("sources"), list) for row in group["sources"]]
    result, seen = [], set()
    for row in candidates:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            continue
        title = " ".join(row["title"].split())
        url = safe_url(row.get("url"), source["url"])
        if not url or not 2 <= len(title) <= 500 or url in seen:
            continue
        seen.add(url)
        result.append({"id": hashlib.sha256((source_id + "\0" + url).encode()).hexdigest()[:24],
                       "title": title, "url": url, "source_id": source_id, "source_name": source["name"],
                       "published_at": _published(row.get("publishedDate")),
                       "published_label": row.get("published_label")})
    return result


def _hot_original_url(value: object) -> str | None:
    url = safe_url(value)
    host = urlsplit(url).hostname if url else None
    if host is None or host == "aihot.news" or host.endswith(".aihot.news"):
        return None
    return url


def _parse_hot(source: dict, document: str) -> list[dict]:
    data = json.loads(document)
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("items"), list):
        raise ValueError("invalid hot topics")
    result, ranks, urls = [], set(), set()
    for row in data["items"]:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            continue
        rank, links = row.get("rank"), row.get("links")
        if type(rank) is not int or not 1 <= rank <= HOT_ITEMS or rank in ranks or not isinstance(links, dict):
            continue
        title = " ".join(row["title"].split())
        url = _hot_original_url(links.get("original"))
        if not url or url in urls or not 2 <= len(title) <= 500:
            continue
        ranks.add(rank)
        urls.add(url)
        result.append({"id": hashlib.sha256((source["id"] + "\0" + url).encode()).hexdigest()[:24],
                       "title": title, "url": url, "source_id": source["id"], "source_name": source["name"],
                       "published_at": None, "published_label": None, "hot_rank": rank,
                       "original_url": url,
                       "updated_at": _iso(_timestamp(row.get("latestAt")))})
    if data["items"] and not result:
        raise ValueError("no valid hot topics")
    return sorted(result, key=lambda item: item["hot_rank"])


def _iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _publication_time(item: dict, observed: float) -> float | None:
    """Resolve ordering only; an unqualified source date stays unqualified in UI."""
    timestamp = _timestamp(item.get("published_at"))
    if timestamp is not None:
        return timestamp
    published = _published(item.get("published_at"))
    if published is not None:
        return datetime.fromisoformat(published).replace(tzinfo=timezone.utc).timestamp()
    label = item.get("published_label")
    match = re.match(r"\s*(\d{1,2})月(\d{1,2})(?:日|[·・\s]|$)", label) if isinstance(label, str) else None
    if match:
        # AI-Bot uses Chinese calendar dates without a year. Anchor the inferred
        # year to the first observation, not each refresh (including New Year).
        today = datetime.fromtimestamp(observed, timezone(timedelta(hours=8))).date()
        month, day = map(int, match.groups())
        for year in range(today.year, today.year - 5, -1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate <= today:
                return datetime(candidate.year, month, day, tzinfo=timezone.utc).timestamp()
    return None


def _ordered_items(items: list[dict]) -> list[dict]:
    # Python's stable sort retains each source's newest-first article order on
    # a date tie; source priority follows SOURCES, not network response timing.
    hot = sorted((item for item in items if item["source_id"] == "aihot"), key=lambda item: item["hot_rank"])
    recent = sorted((item for item in items if item["source_id"] != "aihot"), key=lambda item: item["_sort_at"], reverse=True)
    return hot[:HOT_ITEMS] + recent


class NewsService:
    """One scheduler per Web process; SQLite claims prevent duplicate due work."""

    worker_name = "joyagent-news"
    replace_sources = frozenset({"aihot"})

    def __init__(self, path: Path, *, interval_seconds: int = 7200,
                 fetcher: Callable[[str], str] | None = None, clock: Callable[[], float] = time.time,
                 sources: tuple[dict, ...] | None = None,
                 parser: Callable[[str, str], list[dict]] = parse_news):
        self.sources = SOURCES if sources is None else sources
        self.parser = parser
        if type(interval_seconds) is not int or not 60 <= interval_seconds <= 7 * 86400:
            raise ValueError("资讯刷新间隔须为 60–604800 秒。")
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise ValueError("资讯缓存必须是当前用户的普通文件。")
        self._http_cache: dict = {}
        self.interval_seconds, self.clock = interval_seconds, clock
        self.fetcher = fetcher or (lambda url: fetch_html(url, cache=self._http_cache))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refreshing = False
        with self._database() as database:
            database.executescript('''
                CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY CHECK(id=1), last_attempt REAL, next_refresh REAL);
                INSERT OR IGNORE INTO schedule(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, last_attempt REAL, last_success REAL, error TEXT, items TEXT NOT NULL DEFAULT '[]', first_observed REAL);
            ''')
            database.execute("BEGIN IMMEDIATE")
            if "first_observed" not in {row[1] for row in database.execute("PRAGMA table_info(sources)")}:
                database.execute("ALTER TABLE sources ADD COLUMN first_observed REAL")
            now = self.clock()
            added = database.executemany("INSERT OR IGNORE INTO sources(id) VALUES(?)", [(source["id"],) for source in self.sources]).rowcount
            if added:
                # Collect a newly added feed on upgrade instead of showing an
                # empty hot list until the previous sources' next interval.
                database.execute("UPDATE schedule SET next_refresh=? WHERE id=1 AND next_refresh>?", (now, now))
            self._prune(database, now)
        self.path.chmod(0o600)

    @contextmanager
    def _database(self):
        database = sqlite3.connect(self.path, timeout=2)
        try:
            with database:
                yield database
        finally:
            database.close()

    def _prune(self, database: sqlite3.Connection, observed: float) -> None:
        """Keep the current hot list, then newest distinct articles up to 100."""
        rows = []
        for source in self.sources:
            stored, initial, success = database.execute("SELECT items,first_observed,last_success FROM sources WHERE id=?", (source["id"],)).fetchone()
            source_items = json.loads(stored)
            observations = []
            for item in source_items:
                if source["id"] == "aihot":
                    original = _hot_original_url(item.get("original_url"))
                    if original is None:
                        continue
                    # Upgrade cached reading-page links without waiting for the
                    # next scheduled fetch. Never guess a publisher URL.
                    item["url"] = original
                first_seen = _timestamp(item.get("first_seen_at"))
                if first_seen is None:
                    first_seen = _timestamp(item.get("fetched_at"))
                if first_seen is None:
                    first_seen = observed
                observations.append(first_seen)
                item["first_seen_at"] = _iso(first_seen)
                publication = _publication_time(item, first_seen)
                item["_sort_at"] = first_seen if publication is None else publication
                rows.append(item)
            if initial is None and (observations or success is not None):
                initial = min(observations) if observations else success
                database.execute("UPDATE sources SET first_observed=? WHERE id=?", (initial, source["id"]))
        retained, urls = [], set()
        for item in _ordered_items(rows):
            links = {item["url"]}
            if item.get("original_url"):
                links.add(item["original_url"])
            if links & urls:
                continue
            urls.update(links)
            retained.append(item)
            if len(retained) == MAX_ITEMS:
                break
        for source in self.sources:
            items = [item for item in retained if item["source_id"] == source["id"]]
            database.execute("UPDATE sources SET items=? WHERE id=?",
                             (json.dumps(items, ensure_ascii=False), source["id"]))

    def start(self) -> None:
        with self._lock:
            if self._stop.is_set() or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name=self.worker_name, daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
                with self._database() as database:
                    next_refresh = database.execute("SELECT next_refresh FROM schedule WHERE id=1").fetchone()[0]
                delay = max(1, (next_refresh or self.clock() + 60) - self.clock())
            except Exception:
                # Cache contention must not create a busy retry loop or kill Web.
                delay = 60
            self._stop.wait(min(delay, self.interval_seconds))

    def refresh(self) -> bool:
        """Claim a due collection internally; browser reads never call this."""
        with self._lock:
            if self._refreshing or self._stop.is_set():
                return False
            now = self.clock()
            with self._database() as database:
                database.execute("BEGIN IMMEDIATE")
                next_refresh = database.execute("SELECT next_refresh FROM schedule WHERE id=1").fetchone()[0]
                if next_refresh is not None and next_refresh > now:
                    return False
                database.execute("UPDATE schedule SET last_attempt=?,next_refresh=? WHERE id=1", (now, now + self.interval_seconds))
            self._refreshing = True
        try:
            outcomes = []
            for source in self.sources:
                if self._stop.is_set():
                    break
                attempted = self.clock()
                items, error = [], None
                try:
                    document = self.fetcher(source["url"])
                except Exception:
                    error = FETCH_ERROR
                else:
                    try:
                        items = self.parser(source["id"], document)
                        if not items and source["id"] != "aihot":
                            error = PARSE_ERROR
                    except Exception:
                        error = PARSE_ERROR
                if self._stop.is_set():
                    break
                completed = self.clock()
                outcomes.append((source["id"], attempted, completed, items, error))
            if not self._stop.is_set():
                with self._database() as database:
                    database.execute("BEGIN IMMEDIATE")
                    for source_id, attempted, completed, items, error in outcomes:
                        if error:
                            database.execute("UPDATE sources SET last_attempt=?,error=? WHERE id=?", (attempted, error, source_id))
                        else:
                            stored, initial = database.execute("SELECT items,first_observed FROM sources WHERE id=?", (source_id,)).fetchone()
                            old = json.loads(stored)
                            known = {item["url"]: item for item in old}
                            baseline = completed if initial is None else initial
                            has_anchor = any(item["url"] in known for item in items)
                            previous_anchor = None
                            for item in items:
                                previous = known.get(item["url"])
                                if previous is not None:
                                    seen = previous.get("first_seen_at", previous.get("fetched_at", _iso(baseline)))
                                    previous_anchor = seen
                                elif _publication_time(item, completed) is not None:
                                    seen = _iso(completed)
                                else:
                                    # An undated article below a retained entry, or
                                    # from a source whose entries were all pruned,
                                    # must not become "new" merely by reappearing.
                                    seen = previous_anchor or _iso(completed if has_anchor else baseline)
                                item["first_seen_at"] = seen
                                item["fetched_at"] = _iso(completed)
                            if source_id not in self.replace_sources:
                                # A source may expose only today's articles. Keep
                                # yesterday's cached entries to fill the rolling
                                # total; a hot ranking instead replaces its list.
                                incoming = {item["url"] for item in items}
                                items.extend(item for item in old if item["url"] not in incoming)
                            database.execute("UPDATE sources SET last_attempt=?,last_success=?,error=NULL,items=?,first_observed=COALESCE(first_observed,?) WHERE id=?",
                                             (attempted, completed, json.dumps(items, ensure_ascii=False), completed, source_id))
                    self._prune(database, self.clock())
            return True
        finally:
            with self._lock:
                self._refreshing = False

    def snapshot(self) -> dict:
        with self._lock, self._database() as database:
            last, upcoming = database.execute("SELECT last_attempt,next_refresh FROM schedule WHERE id=1").fetchone()
            result = {"enabled": True, "interval_seconds": self.interval_seconds,
                      "refreshing": self._refreshing, "last_attempt_at": _iso(last),
                      "next_refresh_at": _iso(upcoming), "sources": [], "items": []}
            for source in self.sources:
                attempt, success, error, stored = database.execute(
                    "SELECT last_attempt,last_success,error,items FROM sources WHERE id=?", (source["id"],)).fetchone()
                items = json.loads(stored)
                result["sources"].append({**source, "last_attempt_at": _iso(attempt),
                                          "last_success_at": _iso(success), "error": error, "item_count": len(items)})
                result["items"].extend(items)
            result["items"] = [{key: value for key, value in item.items() if not key.startswith("_")}
                               for item in _ordered_items(result["items"])]
            return result
