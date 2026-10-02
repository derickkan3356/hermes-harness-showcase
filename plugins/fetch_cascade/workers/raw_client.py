"""The caller's side of the HTTP layer's raw format, for code that parses its own bytes.

The news digest's feeds, the job digest's sources and the OfferToday watcher all
read a feed or a JSON API rather than a page, so they skip `web_extract` and run
`fetch_http.py` directly with `format: "raw"` (`docs/web-fetch.md`, Raw bytes).
This is that call, once. Stdlib only, so any caller's environment can import it:

    sys.path.insert(0, str(<repo>/"plugins"/"fetch_cascade"/"workers"))
    from raw_client import fetch_raw
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Union

FETCH_HTTP = Path(__file__).resolve().parent / "fetch_http.py"

Spec = Union[str, Dict[str, Any]]


def unset_proxy() -> None:
    """A source must see this machine's egress, which is what Hermes sees."""
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(var, None)


def fetch_raw(specs: List[Spec], concurrency: int = 1, timeout: float = 30.0) -> List[Dict[str, Any]]:
    """One row per spec, in order: `url`, `status`, `headers`, and `body` (bytes) or `error`.

    A spec is a URL or `{"url", "method", "headers", "body"}`. A worker that
    cannot run at all fails every spec with its reason, so a broken transport
    shows up as source errors rather than as a quiet empty result.
    """
    if not specs:
        return []
    uv = os.environ.get("UV") or shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    request = {"urls": specs, "timeout": timeout, "format": "raw", "concurrency": concurrency}
    # The worker's timeout is per gap between bytes, so this is room for every
    # round of the batch plus uv preparing the environment, not a tight bound.
    budget = 60 + timeout * 2 * -(-len(specs) // concurrency)
    url_of = lambda s: s if isinstance(s, str) else str(s.get("url") or "")
    try:
        proc = subprocess.run(
            [uv, "run", "--script", "--no-project", "--locked", "-q", str(FETCH_HTTP)],
            input=json.dumps(request), capture_output=True, text=True,
            timeout=budget, cwd=str(FETCH_HTTP.parent),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"fetch worker failed: {(proc.stderr or '').strip()[-400:] or proc.returncode}")
        rows = json.loads(proc.stdout)["results"]
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return [{"url": url_of(s), "status": 0, "headers": {}, "error": f"{type(exc).__name__}: {exc}"} for s in specs]
    out = []
    for r in rows:
        row = {"url": r["url"], "status": r.get("status", 0), "headers": r.get("headers") or {}}
        if r.get("error"):
            row["error"] = r["error"]
        else:
            row["body"] = base64.b64decode(r["body"])
        out.append(row)
    return out
