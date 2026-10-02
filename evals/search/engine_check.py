# /// script
# requires-python = ">=3.11"
# dependencies = ["ddgs==9.15.0"]
# ///
"""Which ddgs engines work from this network? One row per engine, one cell per query.

A cell is the hit count, or the error type. ddgs `auto` only fails when every engine
it tries fails, so this shows what `auto` is standing on.

  env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy uv run evals/search/engine_check.py

Unset the proxy variables when the shell has a proxy Hermes does not use.
"""

import time

from ddgs import DDGS

ENGINES = ["bing", "brave", "duckduckgo", "google", "grokipedia", "mojeek", "startpage", "wikipedia", "yahoo", "yandex"]
QUERIES = ["天文台 九天天氣預報", "北京地铁 末班车 时间", "渋谷 ラーメン おすすめ", "weather forecast", "Hong Kong typhoon signal No. 8 latest"]

for engine in ENGINES:
    cells = []
    for q in QUERIES:
        try:
            cells.append(str(len(DDGS(timeout=10).text(q, region="us-en", max_results=10, backend=engine))))
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            cells.append("empty" if "No results" in msg else (msg.split(":")[0] if ":" in msg else type(exc).__name__)[:16])
        time.sleep(0.5)
    ok = sum(c.isdigit() for c in cells)
    print(f"{engine:<11} {ok}/{len(QUERIES)}  " + " | ".join(f"{c:<16}" for c in cells), flush=True)
