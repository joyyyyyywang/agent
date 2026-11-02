"""Agent selection uses fake HF documents; never call a network or model API."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from joyagent.papers import (AGENTS_API, AGENTS_SLUG, AGENTS_SOURCE, PAPERS_API,
                             AgentPapersService, PapersService, _fetch_paper_json,
                             fetch_agent_papers, parse_agent_papers)


def detail(index, *, title=None):
    return {"id": f"2610.{10000 + index}", "title": title or f"Agent Research {index}",
            "summary": f"Abstract {index}.", "publishedAt": "2026-10-30T12:00:00Z",
            "submittedOnDailyAt": "2026-11-02T00:00:00Z"}


def selection(start=0):
    return json.dumps({"kind": "agent-selection-v1", "items": [
        {"paper": detail(index), "selection_source": "collection" if index - start < 13 else "daily"}
        for index in range(start, start + 20)]})


class AgentPapersTests(unittest.TestCase):
    def test_collection_has_priority_and_agent_only_supplements_fill_exactly_twenty(self):
        collection = {"slug": AGENTS_SLUG, "private": False, "items": [
            *({**detail(i), "type": "paper"} for i in range(13)),
            {**detail(0), "type": "paper"}, {"id": "../../secrets", "type": "paper"},
            {"id": "owner/model", "type": "model"},
        ]}
        daily = [{"paper": detail(0)}, {"paper": detail(50, title="Unrelated Image Generation")},
                 *({"paper": detail(i)} for i in range(13, 33))]
        calls = []
        def fetch(url, *, cache=None):
            calls.append(url)
            if url == AGENTS_API:
                return json.dumps(collection)
            if url == PAPERS_API:
                return json.dumps(daily)
            identifier = url.rsplit("/", 1)[-1]
            return json.dumps(detail(int(identifier.split(".")[1]) - 10000))
        with patch("joyagent.papers._fetch_paper_json", side_effect=fetch):
            items = parse_agent_papers("agents", fetch_agent_papers(AGENTS_SOURCE["url"]))
        self.assertEqual(len(items), 20)
        self.assertEqual([row["arxiv_id"] for row in items], [detail(i)["id"] for i in range(20)])
        self.assertEqual([row["selection_source"] for row in items], ["collection"] * 13 + ["daily"] * 7)
        self.assertEqual(len(calls), 15)
        self.assertEqual(calls[:2], [AGENTS_API, PAPERS_API])
        self.assertTrue(all(url.startswith("https://huggingface.co/api/papers/") for url in calls[2:]))
        self.assertEqual(items[0]["summary"], "Abstract 0.")

    def test_full_collection_does_not_fetch_supplements_or_other_collections(self):
        collection = {"slug": AGENTS_SLUG, "private": False, "items": [{**detail(i), "type": "paper"} for i in range(30)]}
        def fetch(url, *, cache=None):
            if url == AGENTS_API:
                return json.dumps(collection)
            self.assertNotEqual(url, PAPERS_API)
            return json.dumps(detail(int(url.rsplit(".", 1)[-1]) - 10000))
        with patch("joyagent.papers._fetch_paper_json", side_effect=fetch) as request:
            self.assertEqual(len(parse_agent_papers("agents", fetch_agent_papers(AGENTS_SOURCE["url"]))), 20)
        self.assertEqual(request.call_count, 21)

    def test_source_identity_malformed_incomplete_and_untrusted_urls_fail_closed(self):
        for value in ({"slug": "wrong", "private": False, "items": []},
                      {"slug": AGENTS_SLUG, "private": True, "items": []}, []):
            with self.subTest(value=value), patch("joyagent.papers._fetch_paper_json", return_value=json.dumps(value)):
                with self.assertRaises(ValueError):
                    fetch_agent_papers(AGENTS_SOURCE["url"])
        with patch("joyagent.papers._fetch_paper_json", side_effect=[
                json.dumps({"slug": AGENTS_SLUG, "private": False, "items": []}), "[]"]):
            with self.assertRaises(ValueError):
                fetch_agent_papers(AGENTS_SOURCE["url"])
        with patch("joyagent.papers.urllib.request.build_opener") as network:
            for url in ("https://untrusted.example/", "https://huggingface.co/api/papers/../../secrets",
                        "https://huggingface.co/api/papers/2610.12345?url=http://127.0.0.1",
                        "https://huggingface.co/collections/zzfive/safety"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    _fetch_paper_json(url)
            network.assert_not_called()

    def test_parser_requires_twenty_distinct_safe_papers_and_preserves_english(self):
        value = json.loads(selection())
        items = parse_agent_papers("agents", json.dumps(value))
        self.assertEqual(items[0]["title"], "Agent Research 0")
        self.assertEqual(items[0]["paper_published_at"], "2026-10-30T12:00:00Z")
        for field, invalid in (("id", "../../bad"), ("publishedAt", "invalid"), ("title", ""), ("summary", {})):
            altered = json.loads(selection())
            altered["items"][0]["paper"][field] = invalid
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_agent_papers("agents", json.dumps(altered))
        for rows in (value["items"][:19], value["items"] + [value["items"][0]], [value["items"][0]] * 20):
            with self.assertRaises(ValueError):
                parse_agent_papers("agents", json.dumps({**value, "items": rows}))
        value["items"][-1]["paper"]["title"] = "An unrelated paper"
        with self.assertRaises(ValueError):
            parse_agent_papers("agents", json.dumps(value))

    def test_upgrade_hides_old_daily_papers_and_replaces_selection_without_history_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "papers.sqlite3"
            now = [0.0]
            original = PapersService(path, fetcher=lambda url: json.dumps([{"paper": detail(80)}]), clock=lambda: now[0])
            original.refresh()
            original.close()
            documents = [selection()]
            service = AgentPapersService(path, fetcher=lambda url: documents[0], clock=lambda: now[0])
            self.assertEqual(service.snapshot()["items"], [])
            self.assertTrue(service.refresh(), "new collection must not wait for the old schedule")
            first = service.snapshot()
            self.assertEqual(len(first["items"]), 20)
            self.assertEqual([row["selection_rank"] for row in first["items"]], list(range(20)))
            now[0] += 7200
            documents[0] = selection(20)
            service.refresh()
            second = service.snapshot()
            self.assertTrue({row["id"] for row in first["items"]}.isdisjoint(row["id"] for row in second["items"]))
            self.assertEqual(len(second["items"]), 20)
            documents[0] = "unavailable"
            now[0] += 7200
            service.refresh()
            self.assertEqual(service.snapshot()["items"], second["items"])
            self.assertTrue(service.snapshot()["sources"][0]["error"])
            service.close()
            restarted = AgentPapersService(path, fetcher=lambda url: self.fail("snapshot must not fetch"), clock=lambda: now[0])
            self.assertFalse(restarted.refresh())
            self.assertEqual(restarted.snapshot()["items"], second["items"])
            restarted.close()


if __name__ == "__main__":
    unittest.main()
