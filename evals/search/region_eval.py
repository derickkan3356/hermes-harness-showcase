# /// script
# requires-python = ">=3.11"
# dependencies = ["ddgs==9.15.0", "pyyaml"]
# ///
"""Does a search provider's region parameter change results, and does the provider stay up?

Providers: ddgs (keyless scraper), serper and scrappa (Google results via a paid
scraper), brave (own index; `brave` sends country only, `brave-lang` adds
search_lang, which filters by content language, `brave-rewrite` sends country and
puts the region's place name in front of the query text). Keys:
SERPER_API_KEY, SCRAPPA_API_KEY, BRAVE_SEARCH_API_KEY, read from the environment or
~/.hermes/.env. scrappa can fall back to other engines; each row
records `engine`.

For every query, three conditions:
  base_a, base_b  no region: ddgs us-en (what Hermes sends today), serper defaults. Run twice.
  region          the region's parameters from queries.yaml (ddgs code, serper gl/hl).

ddgs shuffles its engines on every call, so base_a vs base_b is the noise floor.
Region only matters if base_a vs region differs clearly more than that, and the
in-region share goes up. --no-noise skips base_b to save paid credits.

Local queries use their own region. Ambiguous queries run under every region
(base_a/base_b once per query, shared across regions).

Usage:
  uv run evals/search/region_eval.py                 # full set, backend auto
  uv run evals/search/region_eval.py --backend bing  # one ddgs engine only
  uv run evals/search/region_eval.py --provider scrappa --no-noise
  uv run evals/search/region_eval.py --only hk-hko-9day,amb-weather-en
  uv run evals/search/region_eval.py --summarize evals/search/runs/<file>.jsonl
  uv run evals/search/region_eval.py --compare evals/search/runs/<a>.jsonl evals/search/runs/<b>.jsonl

Raw results go to evals/search/runs/ (not tracked).

Engines localize by the exit IP. Run it with the same egress Hermes has (check the
egress line); a proxy in the shell changes the baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from ddgs import DDGS

HERE = Path(__file__).parent
BASELINE_REGION = "us-en"
FETCH = 10  # Hermes asks ddgs for a bucketed 10
TOP = 5  # Hermes shows 5 by default
UA = "hermes-harness-eval/0.1"  # Cloudflare in front of scrappa blocks the Python-urllib default (error 1010)


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def in_region(url: str, domains: list[str]) -> bool:
    h = host(url)
    return any(h == d.lstrip(".") or h.endswith(d if d.startswith(".") else "." + d) for d in domains)


def jaccard(a: list[str], b: list[str]) -> float | None:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return None
    return len(sa & sb) / len(sa | sb)


def secret(name: str) -> str:
    """Env var, else the same key from ~/.hermes/.env. Never printed."""
    if os.environ.get(name):
        return os.environ[name]
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip().removeprefix("export ").strip() == name:
                return v.strip().strip("'\"")
    raise SystemExit(f"{name} is not set (environment or ~/.hermes/.env)")


def ddgs_search(query: str, params: dict, backend: str) -> list[str]:
    hits = DDGS(timeout=10).text(query, region=params.get("region", BASELINE_REGION), max_results=FETCH, backend=backend)
    urls = [str(h.get("href") or h.get("url") or "") for h in hits]
    if not urls:
        raise RuntimeError("0 results")
    return {"urls": urls}


def serper_search(query: str, params: dict, _backend: str) -> list[str]:
    body = json.dumps({"q": query, "num": FETCH, **params}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=body,
        headers={"X-API-KEY": secret("SERPER_API_KEY"), "Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    return {"urls": [str(h.get("link", "")) for h in data.get("organic", [])]}


def scrappa_search(query: str, params: dict, _backend: str) -> dict:
    qs = urllib.parse.urlencode({"query": query, "amount": FETCH, **params})
    req = urllib.request.Request(
        f"https://scrappa.co/api/search?{qs}",
        headers={"x-api-key": secret("SCRAPPA_API_KEY"), "Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    return {
        "urls": [str(h.get("link", "")) for h in data.get("organic_results") or []],
        "engine": data.get("engine_used") or data.get("service_used"),
    }


def brave_search(query: str, params: dict, _backend: str) -> dict:
    qs = urllib.parse.urlencode({"q": query, "count": FETCH, **params})
    req = urllib.request.Request(
        f"https://api.search.brave.com/res/v1/web/search?{qs}",
        headers={"X-Subscription-Token": secret("BRAVE_SEARCH_API_KEY"), "Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    return {"urls": [str(h.get("url", "")) for h in (data.get("web") or {}).get("results", [])]}


def brave_rewrite_search(query: str, params: dict, backend: str) -> dict:
    """Brave with the region's place name pushed into the query text.

    `country` alone barely moves an ambiguous query. This is the other lever:
    search `香港 weather forecast` instead of `weather forecast`. The place is
    skipped when the query already contains it, so a local query is unchanged.
    """
    params = dict(params)
    place = params.pop("_place", "")
    if place and place not in query:
        query = f"{place} {query}"
    out = brave_search(query, params, backend)
    return {**out, "sent_query": query}


PROVIDERS = {
    "ddgs": ddgs_search,
    "serper": serper_search,
    "scrappa": scrappa_search,
    "brave": brave_search,
    "brave-lang": brave_search,
    "brave-rewrite": brave_rewrite_search,
}
KEYS = {
    "serper": "SERPER_API_KEY",
    "scrappa": "SCRAPPA_API_KEY",
    "brave": "BRAVE_SEARCH_API_KEY",
    "brave-lang": "BRAVE_SEARCH_API_KEY",
    "brave-rewrite": "BRAVE_SEARCH_API_KEY",
}
# Key in queries.yaml `regions.<r>` that holds each provider's region parameters.
REGION_KEY = {"serper": "google", "scrappa": "google", "brave": "brave", "brave-lang": "brave_lang", "brave-rewrite": "brave"}


def region_params(provider: str, region_cfg: dict | None) -> dict:
    if region_cfg is None:
        return {}
    if provider == "ddgs":
        return {"region": region_cfg["ddgs"]}
    params = dict(region_cfg[REGION_KEY[provider]])
    if provider == "brave-rewrite":
        # Consumed by brave_rewrite_search, never sent to Brave.
        params["_place"] = region_cfg["place"]
    return params


def search(provider: str, query: str, params: dict, backend: str) -> dict:
    t0 = time.monotonic()
    try:
        out, err = PROVIDERS[provider](query, params, backend), None
    except urllib.error.HTTPError as exc:
        out, err = {}, f"HTTPError {exc.code}: {exc.read()[:200].decode(errors='replace')}"
    except Exception as exc:  # noqa: BLE001 — record every failure, keep going
        out, err = {}, f"{type(exc).__name__}: {exc}"
    urls = out.pop("urls", [])[:TOP]
    return {"urls": urls, **out, "error": err, "secs": round(time.monotonic() - t0, 2)}


def egress() -> str:
    """Where search traffic exits. Engines localize by IP, so this is part of the result."""
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=8) as r:
            d = json.load(r)
        return f"{d.get('country')} {d.get('city')} ({d.get('org')})"
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({type(exc).__name__})"


def plan_calls(cfg: dict, provider: str, only: set[str] | None, noise: bool) -> list[dict]:
    calls = []
    for q in cfg["queries"]:
        if only and q["id"] not in only:
            continue
        targets = [q["region"]] if q["kind"] == "local" else list(cfg["regions"])
        base = {"id": q["id"], "kind": q["kind"], "lang": q["lang"], "query": q["query"]}
        for cond in ("base_a", "base_b") if noise else ("base_a",):
            calls.append({**base, "cond": cond, "params": region_params(provider, None)})
        for r in targets:
            calls.append({**base, "cond": "region", "target": r, "params": region_params(provider, cfg["regions"][r])})
    return calls


def run(cfg: dict, args) -> Path:
    calls = plan_calls(cfg, args.provider, set(args.only.split(",")) if args.only else None, args.noise)
    if args.provider in KEYS:
        secret(KEYS[args.provider])  # fail before any call
    label = args.provider if args.provider != "ddgs" else f"ddgs-{args.backend}"
    out_dir = HERE / "runs"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"region-{label}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    where = egress()
    print(f"{len(calls)} calls, provider={label}, delay={args.delay}s, egress={where} -> {out}")
    with out.open("w") as f:
        f.write(json.dumps({"meta": True, "egress": where, "provider": label}) + "\n")
        for i, c in enumerate(calls, 1):
            res = search(args.provider, c["query"], c["params"], args.backend)
            row = {**c, **res, "provider": label}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            mark = "ERR" if res["error"] else f"{len(res['urls'])} urls" + (f" via {res['engine']}" if res.get("engine") else "")
            shown = ",".join(f"{v}" for v in c["params"].values()) or "-"
            print(f"[{i}/{len(calls)}] {c['id']:<28} {c['cond']:<6} {shown:<10} {mark}", flush=True)
            time.sleep(args.delay)
    return out


def load(path: Path) -> tuple[dict, list[dict]]:
    rows = [json.loads(line) for line in path.open()]
    meta = next((r for r in rows if r.get("meta")), {})
    return meta, [r for r in rows if not r.get("meta")]


def score(cfg: dict, rows: list[dict]) -> list[dict]:
    """One line per (query, target region)."""
    regions = cfg["regions"]
    by_id: dict[str, dict] = {}
    for r in rows:
        d = by_id.setdefault(r["id"], {"kind": r["kind"], "query": r["query"], "region": {}})
        if r["cond"] == "region":
            d["region"][r["target"]] = r
        else:
            d[r["cond"]] = r

    lines = []
    for qid, d in by_id.items():
        a, b = d.get("base_a"), d.get("base_b")
        if not a:
            continue
        for target, reg in d["region"].items():
            dom = regions[target]["domains"]
            share = lambda row: None if row["error"] or not row["urls"] else sum(in_region(u, dom) for u in row["urls"]) / len(row["urls"])  # noqa: E731
            base_share = [s for s in (share(a), share(b) if b else None) if s is not None]
            ok = lambda *rs: all(not r["error"] for r in rs)  # noqa: E731
            lines.append({
                "id": qid, "kind": d["kind"], "target": target,
                "noise": jaccard(a["urls"], b["urls"]) if b and ok(a, b) else None,
                "effect": jaccard(a["urls"], reg["urls"]) if ok(a, reg) else None,
                "base_share": statistics.mean(base_share) if base_share else None,
                "reg_share": share(reg),
                "base_hit": any(s > 0 for s in base_share),
                "reg_hit": bool(share(reg)),
            })
    return lines


def groups_of(lines: list[dict]) -> dict[tuple, list]:
    groups: dict[tuple, list] = {}
    for ln in lines:
        groups.setdefault((ln["kind"], ln["target"]), []).append(ln)
    return dict(sorted(groups.items()))


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def f(x):
    return "  -  " if x is None else f"{x:5.2f}"


def summarize(cfg: dict, path: Path) -> None:
    meta, rows = load(path)
    errors = [r for r in rows if r["error"]]
    empties = [r for r in rows if not r["error"] and not r["urls"]]
    print(f"\n{path.name}: provider {meta.get('provider') or meta.get('backend')}, egress {meta.get('egress', 'not recorded')}")
    print(f"{len(rows)} calls, {len(errors)} errors, {len(empties)} empty")
    engines = {}
    for r in rows:
        if r.get("engine"):
            engines[r["engine"]] = engines.get(r["engine"], 0) + 1
    if engines:
        print(f"engines used: {engines}")
    for r in errors[:10]:
        print(f"  ERR {r['id']} {r['cond']} {r.get('params') or r.get('region_ddgs')}: {r['error'][:120]}")

    lines = score(cfg, rows)
    print("\nnoise  = URL overlap base_a vs base_b (both without region). 1.00 = identical.")
    print("effect = URL overlap base_a vs region. Region matters if effect is clearly below noise.")
    print("share  = in-region fraction of top 5.\n")
    print(f"{'id':<28} {'target':<6} {'noise':>5} {'effect':>6} {'base%':>5} {'reg%':>5}")
    for ln in lines:
        print(f"{ln['id']:<28} {ln['target']:<6} {f(ln['noise'])} {f(ln['effect']):>6} {f(ln['base_share'])} {f(ln['reg_share'])}")

    print(f"\n{'group':<18} {'n':>3} {'noise':>5} {'effect':>6} {'base%':>5} {'reg%':>5} {'base hit':>8} {'reg hit':>7}")
    for (kind, target), g in groups_of(lines).items():
        print(
            f"{kind + '/' + target:<18} {len(g):>3} {f(mean(x['noise'] for x in g))} "
            f"{f(mean(x['effect'] for x in g)):>6} {f(mean(x['base_share'] for x in g))} "
            f"{f(mean(x['reg_share'] for x in g))} "
            f"{sum(x['base_hit'] for x in g):>4}/{len(g):<3} {sum(x['reg_hit'] for x in g):>3}/{len(g):<3}"
        )


def compare(cfg: dict, paths: list[Path]) -> None:
    """Side by side: one column block per run. Cells are in-region share with region, and hits."""
    runs = []
    for path in paths:
        meta, rows = load(path)
        ok = [r["secs"] for r in rows if not r["error"]]
        runs.append({
            "name": meta.get("provider") or meta.get("backend") or path.stem,
            "errors": sum(bool(r["error"]) for r in rows),
            "calls": len(rows),
            "median": statistics.median(ok) if ok else None,
            "groups": groups_of(score(cfg, rows)),
        })
    w = 16
    print(f"{'':<18}" + "".join(f"{r['name']:>{w}}" for r in runs))
    print(f"{'errors':<18}" + "".join(f"{str(r['errors']) + '/' + str(r['calls']):>{w}}" for r in runs))
    print(f"{'median secs':<18}" + "".join(f"{f(r['median']):>{w}}" for r in runs))
    print("\ngroup: in-region share of top 5 with region, failed calls left out (hits / n, failed = miss)")
    for key in runs[0]["groups"]:
        cells = []
        for r in runs:
            g = r["groups"].get(key, [])
            cells.append(f"{f(mean(x['reg_share'] for x in g))} ({sum(x['reg_hit'] for x in g)}/{len(g)})")
        print(f"{key[0] + '/' + key[1]:<18}" + "".join(f"{c:>{w}}" for c in cells))
    print("\ngroup: in-region share without region")
    for key in runs[0]["groups"]:
        cells = [f(mean(x["base_share"] for x in r["groups"].get(key, []))) for r in runs]
        print(f"{key[0] + '/' + key[1]:<18}" + "".join(f"{c:>{w}}" for c in cells))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--queries", type=Path, default=HERE / "queries.yaml")
    p.add_argument("--provider", choices=sorted(PROVIDERS), default="ddgs")
    p.add_argument("--backend", default="auto", help="ddgs only: auto (Hermes default), bing, google, brave, duckduckgo, ...")
    p.add_argument("--no-noise", dest="noise", action="store_false", help="skip the repeat baseline (saves paid calls)")
    p.add_argument("--delay", type=float, default=1.5, help="seconds between calls")
    p.add_argument("--only", help="comma-separated query ids")
    p.add_argument("--summarize", type=Path, help="summarize an existing run file, no searching")
    p.add_argument("--compare", type=Path, nargs="+", help="compare existing run files side by side, no searching")
    args = p.parse_args()

    cfg = yaml.safe_load(args.queries.read_text())
    if args.compare:
        compare(cfg, args.compare)
        return
    path = args.summarize or run(cfg, args)
    summarize(cfg, path)


if __name__ == "__main__":
    main()
