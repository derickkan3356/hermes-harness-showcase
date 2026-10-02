# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""How well the local model judges the entries the code layer keeps, against the user's labels.

The model layer is the last filter: code keeps every job that might fit and
folds a role posted per product into one entry; the model decides which
entries are the work the profile describes. This asks the model LM Studio has
loaded — the one Hermes runs — to judge the labelled entries of one run in one
call, as the digest would, and scores the answer against the labels.

It is not the digest: no Hermes, no tools, no `job_read`. `--with-jd` puts
each entry's JD in the prompt as `job_candidates` gives it (`sources.jd_excerpt`).
The instructions are the Judging section of `skills/hk-job-digest/SKILL.md`, so
this measures the text the digest runs on.

Usage:
  uv run evals/jobs/model_eval.py                          # latest run, labels in evals/jobs/labels/
  uv run evals/jobs/model_eval.py --with-jd
  uv run evals/jobs/model_eval.py --run evals/jobs/runs/20260925-040123

Writes evals/jobs/runs/model-<timestamp>/ (not tracked): the prompt, the raw
answer, and one row per entry with the model's verdict and the label.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PLUGIN = REPO / "plugins" / "job_ledger"
sys.path.insert(0, str(PLUGIN / "workers"))

import rules  # noqa: E402
import sources  # noqa: E402

SKILL = REPO / "skills" / "hk-job-digest" / "SKILL.md"

ANSWER = """
Answer with a JSON array and nothing else, one object per entry, in order:
[{{"id": "<entry id>", "keep": true or false, "reason": "<one short sentence>"}}]

Entries:
"""


def judging() -> str:
    """The skill's Judging section: the instructions the digest's model follows."""
    text = SKILL.read_text()
    m = re.search(r"^## Judging\n(.*?)(?=^## )", text, re.S | re.M)
    if not m:
        sys.exit(f"no '## Judging' section in {SKILL}")
    return m.group(1).strip()


def lmstudio_url() -> str:
    """LM Studio's /v1 as Hermes' config names it."""
    cfg = yaml.safe_load((REPO / "config" / "config.yaml").read_text())
    for block in (cfg.get("model") or {}, *(cfg.get("providers") or {}).values()):
        if isinstance(block, dict):
            url = block.get("base_url") or block.get("api_base")
            if url and ":1234" in url:
                return url.rstrip("/")
    sys.exit("no LM Studio base_url in config/config.yaml")


def reasoning_effort() -> str:
    """The effort Hermes asks for (`agent.reasoning_effort`), so the eval measures the same model call."""
    cfg = yaml.safe_load((REPO / "config" / "config.yaml").read_text())
    return (cfg.get("agent") or {}).get("reasoning_effort") or "medium"


def chat(url: str, prompt: str) -> tuple[str, dict, float]:
    models = json.load(urllib.request.urlopen(f"{url}/models", timeout=30))
    model = models["data"][0]["id"]
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "reasoning_effort": reasoning_effort(), "max_tokens": 32000}
    req = urllib.request.Request(f"{url}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    started = time.time()
    out = json.load(urllib.request.urlopen(req, timeout=3600))
    return out["choices"][0]["message"].get("content") or "", \
        {"model": model, "reasoning_effort": body["reasoning_effort"], **(out.get("usage") or {})}, \
        time.time() - started


def entries_for(run: Path, labels: dict[str, dict], profile: dict, with_jd: bool,
                labelled_only: bool = False) -> list[dict]:
    ads = {f"{a['source']}:{a['source_id']}": a for a in map(json.loads, (run / "ads.jsonl").open())}
    jobs = []
    for j in map(json.loads, (run / "jobs.jsonl").open()):
        j["ads"] = [ads[k] for k in j["ads"]]
        jobs.append(j)
    if labelled_only:
        # Every labelled job of the run, whatever the rules did with it, one entry each.
        pool = [{**j, "deny": [], "variants": [j["title"]], "keys": [j["key"]]}
                for j in jobs if j["key"] in labels]
    else:
        pool = rules.collapse(jobs)
    out = []
    for i, e in enumerate(pool, 1):
        got = {labels[k]["label"] for k in e["keys"] if k in labels}
        if len(got) != 1:
            continue   # unlabelled, or a fold that mixes labels — not scorable
        teaser = next((a["teaser"] for a in e["ads"] if a["teaser"]), "")
        bullets = [b for a in e["ads"] for b in a["bullets"]][:4]
        jd = next((labels[k]["jd"] for k in e["keys"] if labels.get(k, {}).get("jd")), "")
        lines = [f"[E{i}] {e['title']}" + (f" (also posted as: {'; '.join(e['variants'][1:])})" if len(e["variants"]) > 1 else ""),
                 f"Company: {e['company']}", f"Marks: {', '.join(e['marks']) or '-'}"]
        salary = next((a["salary_text"] for a in e["ads"] if a["salary_text"]), "")
        if salary:
            lines.append(f"Salary: {salary}")
        if teaser:
            lines.append(f"Teaser: {teaser}")
        if bullets:
            lines.append("Highlights: " + " | ".join(bullets))
        if with_jd and jd:
            lines.append(f"JD: {sources.jd_excerpt(jd)}")
        out.append({"id": f"E{i}", "label": got.pop(), "jobs": len(e["keys"]), "title": e["title"],
                    "company": e["company"], "text": "\n".join(lines)})
    return out


def parse(answer: str) -> dict[str, dict]:
    m = re.search(r"\[\s*\{.*\}\s*\]", answer, re.S)
    if not m:
        return {}
    try:
        return {str(r.get("id")): r for r in json.loads(m.group(0))}
    except ValueError:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="an evals/jobs/runs/<ts> directory; default the latest collection run")
    ap.add_argument("--labels", default=str(HERE / "labels" / "20260925.jsonl"))
    ap.add_argument("--with-jd", action="store_true")
    ap.add_argument("--profile", default=str(PLUGIN / "profile.yaml"), help="a profile to try instead of the plugin's")
    ap.add_argument("--labelled-only", action="store_true",
                    help="judge every labelled job of the run, one entry each, instead of the survivors")
    args = ap.parse_args()
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(var, None)   # LM Studio is on the tailnet, not behind the shell's proxy

    run = Path(args.run) if args.run else max(d for d in (HERE / "runs").glob("2*/") if (d / "jobs.jsonl").exists())
    labels = {r["key"]: r for r in map(json.loads, open(args.labels))}
    profile = yaml.safe_load(Path(args.profile).read_text())
    entries = entries_for(run, labels, profile, args.with_jd, args.labelled_only)
    not_wanted = profile.get("not_wanted") or []
    prompt = ("You screen Hong Kong job ads for one person.\n\n" + judging()
              + "\n\nThe person's profile:\n" + " ".join(profile["description"].split())
              + ("\n\nWork this person does not want:\n" + "\n".join(f"- {n}" for n in not_wanted) if not_wanted else "")
              + "\n" + ANSWER + "\n\n".join(e["text"] for e in entries))

    url = lmstudio_url()
    print(f"{len(entries)} labelled entries from {run.name}, {'with' if args.with_jd else 'without'} JD, "
          f"{len(prompt)} chars → {url}")
    answer, usage, secs = chat(url, prompt)
    verdicts = parse(answer)

    out = HERE / "runs" / f"model-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    (out / "prompt.txt").write_text(prompt)
    (out / "answer.txt").write_text(answer)
    rows = []
    for e in entries:
        v = verdicts.get(e["id"], {})
        rows.append({**{k: e[k] for k in ("id", "label", "jobs", "title", "company")},
                     "keep": v.get("keep"), "reason": v.get("reason", "")})
    (out / "verdicts.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    meta = {"run": run.name, "with_jd": args.with_jd, "profile": args.profile,
            "labelled_only": args.labelled_only, "labels": args.labels, "seconds": round(secs), **usage}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))

    yes = [r for r in rows if r["label"] == "yes"]
    no = [r for r in rows if r["label"] == "no"]
    missing = [r for r in rows if r["keep"] is None]
    print(f"{usage.get('model')} in {secs:.0f}s, tokens {usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out")
    print(f"worth opening kept: {sum(r['keep'] is True for r in yes)}/{len(yes)} entries "
          f"({sum(r['jobs'] for r in yes if r['keep'] is True)}/{sum(r['jobs'] for r in yes)} jobs)")
    print(f"not worth opening dropped: {sum(r['keep'] is False for r in no)}/{len(no)} entries "
          f"({sum(r['jobs'] for r in no if r['keep'] is False)}/{sum(r['jobs'] for r in no)} jobs)")
    if missing:
        print(f"no verdict for {len(missing)} entries: {', '.join(r['id'] for r in missing)}")
    for r in rows:
        if (r["label"] == "yes") != (r["keep"] is True):
            print(f"  {'MISSED' if r['label'] == 'yes' else 'kept  '} {r['id']:<4} {r['title'][:55]:<55} | {r['reason'][:90]}")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
