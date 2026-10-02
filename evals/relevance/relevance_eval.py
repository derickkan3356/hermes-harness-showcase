# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "feedparser>=6.0", "fastembed>=0.4", "numpy"]
# ///
"""Where each wide-net source's `min_relevance` sits, from labelled data.

The digest scores every story against the topic descriptions in
`plugins/news_ledger/topics.yaml` (`relevance` in `workers/news.py`) and drops
a story whose sources all have a `min_relevance` in `sources.yaml` and which
scores below the lowest of them (`passes`). This eval scores what the feeds
eval collected with that same code and compares the scores with labels.

Two pools, both built from every full run in `evals/feeds/runs/`:

  control  each arXiv cs.CL / cs.IR paper once — the wide net the applied
           topics would be read from. Labelled for document-ai and text-to-sql.
  stories  the digest's own sources, merged into stories with the digest's
           story builder over every item published inside the widest window
           before the first run. Labelled for every topic.

Usage:
  uv run evals/relevance/relevance_eval.py pool     # score both pools
  uv run evals/relevance/relevance_eval.py report   # scores against labels.jsonl

`pool` writes evals/relevance/runs/<timestamp>/{control,stories}.jsonl (not
tracked). `labels.jsonl` here is tracked, one row per item:

  pool, id, title
  topics    the topics the item is about; empty for off the digest's subject
  how       `abstract` (title and abstract read) or `title` (title only)
  boundary  set on a row whose label came from a ruling on a class of
            borderline items, rather than from the item alone:
              industry              AI policy, funding and business news not about
                                    a model or a model provider — off-topic
              provider-marketing    a lab's customer stories, courses, webinars
                                    and product pages — off-topic
              visual-doc-retrieval  retrieving document pages as images —
                                    document-ai
              text-extraction       extracting fields or relations from plain-text
                                    documents — document-ai
              data-agent            an agent that queries a data platform in
                                    natural language — text-to-sql
              query-language        natural language to SPARQL and other
                                    non-SQL query languages — text-to-sql
            General LLM research papers (training, distillation, architecture)
            are off-topic too; a named model's technical report is `models`.

The labels were written by Claude Opus 5.5 from titles and abstracts, with the
boundary rulings made by the user. A label outlives a change of description or
embedding model; the scores do not, so rerun `pool` after either.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FEED_RUNS = ROOT / "evals" / "feeds" / "runs"
RUNS = HERE / "runs"
LABELS = HERE / "labels.jsonl"
PLUGIN = ROOT / "plugins" / "news_ledger"
sys.path.insert(0, str(PLUGIN / "workers"))
sys.path.insert(0, str(ROOT / "evals" / "feeds"))

import news  # noqa: E402
import stories as S  # noqa: E402
from feeds_eval import load_runs, load_sources, upgrade  # noqa: E402

CONTROL = {"arxiv-cs-cl", "arxiv-cs-ir"}


def arxiv_id(url: str) -> str:
    m = re.search(r"arxiv\.org/abs/([0-9.]+)", url)
    return m.group(1) if m else url


def collect() -> tuple[list[dict], list[dict]]:
    """Each control paper once, and every other item once, over all full runs."""
    table = {s["id"]: s for s in load_sources()}
    latest_only = {s["id"] for s in table.values() if s.get("latest_only")}
    runs = load_runs()
    # Several feeds carry their whole archive. What the digest could have seen
    # is what published inside the widest window before the first run.
    first = min(S.parse_time(it["fetched_at"]) for _, run_items, _ in runs for it in run_items)
    since = first - news.WINDOW_CAP
    control: dict[str, dict] = {}
    items: dict[tuple[str, str], dict] = {}
    for _, run_items, _ in runs:
        for raw in run_items:
            it = upgrade(dict(raw), table)
            if it is None or not S.in_window(it, since, S.parse_time(it["fetched_at"])):
                continue
            if it["source"] in CONTROL:
                control.setdefault(arxiv_id(it["url"]), it)
                continue
            # A release feed tags several builds a day; the digest keeps the newest
            # in its window, so one a day stands in for it here.
            day = (it["published"] or "")[:10] if it["source"] in latest_only else ""
            key = (it["source"], day or it["url_key"] or it["title_key"])
            if key not in items or (it["published"] or "") > (items[key]["published"] or ""):
                items[key] = it
    papers = [{"id": pid, "title": it["title"], "summary": it["summary"][:S.SUMMARY],
               "published": it["published"], "sources": [it["source"]]}
              for pid, it in sorted(control.items())]
    return papers, S.build_stories(list(items.values()))


def score(rows: list[dict], topics: list[dict]) -> None:
    """The digest's `relevance`, plus the score against every topic."""
    errors: list[str] = []
    news.relevance(rows, topics, errors)
    if errors:
        sys.exit(errors[0])
    from fastembed import TextEmbedding

    model = TextEmbedding(news.EMBED_MODEL, cache_dir=str(news.EMBED_CACHE))
    docs = np.array(list(model.embed([f"{r['title']}. {r['summary']}" for r in rows])))
    tops = np.array(list(model.embed([t["description"] for t in topics])))
    for r, row in zip(rows, docs @ tops.T):
        r["scores"] = {t["name"]: round(float(v), 3) for t, v in zip(topics, row)}


def pool() -> None:
    topics = yaml.safe_load((PLUGIN / "topics.yaml").read_text())
    papers, stories = collect()
    rows = {
        "control": papers,
        "stories": [{"id": s["id"], "title": s["title"], "summary": s["summary"], "published": s["published"],
                     "sources": s["sources"], "bucket": s["bucket"], "corroboration": s["corroboration"]}
                    for s in stories],
    }
    run = RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    for name, rs in rows.items():
        score(rs, topics)
        with (run / f"{name}.jsonl").open("w") as f:
            for r in rs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{name}: {len(rs)} rows")
    print(run)


# --- report -------------------------------------------------------------------

def latest_run() -> Path:
    runs = sorted(d for d in RUNS.glob("*/") if (d / "control.jsonl").exists())
    if not runs:
        sys.exit("no pool yet — run `pool` first")
    return runs[-1]


def load_labels() -> dict[tuple[str, str], dict]:
    if not LABELS.exists():
        return {}
    return {(r["pool"], r["id"]): r for r in map(json.loads, LABELS.read_text().splitlines()) if r}


def auc(scores: list[float], truth: list[bool]) -> float:
    """Chance that an on-topic row outscores an off-topic one."""
    pos = [s for s, t in zip(scores, truth) if t]
    neg = [s for s, t in zip(scores, truth) if not t]
    if not pos or not neg:
        return float("nan")
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


FLOORS = [round(0.50 + 0.02 * i, 2) for i in range(18)]


def floor_table(title: str, scores: list[float], truth: list[bool], mark: float | None) -> None:
    """What a floor at each step keeps and loses. `mark` flags the configured one."""
    pos, neg = sum(truth), len(truth) - sum(truth)
    print(f"### {title}\n")
    print(f"{len(truth)} rows, {pos} on-topic. AUC {auc(scores, truth):.3f}.\n")
    print("| floor | on-topic lost | off-topic removed | kept | precision kept |")
    print("| --- | --- | --- | --- | --- |")
    for f in FLOORS:
        kept = [t for s, t in zip(scores, truth) if s >= f]
        lost = pos - sum(kept)
        removed = neg - (len(kept) - sum(kept))
        prec = f"{sum(kept) / len(kept):.2f}" if kept else "-"
        flag = " ←" if mark is not None and abs(f - mark) < 1e-9 else ""
        print(f"| {f:.2f}{flag} | {lost}/{pos} | {removed}/{neg} | {len(kept)} | {prec} |")
    print()


def report() -> None:
    run = latest_run()
    labels = load_labels()
    topics = [t["name"] for t in yaml.safe_load((PLUGIN / "topics.yaml").read_text())]
    table = {s["id"]: s for s in load_sources()}
    floors = {sid: s["min_relevance"] for sid, s in table.items() if "min_relevance" in s}
    rows = {name: [r for r in map(json.loads, (run / f"{name}.jsonl").read_text().splitlines())
                   if (name, r["id"]) in labels]
            for name in ("stories", "control")}
    print(f"pool {run.name}, {len(labels)} labels\n")

    st = rows["stories"]
    truth = [bool(labels[("stories", r["id"])]["topics"]) for r in st]
    kept = [news.passes(r, floors) for r in st]
    lost = sum(t and not k for t, k in zip(truth, kept))
    removed = sum(not t and not k for t, k in zip(truth, kept))
    print("## stories\n")
    print(f"{len(st)} stories, {sum(truth)} on-topic. The configured floors drop {removed} of "
          f"{len(st) - sum(truth)} off-topic and {lost} on-topic.\n")
    print("Per source: every story the source carries, on-topic for any topic. A story also")
    print("carried by a source without a floor passes whatever this table says.\n")
    by_src: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(st):
        for src in r["sources"]:
            by_src[src].append(i)
    for src, ix in sorted(by_src.items(), key=lambda kv: (kv[0] not in floors, -len(kv[1]))):
        if src in floors or len(ix) >= 50:
            floor_table(f"{src}" + (f", min_relevance {floors[src]}" if src in floors else ", no floor"),
                        [st[i]["relevance"] for i in ix], [truth[i] for i in ix], floors.get(src))

    print("## per topic\n")
    print("How well each description picks out its own stories. Not a filter: a guide for")
    print("rewriting a description. `routed` is the digest's `topic`, the closest description.\n")
    print("| topic | on-topic | AUC own score | routed here | on-topic routed elsewhere |")
    print("| --- | --- | --- | --- | --- |")
    for name in topics:
        t = [name in labels[("stories", r["id"])]["topics"] for r in st]
        print(f"| {name} | {sum(t)} | {auc([r['scores'][name] for r in st], t):.3f} | "
              f"{sum(r['topic'] == name for r in st)} | {sum(x and r['topic'] != name for r, x in zip(st, t))} |")
    print()

    ctrl = rows["control"]
    applied = ["document-ai", "text-to-sql"]
    truth = [bool(set(applied) & set(labels[("control", r["id"])]["topics"])) for r in ctrl]
    print("## control: arXiv cs.CL and cs.IR as a wide net for the applied bucket\n")
    print("`relevance` is what `passes` would see: the closest of all topics, so an agent or a")
    print("model paper clears it as easily as an applied one. `applied score` is the closer of")
    print("document-ai and text-to-sql alone.\n")
    print(f"AUC on `relevance` {auc([r['relevance'] for r in ctrl], truth):.3f}.\n")
    floor_table("applied score", [max(r["scores"][n] for n in applied) for r in ctrl], truth, None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pool", "report"])
    args = ap.parse_args()
    {"pool": pool, "report": report}[args.cmd]()


if __name__ == "__main__":
    main()
