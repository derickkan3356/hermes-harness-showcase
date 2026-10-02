# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "feedparser>=6.0"]
# ///
"""How much the AI feed set carries, how much of it is one story told twice, and whether it holds up over a week.

Pulls every source in `plugins/news_ledger/sources.yaml` with the digest's own
pulling code (`plugins/news_ledger/workers/feeds.py`) and writes what it saw.
Runs are compared against each other, so a week of them answers items a day
per bucket, how much repeats between runs, how many feeds carry no usable
timestamp, and whether the story merging joins what it should and nothing else.
Its runs are also what `evals/relevance` labels and scores.

The table includes `control` sources the digest never reads — the whole of
arXiv cs.CL and cs.IR — so the topic queries can be checked against everything
they could have caught.

`--report` replays the latest run through the digest's story builder
(`workers/stories.py`) over a seven-day window. The largest stories are listed
first on purpose: a bad merge shows up as a story that is too big. It also
prints how much text each source carries, which is what `news_read` can return
without a fetch.

Egress matters: Reddit, Google and HuggingFace answer this machine, not a proxy.
The pulling code unsets HTTPS_PROXY/HTTP_PROXY itself.

Usage:
  uv run evals/feeds/feeds_eval.py              # one collection run
  uv run evals/feeds/feeds_eval.py --only hn-llm,localllama
  uv run evals/feeds/feeds_eval.py --report

Each run writes evals/feeds/runs/<timestamp>/ (not tracked):

  items.jsonl   one record per item
  sources.json  per source: status, item count, how many carried a timestamp
  index.md      the table to open first

A run with `--only` is marked partial and left out of `--report`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
PLUGIN = HERE.parent.parent / "plugins" / "news_ledger"
sys.path.insert(0, str(PLUGIN / "workers"))

import feeds  # noqa: E402
import stories as S  # noqa: E402
from keys import artifacts  # noqa: E402


def load_sources() -> list[dict]:
    return yaml.safe_load((PLUGIN / "sources.yaml").read_text())


def write_run(items: list[dict], report: list[dict], partial: bool) -> Path:
    run = RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    with (run / "items.jsonl").open("w") as f:
        for it in items:
            # paragraphs are for news_read; the eval measures items, and a week of
            # newsletter bodies would multiply the runs' size many times over
            row = {k: v for k, v in it.items() if k != "blocks"}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    (run / "sources.json").write_text(
        json.dumps({"partial": partial, "sources": report}, indent=2, ensure_ascii=False)
    )
    lines = [f"# feeds run {run.name}", "", "| source | bucket | status | items | dated | newest |",
             "| --- | --- | --- | --- | --- | --- |"]
    for r in report:
        lines.append(
            f"| {r['id']} | {r['bucket']} | {r.get('error', r['status'])[:40]} | "
            f"{r.get('items', 0)} | {r.get('dated', 0)} | {r.get('newest') or '-'} |"
        )
    (run / "index.md").write_text("\n".join(lines) + "\n")
    return run


# --- report -------------------------------------------------------------------

def load_runs() -> list[tuple[str, list[dict], list[dict]]]:
    out = []
    for d in sorted(RUNS.glob("*/")):
        p, meta = d / "items.jsonl", d / "sources.json"
        if not p.exists():
            continue
        report = json.loads(meta.read_text()) if meta.exists() else {}
        if isinstance(report, dict) and report.get("partial"):
            continue  # a --only spot check is not a data point
        srcs = report.get("sources", report) if isinstance(report, (dict, list)) else []
        out.append((d.name, [json.loads(line) for line in p.read_text().splitlines() if line.strip()], srcs))
    return out


def upgrade(it: dict, table: dict[str, dict]) -> dict | None:
    """Give an item from an early run the fields later runs record."""
    src = table.get(it["source"])
    if src is None:
        return None
    it.setdefault("kind", src["kind"])
    it.setdefault("family", feeds.family(src, it["url"]))
    it.setdefault("control", bool(src.get("control")))
    it.setdefault("opaque_url", bool(src.get("opaque_url")))
    if "primary" not in it:
        primary = sorted(artifacts("" if it["opaque_url"] else it["url"]))
        it["primary"] = primary
        it["mentioned"] = sorted(set(it.get("artifacts", [])) - set(primary))
    return it


def report() -> None:
    runs = load_runs()
    if not runs:
        sys.exit("no runs yet — run the collector first")
    table = {s["id"]: s for s in load_sources()}
    print(f"{len(runs)} runs, {runs[0][0]} .. {runs[-1][0]}\n")

    buckets = sorted({s["bucket"] for s in table.values()})
    print("## items per bucket per run, control sources excluded\n")
    print("| run | " + " | ".join(buckets) + " | total | failed sources |")
    print("| --- |" + " --- |" * (len(buckets) + 2))
    for name, items, srcs in runs:
        counts = defaultdict(int)
        for it in items:
            if not table.get(it["source"], {}).get("control"):
                counts[it["bucket"]] += 1
        failed = [r["id"] for r in srcs if r.get("status") == "error"]
        print(f"| {name} | " + " | ".join(str(counts[b]) for b in buckets)
              + f" | {sum(counts.values())} | {', '.join(failed) or '-'} |")

    latest_name, latest, _ = runs[-1]
    fetched = S.parse_time(latest[0]["fetched_at"]) if latest else datetime.now(timezone.utc)

    print("\n## in the window, latest run\n")
    print("Several feeds carry their whole archive, so the raw count is not the daily")
    print("volume. What the digest sees is what published inside the window.\n")
    win: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for it in latest:
        if table.get(it["source"], {}).get("control"):
            continue
        t = S.parse_time(it["published"])
        if t is None:
            continue
        age = fetched - t
        win[it["bucket"]][0] += age <= timedelta(hours=24)
        win[it["bucket"]][1] += age <= timedelta(days=7)
    print("| bucket | last 24h | last 7d |")
    print("| --- | --- | --- |")
    for b in buckets:
        print(f"| {b} | {win[b][0]} | {win[b][1]} |")

    print("\n## repeats between runs\n")
    print("A later run seeing an item an earlier run saw. `url` repeats cost nothing to")
    print("catch; `title` repeats are one story under a new link.\n")
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    url_rep = title_rep = fresh = 0
    for _, items, _ in runs:
        new_u, new_t = set(), set()
        for it in items:
            if it["url_key"] and it["url_key"] in seen_urls:
                url_rep += 1
            elif it["title_key"] in seen_titles:
                title_rep += 1
            else:
                fresh += 1
            new_u.add(it["url_key"])
            new_t.add(it["title_key"])
        seen_urls |= new_u
        seen_titles |= new_t
    total = url_rep + title_rep + fresh
    pct = lambda n: f"{100 * n / total:.1f}%" if total else "-"
    print("| layer | rows | share |")
    print("| --- | --- | --- |")
    print(f"| same url as an earlier run | {url_rep} | {pct(url_rep)} |")
    print(f"| new url, exact title seen before | {title_rep} | {pct(title_rep)} |")
    print(f"| new | {fresh} | {pct(fresh)} |")

    print("\n## stories, latest run, seven-day window\n")
    items = [u for it in latest
             if (u := upgrade(dict(it), table)) and not u["control"]
             and S.in_window(u, fetched - timedelta(days=7), fetched)]
    st = S.build_stories(items)
    dist = defaultdict(int)
    for s in st:
        dist[s["corroboration"]] += 1
    print(f"{len(items)} items → {len(st)} stories. Corroboration: "
          + ", ".join(f"{k} source{'s' if k > 1 else ''}: {v}" for k, v in sorted(dist.items())) + "\n")
    print("Largest first — a story that is too big is a bad merge.\n")
    print("| items | corroboration | title | merged on |")
    print("| --- | --- | --- | --- |")
    for s in sorted(st, key=lambda s: (-s["items"], -s["corroboration"]))[:12]:
        keys = [k for k in s["keys"] if not k.startswith(("url:", "title:"))][:3]
        print(f"| {s['items']} | {s['corroboration']} | {s['title'][:60]} | {', '.join(keys) or 'title'} |")

    print("\n## text the feeds carry, latest run\n")
    print("Median characters of plain text per item: what news_read can return without")
    print("a fetch. A source near zero is one only web_extract can read.\n")
    lens: dict[str, list[int]] = defaultdict(list)
    for it in latest:
        if "body_chars" in it and not table.get(it["source"], {}).get("control"):
            lens[it["source"]].append(it["body_chars"])
    if not lens:
        print("No run records body_chars yet.")
    else:
        print("| source | items | median chars |")
        print("| --- | --- | --- |")
        for sid, ls in sorted(lens.items(), key=lambda kv: -sorted(kv[1])[len(kv[1]) // 2]):
            print(f"| {sid} | {len(ls)} | {sorted(ls)[len(ls) // 2]} |")

    print("\n## timestamps, all runs\n")
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for _, items, _ in runs:
        for it in items:
            per[it["source"]][0] += 1
            per[it["source"]][1] += bool(it["published"])
    undated = [(sid, n, d) for sid, (n, d) in per.items() if n != d]
    if not undated:
        print("Every item from every source carried a published timestamp.")
    else:
        print("| source | items | undated |")
        print("| --- | --- | --- |")
        for sid, n, d in sorted(undated, key=lambda r: r[1] - r[2], reverse=True):
            print(f"| {sid} | {n} | {n - d} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated source ids")
    ap.add_argument("--report", action="store_true", help="read every run and print the numbers")
    args = ap.parse_args()

    if args.report:
        report()
        return

    sources = load_sources()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        sources = [s for s in sources if s["id"] in wanted]
    feeds.unset_proxy()
    print(f"pulling {len(sources)} sources")
    items, rep = feeds.pull_all(sources)
    for r in rep:
        detail = r.get("error", "")[:70] if r["status"] == "error" else \
            f"{r['items']:>4} items, {r['dated']:>4} dated, newest {r['newest'] or '-'}"
        print(f"  {r['id']:<22} {detail}")
    run = write_run(items, rep, partial=bool(args.only))
    ok = sum(1 for r in rep if r["status"] == "ok")
    print(f"\n{len(items)} items from {ok}/{len(rep)} sources → {run}")


if __name__ == "__main__":
    main()
