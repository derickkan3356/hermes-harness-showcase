# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""The job-ledger worker: today's entries for the job digest, the full ad, and the record of what was posted.

Runs as its own process under `uv run --script`, as the news worker does.
`jobs.py.lock` pins the environment.

Protocol: one JSON object on stdin, one on stdout.

    in   {"op": "candidates", "limit": int}
    out  {"window", "pull", "profile", "entries": [...], "more", "counts", "errors"}

    in   {"op": "read", "entry_id": str}
    out  {"id", "title", "company", "url", "jd"}

    in   {"op": "record_post", "post": str}
    out  {"posted": [title], "seen": int, "high_water": str}

    in   {"op": "feedback", "entry_id": str, "verdict": "wrong" | "right", "reason": str}
    out  {"recorded": title}

What the digest must not get wrong is decided here: the period, the hard denies,
which ads are one job, and what has been shown. The model judges the entries
against the profile and writes. A run's entries count as seen once a finished
answer comes back from the turn that asked for them — the plugin's
`post_llm_call` hook sends it here — whether the model kept any or not: it read
them, and showing them again tomorrow is noise. An entry whose link is in the
answer is posted. A run that dies before answering leaves its entries unseen,
and the next run offers them again.

Each run pulls the last `PULL_DAYS` from both boards into the ledger, unless
the last pull was less than `PULL_EVERY` ago, then builds its entries from
every ad the ledger holds from the last `WINDOW`: a day whose pull failed is covered by the next pull, an entry past `limit` is still
there tomorrow, and an ad merges with its twin on the other board whichever day
each was pulled. A JD is fetched once per ad and kept, so the rules run again on
every ad each day — a changed profile applies at once — and only a new ad costs
a request. A run soon after a pull reads the ledger alone, so the user asking
for the next entries sends no search page to CTgoodjobs.

The ledger is SQLite at `~/.hermes/job_ledger.db` (`JOB_LEDGER_DB` overrides):
runtime state, not tracked.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import rules
import sources

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
DB = Path(os.environ.get("JOB_LEDGER_DB", Path.home() / ".hermes" / "job_ledger.db"))

# Both boards search by day ranges that include 3 (JobsDB `dateRange`, CTgoodjobs
# `post_date`); the overlap between daily pulls costs no JD fetch.
PULL_DAYS = 3
# A run this soon after the last pull reads the ledger only. CTgoodjobs' WAF
# counts every request; the next daily pull covers what a skipped one missed.
PULL_EVERY = timedelta(hours=6)
# Entries come from the ledger's ads posted in this period; past `limit`, the
# rest stay unseen and come first in the next runs.
WINDOW = timedelta(days=7)
# Pulled ads and read JDs are dropped from the ledger after this long.
CACHE_KEEP = timedelta(days=30)
# A job seen once is not offered again for this long; a repost within it is the
# same job, and one after it is worth another look.
SEEN_BLOCK = timedelta(days=30)
LIMIT = 20
HKT = timezone(timedelta(hours=8))

# `ads` and `ad_jd` are keyed by one board's ad (`source:source_id`); `jd` holds
# the text each offered entry carried, by job key, for `read`.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ads (key TEXT PRIMARY KEY, record TEXT, first_pulled TEXT, last_pulled TEXT);
CREATE TABLE IF NOT EXISTS ad_jd (key TEXT PRIMARY KEY, text TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS offered (
    batch TEXT, entry_id TEXT, key TEXT, title TEXT, company TEXT, url TEXT,
    seen INTEGER DEFAULT 0, PRIMARY KEY (batch, key));
CREATE TABLE IF NOT EXISTS posted (key TEXT PRIMARY KEY, title TEXT, url TEXT, batch TEXT, posted_at TEXT);
CREATE TABLE IF NOT EXISTS jd (key TEXT PRIMARY KEY, text TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS feedback (
    key TEXT, batch TEXT, entry_id TEXT, title TEXT, verdict TEXT, reason TEXT, at TEXT,
    PRIMARY KEY (key, at));
"""


def ledger() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    return db


def meta(db: sqlite3.Connection, k: str) -> str | None:
    row = db.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, k: str, v: str) -> None:
    db.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


def profile() -> dict:
    return yaml.safe_load((PLUGIN / "profile.yaml").read_text())


def _ad_key(ad: dict) -> str:
    return f"{ad['source']}:{ad['source_id']}"


def _day(ad: dict) -> str:
    """The Hong Kong day an ad was posted. CTgoodjobs gives only the day; JobsDB a UTC time."""
    if ad["source"] == "ctgoodjobs":
        return ad["posted"] or ""
    t = sources._parse_time(ad["posted"])
    return t.astimezone(HKT).date().isoformat() if t else ""


def _in_window(ad: dict, since: datetime) -> bool:
    if ad["source"] == "ctgoodjobs":   # a Hong Kong calendar day, no time
        return (ad["posted"] or "") >= since.astimezone(HKT).date().isoformat()
    t = sources._parse_time(ad["posted"])
    return t is not None and t >= since


def _seen(db: sqlite3.Connection, keys: list[str], now: datetime) -> bool:
    cutoff = (now - SEEN_BLOCK).isoformat()
    q = ",".join("?" * len(keys))
    return db.execute(f"SELECT 1 FROM offered WHERE seen = 1 AND batch >= ? AND key IN ({q})",
                      (cutoff, *keys)).fetchone() is not None


def _store(db: sqlite3.Connection, ads: list[dict], stamp: str) -> None:
    for a in ads:
        k = _ad_key(a)
        old = db.execute("SELECT record FROM ads WHERE key = ?", (k,)).fetchone()
        if old and a["below_floor"] is None:
            # A pull whose salary query failed says nothing about the floor.
            a = {**a, "below_floor": json.loads(old[0]).get("below_floor")}
        db.execute("INSERT INTO ads (key, record, first_pulled, last_pulled) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET record = excluded.record, last_pulled = excluded.last_pulled",
                   (k, json.dumps(a, ensure_ascii=False), stamp, stamp))


def _held(db: sqlite3.Connection, since: datetime) -> list[dict]:
    """The ledger's ads posted since `since`, each with the time it was first pulled."""
    ads = []
    for record, first in db.execute("SELECT record, first_pulled FROM ads"):
        a = json.loads(record) | {"first_pulled": first}
        if _in_window(a, since):
            ads.append(a)
    return ads


def _read_jd(db: sqlite3.Connection, ads: list[dict], fetch: set[str], stamp: str) -> tuple[dict, list[dict]]:
    """The JD of each ad from the ledger; of the ads keyed in `fetch` that it lacks,
    from the board. A page read, with or without a JD in it, is kept; a failed
    request is tried again next run. Returns the JDs and the ads that were fetched."""
    held = {k: ({"text": t} if t is not None else {"error": sources.NO_TEXT})
            for k, t in db.execute("SELECT key, text FROM ad_jd")}
    todo = [a for a in ads if _ad_key(a) in fetch and _ad_key(a) not in held]
    got = sources.fetch_jd(todo)
    for k, v in got.items():
        if "text" in v or v["error"] == sources.NO_TEXT:
            db.execute("INSERT OR REPLACE INTO ad_jd (key, text, fetched_at) VALUES (?, ?, ?)", (k, v.get("text"), stamp))
    return {k: v for k, v in held.items() if k in {_ad_key(a) for a in ads}} | got, todo


def candidates(req: dict) -> dict:
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    p = profile()
    db = ledger()
    keep = (now - CACHE_KEEP).isoformat()
    db.execute("DELETE FROM ads WHERE last_pulled < ?", (keep,))
    db.execute("DELETE FROM ad_jd WHERE fetched_at < ?", (keep,))

    last = meta(db, "last_pull")
    pull = last is None or now - datetime.fromisoformat(last) >= PULL_EVERY
    if pull:
        pulled, report = sources.pull_all(p, PULL_DAYS)
        _store(db, pulled, stamp)
        errors = report["jobsdb"]["errors"] + report["ctgoodjobs"]["errors"]
        ct_wall = [e for e in errors if "CAPTCHA" in e]
        if ct_wall:   # one line for the wall, not one per page the breaker skipped
            errors = [e for e in errors if "CAPTCHA" not in e] + [f"ctgoodjobs: {ct_wall[0].split(': ', 1)[1]}"]
        last = stamp
        set_meta(db, "last_pull", last)
        set_meta(db, "last_errors", json.dumps(errors, ensure_ascii=False))   # read by tools/facts.sh jobs
        db.commit()
    else:   # the ledger holds what the last pull got, and that pull's errors still hold
        pulled, report = [], {"jobsdb": {"requests": 0}}
        errors = json.loads(meta(db, "last_errors") or "[]")
    ct_search = sources.ct_requests()
    since = now - WINDOW
    ads = _held(db, since)

    jobs = [rules.judge(j, p) for j in rules.merge(ads)]
    for j in jobs:
        j["first_pulled"] = min(a["first_pulled"] for a in j["ads"])
        j["day"] = max(_day(a) for a in j["ads"])
    # A job already shown is dropped below whatever its JD says, so only an
    # unseen one's missing JD is fetched.
    kept = [j for j in jobs if not j["deny"]]
    fetch = {_ad_key(a) for j in kept if not _seen(db, [j["key"]], now) for a in j["ads"]}
    jd, fetched = _read_jd(db, [a for j in kept for a in j["ads"]], fetch, stamp)
    jobs = [rules.judge_jd(j, p, jd) if not j["deny"] else j for j in jobs]

    collapsed = rules.collapse(jobs)
    entries = [e for e in collapsed if not _seen(db, e["keys"], now)]
    # First pulled first, so an entry past `limit` comes before newer ones next
    # time; within one pull, newest posted first.
    entries.sort(key=lambda e: e["day"], reverse=True)
    entries.sort(key=lambda e: e["first_pulled"])
    limit = max(1, min(int(req.get("limit") or LIMIT), 40))
    shown, more = entries[:limit], len(entries) - limit

    out = []
    for i, e in enumerate(shown, 1):
        eid = f"J{i}"
        body = jd.get(_ad_key(e["ads"][0]), {}).get("text", "")
        for k in e["keys"]:
            db.execute("INSERT OR REPLACE INTO offered (batch, entry_id, key, title, company, url, seen) "
                       "VALUES (?, ?, ?, ?, ?, ?, 0)", (stamp, eid, k, e["title"], e["company"], e["ads"][0]["url"]))
            if body:
                db.execute("INSERT OR REPLACE INTO jd (key, text, fetched_at) VALUES (?, ?, ?)", (k, body, stamp))
        out.append({
            "id": eid, "title": e["title"], "company": e["company"],
            "also_posted_as": e["variants"][1:], "posted": e["day"],
            "boards": e["sources"], "url": e["ads"][0]["url"],
            "salary": next((a["salary_text"] for a in e["ads"] if a["salary_text"]), ""),
            "marks": e["marks"],
            "jd": sources.jd_excerpt(body) if body else "",
        })
    set_meta(db, "pending_batch", stamp)
    db.commit()
    return {
        "window": {"since": since.isoformat(timespec="minutes"), "until": stamp,
                   "date_hkt": now.astimezone(HKT).date().isoformat()},
        "pull": {"new": pull, "at_hkt": datetime.fromisoformat(last).astimezone(HKT).strftime("%Y-%m-%d %H:%M")},
        "profile": {"description": " ".join(p["description"].split()), "not_wanted": p["not_wanted"]},
        "entries": out, "more": max(0, more),
        "counts": {"pulled": len(pulled), "ads": len(ads), "jobs": len(jobs),
                   "passed_rules": sum(not j["deny"] for j in jobs),
                   "already_seen": len(collapsed) - len(entries), "entries": len(out),
                   "requests": {"jobsdb_search": report["jobsdb"]["requests"], "ctgoodjobs_search": ct_search,
                                "jobsdb_jd": sum(a["source"] == "jobsdb" for a in fetched),
                                "ctgoodjobs_jd": sources.ct_requests() - ct_search}},
        "errors": errors,
    }


def read(req: dict) -> dict:
    db = ledger()
    eid = str(req.get("entry_id") or "").strip().upper()
    row = db.execute("SELECT key, title, company, url FROM offered WHERE entry_id = ? ORDER BY batch DESC LIMIT 1",
                     (eid,)).fetchone()
    if not row:
        return {"error": f"no entry {eid!r} in the latest job digest"}
    text = db.execute("SELECT text FROM jd WHERE key = ?", (row[0],)).fetchone()
    return {"id": eid, "title": row[1], "company": row[2], "url": row[3],
            "jd": text[0] if text else "", **({} if text else {"note": "The JD could not be read when the entry was offered."})}


def record_post(req: dict) -> dict:
    post = str(req.get("post") or "")
    db = ledger()
    batch = meta(db, "pending_batch")
    if not batch:
        return {"posted": [], "seen": 0, "high_water": meta(db, "high_water")}
    rows = db.execute("SELECT entry_id, key, title, url FROM offered WHERE batch = ?", (batch,)).fetchall()
    # A kept entry is its `### J3 · title` heading or its link: either one is
    # enough, so a miscopied link still records the entry the user was shown.
    headed = set(re.findall(r"^#{2,4}\s*(J\d+)\b", post, re.M))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    posted = []
    for eid, key, title, url in rows:
        if eid in headed or (url and url in post):
            db.execute("INSERT OR REPLACE INTO posted (key, title, url, batch, posted_at) VALUES (?, ?, ?, ?, ?)",
                       (key, title, url, batch, now))
            posted.append(title)
    db.execute("UPDATE offered SET seen = 1 WHERE batch = ?", (batch,))
    set_meta(db, "high_water", batch)
    db.execute("DELETE FROM meta WHERE k = 'pending_batch'")
    db.commit()
    return {"posted": sorted(set(posted)), "seen": len(rows), "high_water": batch}


def feedback(req: dict) -> dict:
    db = ledger()
    eid = str(req.get("entry_id") or "").strip().upper()
    verdict = req.get("verdict")
    if verdict not in ("wrong", "right"):
        return {"error": "verdict must be 'wrong' or 'right'"}
    rows = db.execute("SELECT batch, key, title FROM offered WHERE entry_id = ? AND batch = "
                      "(SELECT MAX(batch) FROM offered WHERE entry_id = ?)", (eid, eid)).fetchall()
    if not rows:
        return {"error": f"no entry {eid!r} in the latest job digest"}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for batch, key, title in rows:
        db.execute("INSERT INTO feedback (key, batch, entry_id, title, verdict, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (key, batch, eid, title, verdict, str(req.get("reason") or "")[:500], now))
    db.commit()
    return {"recorded": rows[0][2], "verdict": verdict}


def main() -> None:
    sources.unset_proxy()
    req = json.load(sys.stdin)
    ops = {"candidates": candidates, "read": read, "record_post": record_post, "feedback": feedback}
    op = ops.get(req.get("op"))
    out = op(req) if op else {"error": f"unknown op {req.get('op')!r}"}
    json.dump(out, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
