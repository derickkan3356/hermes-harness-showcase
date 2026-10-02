# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""What the job digest's sources carry, and what each hard deny takes out.

Pulls JobsDB and CTgoodjobs with the digest's own code
(`plugins/job_ledger/workers/sources.py`), groups ads into jobs and applies the
hard denies and marks (`rules.py`), and writes what it saw. Runs are compared
against each other, so a series of them says how many jobs a day survive the
code layer — the number the post's cadence is set from.

What survives is what the model would read, after `rules.collapse` folds one
company's role posted per product into one entry. The experience rule reads a
sentence out of each JD, so each run lists what it removed with that sentence.

A run costs CTgoodjobs a search page per slug, more while the pages come back
full (up to `pages` in the profile), plus one page per surviving ad, paced
(`sources.CT_GAP`); it reads no JD from the ledger, so every surviving ad is
fetched. A few hundred requests in three hours bring up its CAPTCHA for hours
(`docs/hk-jobs.md`, Sources), so do not loop this.

Usage:
  uv run evals/jobs/jobs_eval.py              # one run over the last 7 days
  uv run evals/jobs/jobs_eval.py --days 1
  uv run evals/jobs/jobs_eval.py --report     # every run: survivors, new since the run before

Each run writes evals/jobs/runs/<timestamp>/ (not tracked):

  ads.jsonl     one record per ad, as the sources read it
  jobs.jsonl    one per job: its ads, `deny` reasons, `marks`
  summary.json  counts
  index.md      the entries the model would read, then the experience rule's
                removals — open this first
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
PLUGIN = HERE.parent.parent / "plugins" / "job_ledger"
sys.path.insert(0, str(PLUGIN / "workers"))

import rules  # noqa: E402
import sources  # noqa: E402

def summarize(ads: list[dict], jobs: list[dict], entries: list[dict], rep: dict, days: int) -> dict:
    survivors = [j for j in jobs if not j["deny"]]
    return {
        "days": days,
        "ads": Counter(a["source"] for a in ads),
        "errors": rep["jobsdb"]["errors"] + rep["ctgoodjobs"]["errors"],
        "jobs": len(jobs),
        "on_both_boards": sum(len(j["sources"]) > 1 for j in jobs),
        "deny_any": Counter(r.split(":")[0] for j in jobs for r in j["deny"]),
        "deny_first": Counter(j["deny"][0].split(":")[0] for j in jobs if j["deny"]),
        "survivors": len(survivors),
        "entries": len(entries),
        "survivor_sources": Counter("+".join(j["sources"]) for j in survivors),
        "survivor_marks": Counter(m for j in survivors for m in j["marks"]),
    }


def entry_table(entries: list[dict]) -> list[str]:
    rows = ["| title | company | posted | boards | salary | marks |", "| --- | --- | --- | --- | --- | --- |"]
    for e in sorted(entries, key=lambda e: e["posted"] or "", reverse=True):
        salary = next((a["salary_text"] for a in e["ads"] if a["salary_text"]), "")
        title = e["title"] if len(e["variants"]) == 1 else f"{e['title']} (×{len(e['variants'])})"
        rows.append(f"| {title[:70]} | {e['company'][:30]} | {(e['posted'] or '')[:10]} | "
                    f"{'+'.join(e['sources'])} | {salary[:25]} | {', '.join(e['marks'])} |")
    return rows


def experience_table(jobs: list[dict]) -> list[str]:
    rows = ["| title | company | JD says |", "| --- | --- | --- |"]
    for j in jobs:
        if any(r.startswith("experience") for r in j["deny"]):
            rows.append(f"| {j['title'][:60]} | {j['company'][:30]} | {j.get('evidence', '')[:140]} |")
    return rows


def write_run(ads: list[dict], jobs: list[dict], entries: list[dict], summary: dict) -> Path:
    run = RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    with (run / "ads.jsonl").open("w") as f:
        for a in ads:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    with (run / "jobs.jsonl").open("w") as f:
        for j in jobs:
            f.write(json.dumps({k: v for k, v in j.items() if k != "ads"}
                               | {"ads": [f"{a['source']}:{a['source_id']}" for a in j["ads"]]},
                               ensure_ascii=False) + "\n")
    (run / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    lines = [f"# jobs run {run.name}, last {summary['days']} days", "",
             f"{summary['survivors']} of {summary['jobs']} jobs survive the hard denies, "
             f"{summary['entries']} entries once a role posted per product is one.", ""] + entry_table(entries) + [
             "", "## Removed for the experience the JD requires", ""] + experience_table(jobs)
    (run / "index.md").write_text("\n".join(lines) + "\n")
    return run


def print_summary(s: dict) -> None:
    print("\nads: " + ", ".join(f"{k} {v}" for k, v in s["ads"].items()))
    for e in s["errors"]:
        print(f"  ERROR {e}")
    print(f"jobs: {s['jobs']} ({s['on_both_boards']} on both boards)")
    print("denied, any reason:   " + ", ".join(f"{k} {v}" for k, v in s["deny_any"].most_common()))
    print("denied, first reason: " + ", ".join(f"{k} {v}" for k, v in s["deny_first"].most_common()))
    print(f"survivors: {s['survivors']} jobs, {s['entries']} entries — boards: "
          + ", ".join(f"{k} {v}" for k, v in s["survivor_sources"].most_common()))
    print("survivor marks: " + ", ".join(f"{k} {v}" for k, v in s["survivor_marks"].most_common()))


def report() -> None:
    runs = sorted(d for d in RUNS.glob("*/") if (d / "jobs.jsonl").exists())
    if not runs:
        sys.exit("no runs yet — run the collector first")
    print("| run | days | jobs | survivors | entries | new survivors since the run before | errors |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    seen: set[str] = set()
    for d in runs:
        jobs = [json.loads(x) for x in (d / "jobs.jsonl").read_text().splitlines() if x.strip()]
        s = json.loads((d / "summary.json").read_text())
        surv = {j["key"] for j in jobs if not j["deny"]}
        print(f"| {d.name} | {s['days']} | {s['jobs']} | {len(surv)} | {s.get('entries', '-')} | "
              f"{len(surv - seen) if seen else '-'} | {len(s['errors'])} |")
        seen |= surv
    print(f"\nlatest survivors: {runs[-1] / 'index.md'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        report()
        return
    profile = yaml.safe_load((PLUGIN / "profile.yaml").read_text())
    sources.unset_proxy()
    print(f"pulling the last {args.days} days")
    ads, rep = sources.pull_all(profile, args.days)
    jobs = rules.judge_all(ads, profile, read_jd=sources.fetch_jd)
    entries = rules.collapse(jobs)
    summary = summarize(ads, jobs, entries, rep, args.days)
    run = write_run(ads, jobs, entries, summary)
    print_summary(summary)
    print(f"\n→ {run}")


if __name__ == "__main__":
    main()
