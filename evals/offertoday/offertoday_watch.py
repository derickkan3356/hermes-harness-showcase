# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""How hard OfferToday rate-limits, measured over days rather than one burst.

OfferToday (BOSS Zhipin's Hong Kong board) serves its data as JSON behind a
client-rendered page. One probe from the home line got four calls 1.5 s apart,
then 429 on every call — still 429 at 8 s spacing after a minute's pause — and
was clear again about five minutes later. Whether the job digest can read it
depends on the shape of that limit, which one burst does not show.

Each run tries one call spacing, rotating through `SPACINGS` so a week covers
each several times at different hours:

  1. burst  — search and job-detail calls, `spacing` seconds apart, the mix a
              digest run would send, until the first 429 or `MAX_CALLS`.
  2. block  — after a 429, one search a minute until it answers again, up to
              `BLOCK_LIMIT`. That is the block's length.

A block that outlasts `BLOCK_LIMIT` stops the watcher for good: it writes
`runs/STOP`, and no run starts while that file exists. The home line is also the user's own
browser, and a limit that escalates is the one thing this must not provoke.

Every call goes through the fetch cascade's HTTP layer (`fetch_http.py`, raw
format), which is what the digest would use: the limit may key on the TLS
handshake, as arXiv's does, so a different client would measure a different
limit. The headers are the ones the site's own page sends; without `traceid`
and `origin` the search API answers 400.

Usage:
  uv run evals/offertoday/offertoday_watch.py              # one run
  uv run evals/offertoday/offertoday_watch.py --spacing 20 # one run at 20 s
  uv run evals/offertoday/offertoday_watch.py --report

Each run appends one JSON line per call to runs/<timestamp>.jsonl (not
tracked), and a last line with `"kind": "summary"`.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
STOP = RUNS / "STOP"
sys.path.insert(0, str(HERE.parent.parent / "plugins" / "fetch_cascade" / "workers"))
from raw_client import fetch_raw, unset_proxy  # noqa: E402

SPACINGS = [2, 5, 10, 20, 40, 60]
MAX_CALLS = 12
BLOCK_POLL = 60
BLOCK_LIMIT = 20 * 60
QUERIES = ["AI engineer", "LLM", "generative AI", "AI agent", "machine learning engineer", "Azure AI"]

SITE = "https://www.offertoday.com"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "x-requested-with": "XMLHttpRequest",
    "api-language": "en_HK",
    "accept-language": "en-HK",
    "origin": SITE,
    # Captured from the site's own page load; it answered with this value.
    "traceid": "F-001a0d3b93bd1HsrDdvSC1",
    "csrf-token": "",
}


def fetch(spec: dict) -> dict:
    """One request through the cascade's HTTP layer: status, `Retry-After`, parsed JSON."""
    r = fetch_raw([spec], timeout=20.0)[0]
    out = {"status": r["status"], "error": r.get("error", ""), "retry_after": r["headers"].get("retry-after")}
    if "body" in r:
        try:
            out["json"] = json.loads(r["body"])
        except ValueError:
            out["error"] = "body is not JSON"
    return out


def search_spec(keyword: str, page: int, session_id: str | None) -> dict:
    body = {"keyword": keyword, "page": page, "salaryType": 0, "publishTime": "2",
            "employmentTypes": [], "experiences": [], "educationLevels": [], "benefits": [],
            "workPermits": [], "rcdType": 7, "pageSize": 10, "jobFunctionCodes": [],
            "industries": [], "subDistrictCodes": [], "searchSource": None}
    if session_id:
        body["sessionId"] = session_id
    slug = keyword.lower().replace(" ", "-")
    return {"url": f"{SITE}/wapi/geek/recommend/search/list", "method": "POST", "body": body,
            "headers": {**HEADERS, "content-type": "application/json;charset=UTF-8",
                        "referer": f"{SITE}/en/search/{slug}-jobs"}}


def detail_spec(card: dict, session_id: str | None) -> dict:
    # The same parameters the site's page sends after a search; without
    # `encryptJobId` the API answers code 2, "Invalid param".
    q = {"id": card["jobId"], "lid": card.get("lid") or "", "encryptJobId": card["jobId"],
         "encryptExpectId": card.get("encryptExpectId") or ""}
    if session_id:
        q["sessionId"] = session_id
    return {"url": f"{SITE}/wapi/geek/recommend/jobDetail?" + urllib.parse.urlencode(q), "method": "GET",
            "headers": {**HEADERS, "referer": f"{SITE}/en/job/{urllib.parse.quote(card['jobId'])}"}}


def call(log, kind: str, spec: dict, **extra) -> dict:
    started = time.time()
    r = fetch(spec)
    body = r.get("json") or {}
    data = body.get("data") if isinstance(body, dict) else None
    rec = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": kind,
           "status": r["status"], "code": body.get("code") if isinstance(body, dict) else None,
           "msg": (body.get("msg") or "")[:80] if isinstance(body, dict) else "",
           "retry_after": r.get("retry_after"), "error": r.get("error", ""),
           "ms": round((time.time() - started) * 1000), **extra}
    if kind == "search" and isinstance(data, dict):
        rec["results"] = sum(1 for x in data.get("resultList") or [] if x.get("cardType") == 0)
    log(rec)
    r["data"] = data
    r["ok"] = r["status"] == 200 and isinstance(body, dict) and body.get("code") == 0
    return r


def next_spacing() -> int:
    done = len(list(RUNS.glob("*.jsonl")))
    return SPACINGS[done % len(SPACINGS)]


def stop_for_good(reason: str) -> None:
    STOP.write_text(f"{datetime.now(timezone.utc).isoformat()} {reason}\n")


def run(spacing: int) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    if STOP.exists():
        sys.exit(f"stopped: {STOP.read_text().strip()} — delete {STOP} to run again")
    path = RUNS / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    f = path.open("w")

    def log(rec: dict) -> None:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        print(f"  {rec['kind']:<7} {rec['status']} code={rec.get('code')} {rec.get('error') or ''}".rstrip())

    keyword = QUERIES[len(list(RUNS.glob("*.jsonl"))) % len(QUERIES)]
    print(f"spacing {spacing}s, keyword {keyword!r} → {path.name}")
    calls, first_block, session_id, cards, page = 0, None, None, [], 1
    while calls < MAX_CALLS:
        if calls:
            time.sleep(spacing)
        # Searches and detail calls in turn, as a digest run reads a page of
        # results and then opens the candidates.
        if cards and calls % 2 == 1:
            r = call(log, "detail", detail_spec(cards.pop(0), session_id), seq=calls, spacing=spacing)
        else:
            r = call(log, "search", search_spec(keyword, page, session_id), seq=calls, spacing=spacing)
            if r["ok"]:
                session_id = r["data"].get("sessionId") or session_id
                cards += [x for x in r["data"].get("resultList") or [] if x.get("cardType") == 0]
                page += 1
        calls += 1
        if not r["ok"]:
            first_block = calls
            break

    blocked_for = None
    if first_block is not None:
        started = time.time()
        while time.time() - started < BLOCK_LIMIT:
            time.sleep(BLOCK_POLL)
            r = call(log, "probe", search_spec(keyword, 1, None), spacing=spacing)
            if r["ok"]:
                blocked_for = round(time.time() - started)
                break

    summary = {"kind": "summary", "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "spacing": spacing, "keyword": keyword, "calls": calls, "blocked_at": first_block,
               "blocked_for": blocked_for}
    f.write(json.dumps(summary) + "\n")
    f.close()
    print(json.dumps(summary))
    if first_block is not None and blocked_for is None:
        stop_for_good(f"a block at {spacing}s spacing lasted over {BLOCK_LIMIT // 60} minutes ({path.name})")
        print(f"block outlasted {BLOCK_LIMIT // 60} min: watcher stopped")


def report() -> None:
    runs = []
    for p in sorted(RUNS.glob("*.jsonl")):
        lines = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        s = next((x for x in lines if x["kind"] == "summary"), None)
        if s:
            runs.append((p.stem, s, lines))
    if not runs:
        sys.exit("no finished runs yet")
    print(f"{len(runs)} runs, {runs[0][0]} .. {runs[-1][0]}")
    if STOP.exists():
        print(f"STOPPED: {STOP.read_text().strip()}")

    print("\n## calls before the first refusal, per spacing\n")
    print(f"A run that got through all {MAX_CALLS} calls counts as {MAX_CALLS}+.\n")
    print("| spacing s | runs | refused | calls before refusal (min / median / max) | block s (min / median / max) |")
    print("| --- | --- | --- | --- | --- |")
    by = defaultdict(list)
    for _, s, _ in runs:
        by[s["spacing"]].append(s)
    fmt = lambda xs: " / ".join(str(v) for v in (min(xs), statistics.median(xs), max(xs))) if xs else "-"
    for sp in sorted(by):
        ss = by[sp]
        refused = [s for s in ss if s["blocked_at"] is not None]
        before = [s["blocked_at"] - 1 for s in refused]
        blocks = [s["blocked_for"] for s in refused if s["blocked_for"] is not None]
        print(f"| {sp} | {len(ss)} | {len(refused)} | {fmt(before)} | {fmt(blocks)} |")

    print("\n## blocks per day — does the limit escalate?\n")
    print("| day | runs | refused | median block s | longest block s |")
    print("| --- | --- | --- | --- | --- |")
    days = defaultdict(list)
    for name, s, _ in runs:
        days[name[:8]].append(s)
    for d in sorted(days):
        ss = days[d]
        blocks = [s["blocked_for"] for s in ss if s["blocked_for"] is not None]
        print(f"| {d} | {len(ss)} | {sum(s['blocked_at'] is not None for s in ss)} | "
              f"{statistics.median(blocks) if blocks else '-'} | {max(blocks) if blocks else '-'} |")

    print("\n## what a refusal looks like\n")
    kinds = defaultdict(int)
    for _, _, lines in runs:
        for x in lines:
            if x["kind"] != "summary" and not (x["status"] == 200 and x.get("code") == 0):
                kinds[(x["status"], x.get("code"), x.get("msg") or x.get("error"), x.get("retry_after"))] += 1
    print("| status | code | message | retry-after | calls |")
    print("| --- | --- | --- | --- | --- |")
    for (st, code, msg, ra), n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"| {st} | {code} | {(msg or '')[:50]} | {ra or '-'} | {n} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spacing", type=int, help="seconds between calls; default rotates through SPACINGS")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        report()
        return
    unset_proxy()
    RUNS.mkdir(parents=True, exist_ok=True)
    run(args.spacing or next_spacing())


if __name__ == "__main__":
    main()
