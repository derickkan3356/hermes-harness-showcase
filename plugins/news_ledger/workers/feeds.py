"""Pulling the source table and the keys a story is recognised by.

Shared by the `news.py` worker and by `evals/feeds/feeds_eval.py`, so the eval
measures the code the digest runs. Imports feedparser; the caller's PEP 723
header provides it. The bytes come from the fetch cascade's HTTP layer
(`plugins/fetch_cascade/workers/fetch_http.py`, `format: "raw"`), run as its own
process: getting them is shared with `web_extract`, reading them is this file's.

Every parser returns rows of `title, url, body, published, summary`. `body` is
the item's full text with its links, kept only long enough for `keys()` to read
the links out of it.
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import feedparser

from keys import keys, keys_in_text, title_key, url_key

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "fetch_cascade" / "workers"))
from raw_client import fetch_raw, unset_proxy  # noqa: E402,F401 — unset_proxy is re-exported

TIMEOUT = 30.0

# --- body text ----------------------------------------------------------------

_BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "tr", "div", "br", "hr"}


class _Blocks(HTMLParser):
    """An item's body as paragraphs, each with the links inside it.

    `news_read` returns a long roundup's paragraphs about one story rather than
    its first few thousand characters, and a paragraph is found by the links in
    it — which stripping the HTML would lose.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._text: list[str] = []
        self._links: list[str] = []

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._text)).strip()
        if text:
            self.blocks.append({"text": text, "links": self._links})
        self._text, self._links = [], []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._flush()
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._links.append(href)

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        self._text.append(data)


def blocks(body: str) -> list[dict]:
    """Paragraphs of an item's body, each with its match keys."""
    if not body:
        return []
    if "<" not in body:  # an abstract: plain text, one paragraph
        return [{"text": re.sub(r"\s+", " ", body).strip(), "keys": sorted(keys_in_text(body))}]
    parser = _Blocks()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        return [{"text": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip(), "keys": []}]
    parser._flush()
    return [{"text": b["text"], "keys": sorted(keys_in_text(" ".join(b["links"]) + " " + b["text"]))}
            for b in parser.blocks]


# --- pullers ------------------------------------------------------------------
#
# A source kind is two halves: which URLs to get, and how to read what came
# back. Getting the bytes is the fetch cascade's HTTP layer (`fetch()` below);
# nothing here opens a connection.

def iso(value) -> str | None:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def with_query(url: str, params: dict) -> str:
    return f"{url}?{urlencode(params)}"


def urls_feed(src: dict) -> list[str]:
    return [src["url"]]


def parse_feed(src: dict, bodies: list[bytes]) -> list[dict]:
    label = src.get("label", "")
    out = []
    for e in feedparser.parse(bodies[0]).entries:
        body = " ".join([e.get("summary", "")] + [c.get("value", "") for c in e.get("content", [])])
        title = (e.get("title") or "").strip()
        if label and label.lower() not in title.lower():
            title = f"{label} {title}"
        out.append({
            "title": title,
            "url": (e.get("link") or "").strip(),
            "body": body,
            "published": iso(e.get("published_parsed") or e.get("updated_parsed")),
            "summary": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("summary", "")))[:1000].strip(),
        })
    return out


def urls_algolia(src: dict) -> list[str]:
    return [with_query("https://hn.algolia.com/api/v1/search_by_date", {
        "query": src["query"],
        "tags": "story",
        "numericFilters": f"points>{src.get('min_points', 50)}",
        "hitsPerPage": src.get("limit", 50),
    })]


def parse_algolia(src: dict, bodies: list[bytes]) -> list[dict]:
    out = []
    for h in json.loads(bodies[0]).get("hits", []):
        out.append({
            "title": (h.get("title") or "").strip(),
            "url": (h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}").strip(),
            "body": h.get("story_text") or "",
            "published": h.get("created_at"),
            "summary": f"Hacker News: {h.get('points')} points, {h.get('num_comments')} comments",
        })
    return out


def parse_hf(src: dict, bodies: list[bytes]) -> list[dict]:
    out = []
    for row in json.loads(bodies[0]):
        p = row.get("paper") or {}
        abstract = re.sub(r"\s+", " ", p.get("summary") or "").strip()
        out.append({
            "title": (p.get("title") or "").strip(),
            "url": f"https://huggingface.co/papers/{p.get('id')}",
            "body": abstract,
            "published": row.get("publishedAt") or p.get("publishedAt"),
            "summary": abstract[:1000],
        })
    return out


def urls_hf_models(src: dict) -> list[str]:
    return [with_query("https://huggingface.co/api/models", {
        "author": author, "sort": "createdAt", "direction": -1, "limit": src.get("limit", 20),
    }) for author in src["authors"]]


def parse_hf_models(src: dict, bodies: list[bytes]) -> list[dict]:
    """A lab's own releases, straight from HuggingFace, for labs that publish no feed.

    First-party and keyless, with `createdAt` as the timestamp and downloads and
    likes as a free ranking signal. It sees only what ships weights, so it covers
    the open-weight labs and says nothing about a closed one.
    """
    out = []
    for body in bodies:
        for m in json.loads(body):
            out.append({
                "title": f"{m['id']} — model release",
                "url": f"https://huggingface.co/{m['id']}",
                "body": "",
                "published": m.get("createdAt"),
                "summary": f"{m.get('downloads', 0)} downloads, {m.get('likes', 0)} likes",
            })
    return out


def urls_gnews(src: dict) -> list[str]:
    return [with_query("https://news.google.com/rss/search", {
        "q": f"site:{src['site']} when:{src.get('window', '7d')}",
        "hl": "en-US", "gl": "US", "ceid": "US:en",
    })]


def parse_gnews(src: dict, bodies: list[bytes]) -> list[dict]:
    """Google News restricted to one site, standing in for a site that has no feed.

    It carries the headline and the publish date. The link is a Google-encoded
    redirect: a browser follows it to the article, plain HTTP gets Google's own
    page, and the article's URL is not in it — so it cannot be keyed on for
    dedup. Sources of this kind are marked `opaque_url`.
    """
    out = []
    for e in feedparser.parse(bodies[0]).entries:
        title = (e.get("title") or "").strip()
        publisher = ""
        if " - " in title:
            title, _, publisher = title.rpartition(" - ")
        out.append({
            "title": title.strip(),
            "url": (e.get("link") or "").strip(),
            "body": "",
            "published": iso(e.get("published_parsed")),
            "summary": f"via Google News, published by {publisher or src['site']}",
        })
    return out


def urls_arxiv_query(src: dict) -> list[str]:
    return [with_query("https://export.arxiv.org/api/query", {
        "search_query": src["query"], "sortBy": "submittedDate",
        "sortOrder": "descending", "max_results": src.get("limit", 50),
    })]


def parse_arxiv_query(src: dict, bodies: list[bytes]) -> list[dict]:
    """arXiv by topic rather than by category.

    A category feed is the wrong subscription for a bucket defined by topics:
    cs.CL announces about 355 papers a day, and two of them are about anything
    the digest covers. The same keyless API answers a field query instead, and
    its per-paper `published` is the real submission date, where every item in a
    day's category RSS carries one announcement timestamp.
    """
    out = []
    for e in feedparser.parse(bodies[0]).entries:
        abstract = re.sub(r"\s+", " ", e.get("summary", "")).strip()
        out.append({
            "title": re.sub(r"\s+", " ", e.get("title", "")).strip(),
            "url": (e.get("link") or "").strip(),
            "body": abstract,
            "published": iso(e.get("published_parsed")),
            "summary": abstract[:1000],
        })
    return out


KINDS = {
    "feed": (urls_feed, parse_feed),
    "algolia": (urls_algolia, parse_algolia),
    "hf": (urls_feed, parse_hf),
    "hf_models": (urls_hf_models, parse_hf_models),
    "gnews": (urls_gnews, parse_gnews),
    "arxiv_query": (urls_arxiv_query, parse_arxiv_query),
}


# --- fetching -----------------------------------------------------------------

def fetch(urls: list[str], concurrency: int = 1) -> dict[str, dict]:
    """Raw bytes for each URL from the fetch cascade's HTTP layer, keyed by URL.

    Each row has `body` (bytes) or `error`. A worker that cannot run at all
    fails every URL with its reason, so a broken transport shows up as source
    errors in the report rather than as a quiet empty day.
    """
    return {r["url"]: ({"error": r["error"]} if "error" in r else {"body": r["body"]})
            for r in fetch_raw(urls, concurrency, TIMEOUT)}


def family(src: dict, url: str = "") -> str:
    """Sources that share a publisher are one witness, not several.

    HuggingFace uploads count per uploader: NVIDIA quantizing a GLM release is
    NVIDIA deciding it was worth the work, a second witness to the first.
    """
    if src["kind"] == "hf_models" and "huggingface.co/" in url:
        return f"hf:{url.split('huggingface.co/')[1].split('/')[0].lower()}"
    return src.get("family", src["id"])


def _pull_one(src: dict, fetched: list[dict], fetched_at: str) -> tuple[dict, list[dict]]:
    row = {"id": src["id"], "bucket": src["bucket"], "kind": src["kind"]}
    failed = next((f["error"] for f in fetched if "error" in f), None)
    if failed:
        return row | {"status": "error", "error": failed, "items": 0}, []
    try:
        raw = KINDS[src["kind"]][1](src, [f["body"] for f in fetched])
    except Exception as exc:
        return row | {"status": "error", "error": f"{type(exc).__name__}: {exc}", "items": 0}, []
    kept = []
    for it in raw:
        if not it["url"] or not it["title"]:
            continue
        opaque = bool(src.get("opaque_url"))
        body = it.pop("body", "")
        primary, mentioned = keys("" if opaque else it["url"], body)
        it["blocks"] = blocks(body)
        it["body_chars"] = sum(len(b["text"]) for b in it["blocks"])
        it |= {
            "source": src["id"],
            "family": family(src, it["url"]),
            "bucket": src["bucket"],
            "kind": src["kind"],
            "opaque_url": opaque,
            "control": bool(src.get("control")),
            "fetched_at": fetched_at,
            "url_key": "" if opaque else url_key(it["url"]),
            "title_key": title_key(it["title"]),
            "primary": primary,
            "mentioned": mentioned,
        }
        kept.append(it)
    dated = sum(1 for it in kept if it["published"])
    return row | {
        "status": "ok",
        "items": len(kept),
        "dated": dated,
        "newest": max((it["published"] for it in kept if it["published"]), default=None),
    }, kept


def _fetch_paced(urls: list[str]) -> dict[str, dict]:
    """One URL at a time, three seconds apart: arXiv asks for one call every three seconds."""
    out = {}
    for url in urls:
        time.sleep(3)
        out |= fetch([url])
    return out


def pull_all(sources: list[dict], workers: int = 8) -> tuple[list[dict], list[dict]]:
    """Every source, `workers` URLs at a time, except arXiv, which is paced."""
    fetched_at = datetime.now(timezone.utc).isoformat()
    wanted = {s["id"]: KINDS[s["kind"]][0](s) if s["kind"] in KINDS else [] for s in sources}
    paced = [u for s in sources if s["kind"] == "arxiv_query" for u in wanted[s["id"]]]
    batch = [u for s in sources if s["kind"] != "arxiv_query" for u in wanted[s["id"]]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        batched = pool.submit(fetch, batch, workers)
        spaced = pool.submit(_fetch_paced, paced)
        fetched = batched.result() | spaced.result()
    results = []
    for s in sources:
        if s["kind"] not in KINDS:
            results.append(({"id": s["id"], "bucket": s["bucket"], "kind": s["kind"], "status": "error",
                             "error": f"unknown source kind {s['kind']!r}", "items": 0}, []))
            continue
        results.append(_pull_one(s, [fetched[u] for u in wanted[s["id"]]], fetched_at))
    report = [r for r, _ in results]
    items = [it for _, kept in results for it in kept]
    return items, report
