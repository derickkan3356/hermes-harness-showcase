# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "httpx>=0.27", "lxml>=5.0"]
# ///
"""What each fetch layer actually gets, on real pages, with the text kept for reading.

Runs `urls.yaml` through the `local-cascade` provider and writes every extracted
page to disk next to the URL that produced it, so a result can be checked by
opening the link and comparing. Numbers alone cannot tell a thin page from a bad
read; that judgement needs a person, and this run is what gives them the two
things side by side.

The provider is called directly, so Hermes' extract cache is not in the way and
every run is a real fetch.

`urls.yaml` holds landing pages and a few pinned articles. Landing pages are what
the set can compare run against run; articles are the shape an agent actually
reads, since it reaches one from a search result rather than from a front page.
Pinned article URLs rot, so `--discover N` harvests N fresh ones per source in
`sources.yaml` at run time instead — those cannot be compared between runs, and
`--compare` leaves them out.

`--provider exa` runs the same URLs through Exa's `/contents` API instead, judged
by the same quality gate, so the local cascade can be read against a paid fetch
service on identical terms. Exa bills about $0.001 per page and the run prints
what it spent. `EXA_API_KEY` is read from the environment or `~/.hermes/.env`.

Usage:
  uv run evals/fetch/fetch_eval.py
  uv run evals/fetch/fetch_eval.py --only hko-9day,zhihu
  uv run evals/fetch/fetch_eval.py --kind mainland,wall
  uv run evals/fetch/fetch_eval.py --batch 5
  uv run evals/fetch/fetch_eval.py --discover 2
  uv run evals/fetch/fetch_eval.py --provider exa
  uv run evals/fetch/fetch_eval.py --summarize evals/fetch/runs/<run>/results.jsonl
  uv run evals/fetch/fetch_eval.py --compare evals/fetch/runs/<a> evals/fetch/runs/<b>

Each run writes evals/fetch/runs/<timestamp>/ (not tracked):

  index.md        the table to open first — one row per URL, linking to its text
  pages/<id>.md   the extracted text, headed by the URL and the layer trail
  results.jsonl   one record per URL
  cascade.log     the provider's own FETCH_CASCADE_LOG for the run

Egress matters: a site that localizes or blocks by IP answers this machine, not a
proxy. Unset HTTPS_PROXY/HTTP_PROXY before running or the walls tested here are
the wrong ones.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import sys
import time
import re
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).parent
REPO = HERE.parent.parent
PROVIDER = REPO / "plugins" / "fetch_cascade" / "provider.py"
HERMES = Path.home() / ".hermes" / "hermes-agent"


def load_provider():
    """Import the live provider module, not a copy.

    The eval must exercise what Hermes runs. `WebSearchProvider` comes from the
    Hermes tree; it is a plain ABC, so putting that tree on the path is enough
    and no part of Hermes' venv is needed.
    """
    sys.path.insert(0, str(HERMES))
    spec = importlib.util.spec_from_file_location("fetch_cascade_provider", PROVIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover(sources: list[dict], per_source: int) -> list[dict]:
    """Harvest live article URLs from each source's index page.

    Reads the index page's server HTML, so a site that writes its article links
    in with JavaScript yields nothing here; `sources.yaml` says which ones.
    """
    import httpx
    from lxml import html as lxml_html
    from urllib.parse import urljoin

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
    found: list[dict] = []
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
        for source in sources:
            try:
                tree = lxml_html.fromstring(client.get(source["url"]).text)
            except Exception as exc:  # noqa: BLE001 — a dead index is not a test failure
                print(f"  discover {source['id']}: index unreachable ({type(exc).__name__})",
                      file=sys.stderr)
                continue
            pattern = re.compile(source["pattern"])
            seen: list[str] = []
            for anchor in tree.iter("a"):
                url = urljoin(source["url"], anchor.get("href") or "").split("#")[0]
                if pattern.search(url) and url not in seen:
                    seen.append(url)
                if len(seen) >= per_source:
                    break
            if not seen:
                print(f"  discover {source['id']}: no link matched the pattern",
                      file=sys.stderr)
            for index, url in enumerate(seen, 1):
                found.append({
                    "id": f"found-{source['id']}-{index}",
                    "url": url,
                    "kind": source.get("kind", "article"),
                    "note": f"Harvested from {source['url']} at run time.",
                })
    return found


def exa_key() -> str:
    key = os.environ.get("EXA_API_KEY", "").strip()
    if key:
        return key
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("EXA_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("EXA_API_KEY not found in the environment or ~/.hermes/.env")


def exa_error(status: dict) -> str:
    """Exa's per-URL error, which is sometimes a string and sometimes an object."""
    err = status.get("error")
    if isinstance(err, dict):
        return str(err.get("tag") or err.get("message") or err)
    return str(err)


class ExaProvider:
    """Exa's `/contents` API behind the same interface as the local cascade.

    Returns the same result shape, so the same quality gate and the same summary
    read both. Exa has no layers to report, so every result is `via: exa`, and
    `source_bytes` is unknowable from the API — the ratio column is empty for it.
    """

    name = "exa"

    def __init__(self, livecrawl: str = "always") -> None:
        self.key = exa_key()
        self.livecrawl = livecrawl
        self.spent = 0.0
        self.sources: dict[str, int] = {}

    def extract(self, urls: list[str], **_kwargs) -> list[dict]:
        import httpx

        try:
            response = httpx.post(
                "https://api.exa.ai/contents",
                headers={"x-api-key": self.key, "Content-Type": "application/json"},
                json={"urls": urls, "text": True, "livecrawl": self.livecrawl,
                      "livecrawlTimeout": 15000},
                timeout=180.0,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — a dead API is a result too
            return [{"url": u, "title": "", "content": "",
                     "error": f"Exa request failed: {exc}"} for u in urls]

        self.spent += float((payload.get("costDollars") or {}).get("total") or 0)
        by_url = {r.get("url") or r.get("id"): r for r in payload.get("results") or []}
        statuses = {s.get("id"): s for s in payload.get("statuses") or []}

        results = []
        for url in urls:
            status = statuses.get(url) or {}
            self.sources[status.get("source") or "unknown"] = (
                self.sources.get(status.get("source") or "unknown", 0) + 1)
            row = by_url.get(url)
            if row is None or status.get("status") != "success":
                results.append({
                    "url": url, "title": "", "content": "",
                    "error": f"Exa returned {status.get('status') or 'no result'}"
                             + (f": {exa_error(status)}" if status.get("error") else ""),
                })
                continue
            text = row.get("text") or ""
            results.append({
                "url": url,
                "title": row.get("title") or "",
                "content": text,
                "raw_content": text,
                "metadata": {"via": "exa", "source": status.get("source")},
            })
        return results


def page_file(run_dir: Path, entry: dict, result: dict, record: dict, body: str) -> Path:
    """Write one page's text with enough header to check it against the live URL."""
    path = run_dir / "pages" / f"{entry['id']}.md"
    trail = "\n".join(f"  {line}" for line in record["trail"])
    path.write_text(
        f"# {entry['id']}\n\n"
        f"- URL: <{entry['url']}>\n"
        f"- Kind: {entry['kind']}\n"
        f"- Expected: {entry['note']}\n"
        f"- Served by: {record['via'] or 'nothing — hard failure'}\n"
        f"- Characters: {record['chars']}   Ratio: {record['ratio']}\n"
        f"- Layers:\n{trail}\n"
        f"{'- Error: ' + record['error'] if record['error'] else ''}\n"
        f"\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def read_cascade_log(path: Path) -> dict[str, list[dict]]:
    """Group the provider's per-attempt lines by URL."""
    by_url: dict[str, list[dict]] = {}
    if not path.exists():
        return by_url
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        by_url.setdefault(row.get("url", ""), []).append(row)
    return by_url


def run(entries: list[dict], batch_size: int, provider_name: str = "local",
        livecrawl: str = "always") -> Path:
    module = load_provider()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = provider_name + (f"-{livecrawl}" if provider_name == "exa" else "")
    run_dir = HERE / "runs" / f"{stamp}-{suffix}"
    (run_dir / "pages").mkdir(parents=True, exist_ok=True)
    cascade_log = run_dir / "cascade.log"
    os.environ["FETCH_CASCADE_LOG"] = str(cascade_log)

    provider = (ExaProvider(livecrawl) if provider_name == "exa"
                else module.LocalCascadeProvider())
    results: dict[str, dict] = {}
    started = time.time()

    # Batched the way Hermes calls it, so one browser start serves every
    # escalation in the batch rather than one per URL.
    for start in range(0, len(entries), batch_size):
        chunk = entries[start:start + batch_size]
        urls = [e["url"] for e in chunk]
        print(f"  [{start + 1}-{start + len(chunk)}/{len(entries)}] fetching…", flush=True)
        t0 = time.time()
        for entry, result in zip(chunk, provider.extract(urls, format="markdown")):
            results[entry["id"]] = result
        print(f"      {time.time() - t0:.1f}s", flush=True)

    elapsed = time.time() - started
    attempts = read_cascade_log(cascade_log)

    records = []
    for entry in entries:
        result = results[entry["id"]]
        rows = attempts.get(entry["url"], [])
        graded = [r for r in rows if r.get("outcome") in ("accepted", "escalated")]
        via = (result.get("metadata") or {}).get("via")
        # The provider fences page text as untrusted before the agent sees it.
        # Read quality is a property of the text, not of the fence, so measure
        # and file the text without it.
        text = module.strip_untrusted(result.get("content") or "")
        accepted = next((r for r in graded if r.get("outcome") == "accepted"), None)

        if provider_name == "exa":
            # Exa keeps no per-layer log, so its one result is graded here by the
            # same gate the cascade's layers answer to.
            reason = (result.get("error") or
                      module.gate_failure({"text": text, "title": result.get("title")}))
            graded = [{"layer": "exa", "gate": reason,
                       "outcome": "accepted" if reason is None else "escalated",
                       "ratio": None}]
            accepted = graded[0] if reason is None else None
            via = "exa" if reason is None else None

        record = {
            "id": entry["id"],
            "url": entry["url"],
            "kind": entry["kind"],
            "note": entry["note"],
            "via": via,
            "outcome": ("accepted" if accepted else
                        "degraded" if via else
                        "failed"),
            "chars": len(text),
            "ratio": (accepted or (graded[-1] if graded else {})).get("ratio"),
            "layers": [r.get("layer") for r in graded],
            "trail": [f"{r.get('layer')}: {r.get('gate') or 'ok'}" for r in graded],
            "error": result.get("error") or "",
        }
        record["page"] = str(
            page_file(run_dir, entry, result, record, text).relative_to(run_dir))
        records.append(record)

    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    write_index(run_dir, records, elapsed)
    summarize(records, elapsed)
    if isinstance(provider, ExaProvider):
        print(f"\nExa spent ${provider.spent:.3f}; served from "
              + ", ".join(f"{k} {v}" for k, v in sorted(provider.sources.items())))
    print(f"\nOpen {run_dir / 'index.md'}")
    return run_dir


def write_index(run_dir: Path, records: list[dict], elapsed: float) -> None:
    lines = [
        f"# Fetch eval — {run_dir.name}",
        "",
        f"{len(records)} URLs in {elapsed:.0f}s. Open each URL beside its extracted text "
        "and mark whether the text is the page.",
        "",
        "| id | kind | served by | chars | ratio | text | url |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for r in records:
        via = r["via"] or "**failed**"
        lines.append(
            f"| {r['id']} | {r['kind']} | {via} | {r['chars']} | "
            f"{r['ratio'] if r['ratio'] is not None else '—'} | "
            f"[text]({r['page']}) | <{r['url']}> |"
        )
    lines += ["", "## Layer trail", ""]
    for r in records:
        lines.append(f"- **{r['id']}** — {'; '.join(r['trail']) or 'no layer ran'}"
                     + (f" — error: {r['error']}" if r["error"] else ""))
    (run_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def summarize(records: list[dict], elapsed: float | None = None) -> None:
    total = len(records)
    by_via: dict[str, int] = {}
    for r in records:
        by_via[r["via"] or "failed"] = by_via.get(r["via"] or "failed", 0) + 1

    print(f"\n{total} URLs" + (f" in {elapsed:.0f}s" if elapsed else ""))
    print("\nServed by:")
    for via, count in sorted(by_via.items(), key=lambda kv: -kv[1]):
        print(f"  {via:<24} {count:>3}  ({count / total:.0%})")

    print("\nBy kind:")
    kinds: dict[str, list[dict]] = {}
    for r in records:
        kinds.setdefault(r["kind"], []).append(r)
    for kind, rows in sorted(kinds.items()):
        clean = sum(1 for r in rows if r["outcome"] == "accepted")
        degraded = sum(1 for r in rows if r["outcome"] == "degraded")
        failed = sum(1 for r in rows if r["outcome"] == "failed")
        print(f"  {kind:<12} {len(rows):>2} urls   clean {clean}   degraded {degraded}   failed {failed}")

    articles = [r for r in records if r["kind"] == "article"]
    landing = [r for r in records if r["kind"] != "article"]
    if articles and landing:
        print("\nBy page shape:")
        for label, rows in (("landing pages", landing), ("articles", articles)):
            clean = sum(1 for r in rows if r["outcome"] == "accepted")
            by_layer: dict[str, int] = {}
            for r in rows:
                by_layer[r["via"] or "none"] = by_layer.get(r["via"] or "none", 0) + 1
            layers = "  ".join(f"{k} {v}" for k, v in sorted(by_layer.items()))
            print(f"  {label:<14} {len(rows):>2} urls   clean {clean:<3} {layers}")

    ratios = [r["ratio"] for r in records if r.get("ratio") is not None]
    if ratios:
        clean = sorted(r["ratio"] for r in records
                       if r["outcome"] == "accepted" and r.get("ratio") is not None)
        bad = sorted(r["ratio"] for r in records
                     if r["outcome"] != "accepted" and r.get("ratio") is not None)
        print("\nExtraction ratio (chars per source byte):")
        print(f"  accepted   n={len(clean):<3} min {min(clean):.4f}  median {statistics.median(clean):.4f}  max {max(clean):.4f}" if clean else "  accepted   none")
        print(f"  not clean  n={len(bad):<3} min {min(bad):.4f}  median {statistics.median(bad):.4f}  max {max(bad):.4f}" if bad else "  not clean  none")

    print("\nLayer 3 candidates — pages a rendered read did not open:")
    for r in records:
        if r["outcome"] == "accepted":
            continue
        last = (r["trail"][-1] if r["trail"] else "").lower()
        if "http 4" in last:
            label = "refused outright"
        elif "challenge" in last:
            label = "challenge page"
        elif "chromium" in last:
            label = "rendered but thin"
        else:
            label = "no rendered attempt"
        print(f"  {r['id']:<18} {label:<20} {r['trail'][-1] if r['trail'] else '—'}"[:120])


def compare(left: Path, right: Path) -> None:
    """Two runs of the same URL set, side by side, by what each one got clean."""
    def load(run: Path) -> dict[str, dict]:
        path = run / "results.jsonl" if run.is_dir() else run
        return {json.loads(line)["id"]: json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}

    a, b = load(left), load(right)
    # Harvested URLs differ between runs, so there is nothing to line up.
    a = {k: v for k, v in a.items() if not k.startswith("found-")}
    b = {k: v for k, v in b.items() if not k.startswith("found-")}
    a_name, b_name = left.name, right.name
    tally = {"both": 0, "left": 0, "right": 0, "neither": 0}
    rows = []
    for key, la in a.items():
        rb = b.get(key)
        if rb is None:
            continue
        ok_a, ok_b = la["outcome"] == "accepted", rb["outcome"] == "accepted"
        tag = ("both" if ok_a and ok_b else "left" if ok_a
               else "right" if ok_b else "neither")
        tally[tag] += 1
        if tag != "both":
            rows.append((tag, key, la, rb))

    print(f"left  {a_name}\nright {b_name}\n")
    print(f"both clean {tally['both']}   only left {tally['left']}   "
          f"only right {tally['right']}   neither {tally['neither']}\n")
    print(f"{'':<9}{'id':<17}{'kind':<10}{'left':>19}{'right':>19}")
    for tag, key, la, rb in sorted(rows):
        print(f"{tag:<9}{key:<17}{la['kind']:<10}"
              f"{la['outcome'][:8]:>9}{la['chars']:>10}"
              f"{rb['outcome'][:8]:>9}{rb['chars']:>10}")
    for name, run in ((a_name, a), (b_name, b)):
        counts = {k: sum(1 for v in run.values() if v["outcome"] == k)
                  for k in ("accepted", "degraded", "failed")}
        print(f"\n{name}: clean {counts['accepted']}  degraded {counts['degraded']}  "
              f"failed {counts['failed']}  (text returned at all: "
              f"{sum(1 for v in run.values() if v['chars'] > 0)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated ids")
    parser.add_argument("--kind", help="comma-separated kinds")
    parser.add_argument("--discover", type=int, metavar="N",
                        help="also harvest N fresh article URLs per source in sources.yaml")
    parser.add_argument("--batch", type=int, default=10, help="URLs per provider call")
    parser.add_argument("--provider", default="local", choices=["local", "exa"],
                        help="local cascade, or Exa's /contents API as a baseline")
    parser.add_argument("--exa-livecrawl", default="always",
                        choices=["always", "fallback", "preferred", "never"],
                        help="Exa's crawl policy: 'always' is a live fetch, "
                             "'fallback' prefers its cache")
    parser.add_argument("--summarize", help="re-print the summary for a results.jsonl")
    parser.add_argument("--compare", nargs=2, metavar=("LEFT", "RIGHT"),
                        help="two run directories of the same URL set")
    args = parser.parse_args()

    if args.compare:
        compare(Path(args.compare[0]), Path(args.compare[1]))
        return

    if args.summarize:
        records = [json.loads(line) for line in
                   Path(args.summarize).read_text(encoding="utf-8").splitlines() if line.strip()]
        summarize(records)
        return

    entries = yaml.safe_load((HERE / "urls.yaml").read_text(encoding="utf-8"))
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        entries = [e for e in entries if e["id"] in wanted]
    if args.kind:
        wanted = {s.strip() for s in args.kind.split(",")}
        entries = [e for e in entries if e["kind"] in wanted]
    if args.discover:
        sources = yaml.safe_load((HERE / "sources.yaml").read_text(encoding="utf-8"))
        harvested = discover(sources, args.discover)
        print(f"discovered {len(harvested)} article URLs from {len(sources)} sources")
        entries = entries + harvested

    if not entries:
        parser.error("no URLs selected")

    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        print("warning: HTTPS_PROXY is set — sites that block or localize by IP "
              "will answer the proxy, not this machine.\n", file=sys.stderr)

    print(f"{len(entries)} URLs, batches of {args.batch}, provider {args.provider}")
    run(entries, args.batch, args.provider, args.exa_livecrawl)


if __name__ == "__main__":
    main()
