# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml",
#     "feedparser>=6.0",
#     "fastembed>=0.4",
#     "numpy",
# ]
# ///
"""The news-ledger worker: candidates for today's digest, and the record of what was posted.

Runs as its own process under `uv run --script`, so feedparser and
the embedding model never enter Hermes' venv. `news.py.lock` pins the
environment.

Protocol: one JSON object on stdin, one on stdout.

    in   {"op": "candidates", "buckets": [str] | null, "hours_back": int | null, "limit": int}
    out  {"window": {"since", "until"}, "stories": [...], "posted_recently": [...], "errors": [...]}

    in   {"op": "read", "story_id": str}
    out  {"id", "title", "texts": [{"source", "url", "excerpt", "text"}], "note"?}

    in   {"op": "record_post", "post": str}
    out  {"recorded": [title], "not_in_post": int, "high_water": str | null}

Everything the digest must not get wrong is decided here, not by the model:
which period a run covers, which items are one story, and what has been posted.
The model reads the candidates, decides what is worth writing about, and writes.
What it wrote is recorded afterwards from the post itself — the plugin's
`post_llm_call` hook sends the finished text here, and a story counts as posted
when one of its links or artifacts appears in it. Asked to record its own
choices, the 27B recorded before writing and got the list wrong twice in two
runs: once a story it then left out, once one it wrote but did not list.

The ledger is SQLite at `~/.hermes/news_ledger.db` (`NEWS_LEDGER_DB` overrides):
runtime state, not tracked.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import feeds
import stories as S
from keys import artifacts, keys_in_text

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
DB = Path(os.environ.get("NEWS_LEDGER_DB", Path.home() / ".hermes" / "news_ledger.db"))
EMBED_CACHE = Path.home() / ".hermes" / "cache" / "fastembed"
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

WINDOW_FLOOR = timedelta(hours=24)
WINDOW_CAP = timedelta(days=7)
STALE = timedelta(days=7)
RECENT = timedelta(days=14)
HKT = timezone(timedelta(hours=8))  # the digest is dated for its reader, not in UTC


# --- ledger -------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS offered (
    story_id TEXT PRIMARY KEY, title TEXT, parts TEXT,
    first_offered TEXT, last_offered TEXT, times INTEGER);
CREATE TABLE IF NOT EXISTS posted (story_id TEXT PRIMARY KEY, title TEXT, posted_at TEXT, models TEXT);
CREATE TABLE IF NOT EXISTS posted_keys (key TEXT PRIMARY KEY, story_id TEXT, posted_at TEXT);
CREATE TABLE IF NOT EXISTS model_names (name TEXT PRIMARY KEY, last_seen TEXT);
CREATE TABLE IF NOT EXISTS story_text (
    story_id TEXT, pos INTEGER, source TEXT, url TEXT, excerpt INTEGER, text TEXT,
    PRIMARY KEY (story_id, pos));
CREATE TABLE IF NOT EXISTS first_seen (key TEXT PRIMARY KEY, source TEXT, seen_at TEXT);
"""


def ledger() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    # A ledger made before `parts` and `models` existed gets them here; a
    # posted story's models are filled from the model keys it was recorded with.
    for table, col in (("offered", "parts"), ("posted", "models")):
        if col not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            if col == "models":
                for sid, in db.execute("SELECT story_id FROM posted").fetchall():
                    names = [k[6:] for (k,) in db.execute(
                        "SELECT key FROM posted_keys WHERE story_id = ? AND key LIKE 'model:%'", (sid,))]
                    db.execute("UPDATE posted SET models = ? WHERE story_id = ?", (json.dumps(sorted(names)), sid))
            db.commit()
    return db


def meta(db: sqlite3.Connection, k: str) -> str | None:
    row = db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, k: str, v: str) -> None:
    db.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


def blocking(keys: list[str]) -> list[str]:
    """The keys that mark an item as posted: everything but a model name.

    A model name joins the reports of one launch within a run, but it names a
    subject, not an event: "GPT-6.1 Astra scrapped" and "Introducing GPT-6.1
    Sol" a day later share `model:gpt-6.1` and are two stories. Whether a new
    development on a model already covered is news is the model's call, made
    against `posted_recently`; the `models` on both sides are its clue.
    """
    return [k for k in keys if not k.startswith("model:")]


def already_posted(db: sqlite3.Connection, keys: list[str]) -> bool:
    """Whether an item was cited in a post. Asked per item, before merging: a
    merged story holds items the post did not cite, and those stay open."""
    return any(db.execute("SELECT 1 FROM posted_keys WHERE key = ?", (k,)).fetchone()
               for k in blocking(keys))


def models(keys: list[str]) -> list[str]:
    return sorted(k.split(":", 1)[1] for k in keys if k.startswith("model:"))


# --- relevance ----------------------------------------------------------------

def relevance(rows: list[dict], topics: list[dict], errors: list[str]) -> None:
    """Cosine similarity of each story to the closest written topic description.

    It answers whether a story is on the digest's subject, not whether it
    matters. What filters on it is `passes`.
    """
    if not rows or not topics:
        return
    try:
        import numpy as np
        from fastembed import TextEmbedding

        model = TextEmbedding(EMBED_MODEL, cache_dir=str(EMBED_CACHE))
        docs = np.array(list(model.embed([f"{r['title']}. {r['summary']}" for r in rows])))
        tops = np.array(list(model.embed([t["description"] for t in topics])))
    except Exception as exc:  # the digest still runs, ranked on corroboration alone
        errors.append(f"relevance: {type(exc).__name__}: {exc}")
        return
    sims = docs @ tops.T
    for r, row in zip(rows, sims):
        j = int(row.argmax())
        r["relevance"] = round(float(row[j]), 3)
        r["topic"] = topics[j]["name"]


def passes(story: dict, floors: dict[str, float]) -> bool:
    """Whether a story clears the `min_relevance` of the sources that carry it.

    The floor belongs to a source, not a topic: a wide-net source (Techmeme,
    Hacker News) carries much that is not the digest's subject, while a lab feed
    or a release feed is on subject by construction and its titles — `v2.130.0`,
    `Science` — score low for lack of words, not of relevance. A story carried by
    any source without a floor passes, and otherwise needs only the lowest floor
    among its sources.
    """
    if story.get("relevance") is None:
        return True
    mins = [floors.get(src) for src in story["sources"]]
    return None in mins or story["relevance"] >= min(mins)


# --- the text news_read returns ------------------------------------------------

PER_ITEM = 6000
PER_STORY = 12000
MIN_TEXT = 300  # less than this adds nothing the summary did not say


def readable(story: dict) -> list[dict]:
    """What the feeds already gave for a story, so reading it needs no fetch.

    A short item is returned whole. A long one — a newsletter covering forty
    things — is cut to the paragraphs about this story: those that link one of
    its links or artifacts, or name its model, each with the paragraph after it
    for context. A long item nothing in which can be found is returned from the
    top only when it is one of the story's own items; a roundup that merely
    mentions the story is then left out.
    """
    match = set(story["match"])
    names = [k.split(":", 1)[1] for k in story["keys"] if k.startswith("model:")]
    out, total = [], 0
    for it in story["_readers"]:
        bl = it.get("blocks") or []
        full = "\n\n".join(b["text"] for b in bl)
        if len(full) < MIN_TEXT:
            continue
        own = id(it) in story["_members"]
        if len(full) <= PER_ITEM:
            text, excerpt = full, False
        else:
            hits = [i for i, b in enumerate(bl)
                    if match & set(b["keys"]) or any(f"-{n}-" in f"-{S._slug(b['text'])}-" for n in names)]
            if hits:
                keep = sorted({j for i in hits for j in (i, i + 1) if j < len(bl)})
                text, excerpt = " … ".join(bl[j]["text"] for j in keep)[:PER_ITEM], True
            elif own:
                text, excerpt = full[:PER_ITEM], True
            else:
                continue
        if not excerpt and len(text) <= len(story["summary"]) + MIN_TEXT:
            continue  # the summary already carries it
        text = text[:PER_STORY - total]
        if len(text) < MIN_TEXT:
            break
        out.append({"source": it["source"], "url": it["url"], "excerpt": excerpt, "text": text})
        total += len(text)
    return out


def read(req: dict) -> dict:
    db = ledger()
    sid = req.get("story_id") or ""
    row = db.execute("SELECT title FROM offered WHERE story_id = ?", (sid,)).fetchone()
    if not row:
        return {"error": f"no story {sid!r} was offered: use an id from news_candidates"}
    texts = [{"source": s, "url": u, "excerpt": bool(e), "text": t} for s, u, e, t in db.execute(
        "SELECT source, url, excerpt, text FROM story_text WHERE story_id = ? ORDER BY pos", (sid,))]
    out = {"id": sid, "title": row[0], "texts": texts}
    if not texts:
        out["note"] = "The feeds carried no text for this story beyond its summary. Open one of its urls with web_extract."
    return out


# --- ops ----------------------------------------------------------------------

def load_sources(buckets: list[str] | None) -> list[dict]:
    sources = [s for s in yaml.safe_load((PLUGIN / "sources.yaml").read_text()) if not s.get("control")]
    return [s for s in sources if not buckets or s["bucket"] in buckets]


def latest_only(items: list[dict], sources: set[str]) -> list[dict]:
    newest: dict[str, dict] = {}
    rest = []
    for it in items:
        if it["source"] not in sources:
            rest.append(it)
        elif it["source"] not in newest or (it["published"] or "") > (newest[it["source"]]["published"] or ""):
            newest[it["source"]] = it
    return rest + list(newest.values())


def in_period(db: sqlite3.Connection, items: list[dict], late: set[str],
              since: datetime, now: datetime) -> list[dict]:
    """The items of this run's period.

    An item is in it by its `published`, except from a `dated_by: first_seen`
    source, whose date is earlier than the day it can first be read: arXiv dates
    a paper by its submission and shows it at the next announcement, half a day
    to four days later, so a window on submission dates never holds some
    papers. Those are in by when the ledger first saw them, and one submitted
    more than `STALE` before that is a revised old paper, not news. A source
    with nothing recorded yet is a backlog: its items count as seen when
    published, so a first run does not offer a week of papers as new.
    """
    fresh = {s for s in late if not db.execute("SELECT 1 FROM first_seen WHERE source = ?", (s,)).fetchone()}
    out = []
    for it in items:
        if it["source"] not in late:
            if S.in_window(it, since, now):
                out.append(it)
            continue
        key = min(artifacts(it["url"]), default=it["url"])
        published = S.parse_time(it["published"])
        row = db.execute("SELECT seen_at FROM first_seen WHERE key = ?", (key,)).fetchone()
        if row:
            seen = S.parse_time(row[0])
        else:
            seen = published if it["source"] in fresh and published else now
            db.execute("INSERT INTO first_seen (key, source, seen_at) VALUES (?, ?, ?)",
                       (key, it["source"], seen.isoformat()))
        if since < seen <= now and (published is None or seen - published <= STALE):
            out.append(it)
    return out


def candidates(req: dict) -> dict:
    now = datetime.now(timezone.utc)
    db = ledger()
    if req.get("hours_back"):
        since = now - timedelta(hours=int(req["hours_back"]))
    else:
        hw = S.parse_time(meta(db, "high_water"))
        since = max(now - WINDOW_CAP, min(hw or now - WINDOW_FLOOR, now - WINDOW_FLOOR))

    feeds.unset_proxy()
    items, report = feeds.pull_all(load_sources(req.get("buckets")))
    errors = [f"{r['id']}: {r['error']}" for r in report if r["status"] == "error"]
    late = {s["id"] for s in load_sources(None) if s.get("dated_by") == "first_seen"}
    items = latest_only(in_period(db, items, late, since, now),
                        {s["id"] for s in load_sources(None) if s.get("latest_only")})

    known = {n for (n,) in db.execute(
        "SELECT name FROM model_names WHERE last_seen >= ?", ((now - timedelta(days=60)).isoformat(),))}
    names = S.vocabulary(items, known)
    items = [it for it in items if not already_posted(db, S.merge_keys(it, names))]
    stories = S.build_stories(items, known)

    topics = yaml.safe_load((PLUGIN / "topics.yaml").read_text())
    relevance(stories, topics, errors)
    floors = {s["id"]: s["min_relevance"] for s in load_sources(None) if "min_relevance" in s}
    chosen = S.select([s for s in stories if passes(s, floors)], int(req.get("limit") or 30))

    stamp = now.isoformat()
    for s in chosen:
        db.execute(
            """INSERT INTO offered (story_id, title, parts, first_offered, last_offered, times)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(story_id) DO UPDATE SET last_offered = excluded.last_offered,
                   parts = excluded.parts, title = excluded.title, times = times + 1""",
            (s["id"], s["title"], json.dumps(s["parts"]), stamp, stamp))
    for s in chosen:
        db.execute("DELETE FROM story_text WHERE story_id = ?", (s["id"],))
        s["texts"] = readable(s)
        for pos, t in enumerate(s["texts"]):
            db.execute("INSERT INTO story_text (story_id, pos, source, url, excerpt, text) VALUES (?, ?, ?, ?, ?, ?)",
                       (s["id"], pos, t["source"], t["url"], int(t["excerpt"]), t["text"]))
    for n in S.vocabulary(items):
        db.execute("INSERT INTO model_names (name, last_seen) VALUES (?, ?) "
                   "ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen", (n, stamp))
    set_meta(db, "pending_high_water", stamp)
    db.commit()

    recent = [{"title": t, "posted": datetime.fromisoformat(at).astimezone(HKT).date().isoformat(),
               "models": json.loads(m or "[]")}
              for t, at, m in db.execute("SELECT title, posted_at, models FROM posted WHERE posted_at >= ? "
                                         "ORDER BY posted_at DESC", ((now - RECENT).isoformat(),))]
    return {
        "window": {"since": since.isoformat(timespec="minutes"), "until": now.isoformat(timespec="minutes"),
                   "date_hkt": now.astimezone(HKT).date().isoformat()},
        "stories": [_row(s) for s in chosen],
        "considered": {"items": len(items), "stories": len(stories)},
        "posted_recently": recent,
        "errors": errors,
    }


def _row(s: dict) -> dict:
    """What the model reads: enough to choose, nothing it has to recompute."""
    return {
        "id": s["id"],
        "title": s["title"],
        "bucket": s["bucket"],
        "published": (s["published"] or "")[:10],
        "corroboration": s["corroboration"],
        "sources": s["sources"] + [f"{m} (mention)" for m in s["mentioned_by"]],
        "relevance": s.get("relevance"),
        "topic": s.get("topic"),
        # the same names on a `posted_recently` entry mean the subject was covered
        "models": models(s["keys"]),
        "summary": s["summary"],
        "link": s["link"],
        "urls": s["urls"][:3],
        # news_read has more than the summary; otherwise web_extract is the way in
        "readable": bool(s.get("texts")),
    }


def record_post(req: dict) -> dict:
    """Mark as posted every item offered in the last batch whose link is in the post.

    Per item, not per story: a story merged on a model name can hold two events,
    and the one the post did not cite must come back.
    """
    db = ledger()
    batch = meta(db, "pending_high_water")
    if not batch:
        return {"error": "nothing was offered: news_candidates has not run"}
    found = keys_in_text(req.get("post") or "")
    offered = db.execute(
        "SELECT story_id, parts FROM offered WHERE last_offered = ?", (batch,)).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    recorded = []
    for sid, parts_json in offered:
        cited = [p for p in json.loads(parts_json or "[]") if found & set(p["match"])]
        if not cited:
            continue
        keys = sorted({k for p in cited for k in p["keys"]})
        db.execute("INSERT OR REPLACE INTO posted (story_id, title, posted_at, models) VALUES (?, ?, ?, ?)",
                   (sid, cited[0]["title"], now, json.dumps(models(keys))))
        for k in blocking(keys):
            db.execute("INSERT OR REPLACE INTO posted_keys (key, story_id, posted_at) VALUES (?, ?, ?)", (k, sid, now))
        recorded.append(cited[0]["title"])
    # The window moves on when a digest was actually posted, or when there was
    # nothing to post. A turn that died before writing leaves it, and the next
    # run covers the same period again.
    advanced = None
    if recorded or not offered:
        set_meta(db, "high_water", batch)
        advanced = batch
    db.commit()
    return {"recorded": recorded, "not_in_post": len(offered) - len(recorded), "high_water": advanced}


def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    ops = {"candidates": candidates, "read": read, "record_post": record_post}
    op = ops.get(req.get("op"))
    try:
        out = op(req) if op else {"error": f"unknown op {req.get('op')!r}"}
    except Exception as exc:
        out = {"error": f"{type(exc).__name__}: {exc}"}
    json.dump(out, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
