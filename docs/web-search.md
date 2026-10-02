# Web search providers

Which search backend Hermes uses, why, and how to measure it again. Read this when changing `web.search_backend`, adding a search plugin, or when search quality or quota looks wrong. Measurements taken 2026-09-17 from the home line (HK, HKBN).

## Current choice

| Role | Provider | Key in `~/.hermes/.env` | Notes |
| --- | --- | --- | --- |
| Primary | Brave Search API (bundled `brave-free`), wrapped by the repo plugin `search-region` | `BRAVE_SEARCH_API_KEY` | $5 free credit per month (about 1,000 searches), then $5 / 1k. Needs a card on file; prepay can be $0. Own index, not a Google scraper. |
| Mainland, not built | Baidu Qianfan 百度搜索 API | — | Not needed yet; see [Baidu](#baidu). |

### The `search-region` plugin

Canonical copy `plugins/search_region/` in this repo; `~/.hermes/plugins/search_region` is a symlink to it. `hermes plugins list` shows it under its manifest name `search-region`, which is also its config key.

It replaces the built-in `web_search` tool and leaves the search backend alone. `web.search_backend` stays on upstream's `brave-free`, so Hermes' own Brave client, search cache and backend-error reporting are the ones running.

```
agent
  │  web_search(query="天氣", limit=5, region="hk")
  ▼
handle_web_search()                      plugins/search_region/tool.py
  │  1. region -> query becomes "香港 天氣"
  ▼
web_search_tool()                        tools/web_tools.py, upstream
  │     picks the provider from web.search_backend
  │     search cache + single-flight lock, backend-selection errors
  ▼
brave-free provider.search()             upstream — the HTTP call to Brave
  │
  ▼  {"success": true, "data": {"web": [...]}}
back in handle_web_search()
  │  2. empty web list -> {"success": false, "error": ...}
  ▼
agent
```

The plugin owns two steps and nothing else. `web_search_tool` is imported as a Python function rather than called through the tool registry, so replacing the registry's `web_search` entry does not make the handler call itself.

**`region`** is a per-call parameter (`hk`, `cn`, `jp`, optional) that prepends the place name in the region's own script. It belongs to the call, not to config: one pinned region answers the wrong place for questions about somewhere else. Numbers: [Place-name query rewrite](#place-name-query-rewrite). Brave's `country` parameter is not sent — the eval showed it adds close to nothing on its own, and reaching it would mean forking upstream's provider.

**0 hits is `success: false`.** Backends answer `success: true` with an empty list when they find nothing, which reads as a finished search. The check sits in the tool, above the backend, so it holds for whatever `web.search_backend` is set to — `ddgs` has the same behaviour.

Replacing a built-in tool needs two separate things, and both are required:

| | Where | Who states it | What it says |
| --- | --- | --- | --- |
| `override=True` | `ctx.register_tool()` argument | the plugin | this registration replaces the tool of that name |
| `allow_tool_override: true` | `plugins.entries.search-region` in config.yaml | the operator | this plugin is allowed to do that |

Without the config grant the registration raises `PluginToolOverrideError`. The gate is separate because an override can replace a privileged built-in such as `shell_exec` and intercept everything routed through it. `hermes plugins enable search-region --allow-tool-override` sets it.

Set `SEARCH_REGION_LOG=<path>` to append one JSON line per call (`query`, `region`, `sent`). That is how to check whether the model picks regions correctly over real use; it writes nothing when unset.

## Measured comparison

Query set: `evals/search/queries.yaml` (10 HK, 10 mainland, 10 JP local queries; 8 ambiguous queries with no place in them, run once per region). Top 5 URLs scored. "In-region share" = fraction of the top 5 whose host is in the region's domain list; failed calls are left out of the share and count as a miss for hits.

| | ddgs `auto` | Scrappa | Brave `country` | Brave `country` + `search_lang` |
| --- | --- | --- | --- | --- |
| Errors | 58/130 | 2/92 | 0/92 | 0/92 |
| Median seconds per call | 3.8 | 6.8 | 1.3 | 1.2 |
| Local HK share (hits) | 0.63 (6/10) | 0.55 (8/10) | 0.62 (10/10) | 0.64 (10/10) |
| Local CN share (hits) | 0.86 (7/10) | 0.68 (9/10) | 0.66 (9/10) | 0.66 (9/10) |
| Local JP share (hits) | 0.60 (3/10) | 0.68 (9/10) | 0.68 (9/10) | 0.70 (9/10) |
| Ambiguous HK hits | 1/8 | 5/8 | 1/8 | 1/8 |
| Ambiguous CN hits | 0/8 | 4/8 | 3/8 | 3/8 |
| Ambiguous JP hits | 0/8 | 5/8 | 5/8 | 5/8 |

The share only measures locality, not relevance. Reading the HK local top 5 by hand:

| Query | Brave | Scrappa |
| --- | --- | --- |
| 天文台 九天天氣預報 | hko.gov.hk ×2, weather.org.hk | i-cable.com, youtube.com, hk01.com |
| 強積金 可扣稅自願性供款 扣稅上限 | mpfa.org.hk, moneyhero.com.hk | zaobao.com, youtube.com, afcd.gov.hk |
| 旺角 車仔麵 邊間好食 | stheadline.com, openrice.com | bd.gov.hk, netflix.com |
| 點樣 預約 換領 智能身份證 | immd.gov.hk, smartid.gov.hk | HTTP 503 |

Scrappa reports `engine_used: google` on every call, but its results do not look like Google's for these queries.

### Brave behaviour

- `country` does little for English queries with no place in them: `weather forecast` with `country=HK` returns the same US sites (weather.com, weather.gov, noaa.gov) as with no country.
- `search_lang` adds almost nothing on top of `country`: 40 of 54 region calls returned the same URLs.
- Putting the place in the query works: `Hong Kong typhoon signal No. 8 latest` returns hko.gov.hk first. A region-aware plugin should add the place name to the query, not rely on `country` alone.
- Valid codes used: `country` `HK`, `CN`, `JP`; `search_lang` `zh-hant`, `zh-hans`, `jp` (Brave uses `jp`, not `ja`).
- 0 hits returns `success: true` with an empty `web` list, no error — the same silent failure `ddgs` has. The `search-region` tool turns that into an error.
- Only an exact-phrase query (quoted) actually goes empty. Unquoted nonsense is fuzzy-matched into unrelated results, so an empty list is the rare case and irrelevant results are the common one.
- A bad `BRAVE_SEARCH_API_KEY` is HTTP 422, which the provider reports as `{"success": false, "error": "Brave Search returned HTTP 422"}`. With `web.keyless_rescue: false` nothing hops onto the free-tier ring.

### Place-name query rewrite

Prepending the region's place name to the query (`香港 weather forecast`) is measured as the `brave-rewrite` provider in `evals/search/region_eval.py`. 92 calls, 0 errors, HK egress, each query's plain form as its own baseline:

| | Ambiguous HK | Ambiguous CN | Ambiguous JP | Local HK | Local CN | Local JP |
| --- | --- | --- | --- | --- | --- | --- |
| Plain query, hits | 1/8 | 1/8 | 0/8 | 10/10 | 9/10 | 10/10 |
| Place prepended, hits | 7/8 | 6/8 | 5/8 | 10/10 | 9/10 | 9/10 |
| Plain query, in-region share | 0.10 | 0.05 | 0.00 | 0.64 | 0.60 | 0.72 |
| Place prepended, share | 0.43 | 0.28 | 0.38 | 0.68 | 0.66 | 0.74 |

That is the win `country` never delivered, but it only holds when the place matches what the query is about — the eval always prepends the query's own region.

Forcing one fixed region on every query is a different thing, and it breaks queries about somewhere else. Measured with `country=HK` plus `香港` on non-HK queries:

| Query | Plain | Forced `香港` + `country=HK` |
| --- | --- | --- |
| 東京 週間天気 | weathernews.jp, tenki.jp, jma.go.jp — Tokyo | tenki.jp, weathernews.jp — **Hong Kong** pages on Japanese sites |
| JR Pass price and where to buy | japanrailpass.net (official) | hk.trip.com, hk01.com, kkday.com — HK travel blogs, official site gone |
| 北京地铁 末班车 时间 | bjsubway.com, mtr.bj.cn | unchanged |
| 新幹線 東京 新大阪 料金 のぞみ | ekitan.com, jr-shinkansen.net | unchanged |

A place name written strongly in the query (北京, 新大阪) survives the rewrite. A query whose sites are location-parameterized (週間天気) silently answers for the wrong place, and the domain-based in-region score cannot see it — the hosts are still `.jp`. So the region has to be chosen per query, not pinned in config.

## Rejected providers

| Provider | Why |
| --- | --- |
| `ddgs` (keyless, Hermes default) | From the home line most engines fail: per engine over 5 queries, duckduckgo 0/5 (TCP connect times out), google 1/5, bing 3/5, startpage 0/5. Through a US exit, bing/duckduckgo/google are 5/5. `region=hk-tzh` also breaks its wikipedia engine (`tzh.wikipedia.org` does not exist). |
| Scrappa | Results are off topic, not merely worse: a weather query returned youtube.com, a where-to-eat query returned netflix.com (see the hand-read table above). Also 6.8 s median per call and about 2% HTTP 503. Not a fallback either. |
| Serper | Free credits are one-time (2,500), no monthly tier. |
| SerpApi | $25 / 1k after 250 free per month. Defendant in Google v. SerpApi (N.D. Cal. 4:25-cv-10826) over scraping Google. Same Google-scraper category as Scrappa. |
| Exa as primary | Semantic index; not tested for local, time-sensitive queries. |
| Self-hosted SearXNG | Scrapes the same engines as `ddgs`, so it hits the same blocks from this line. |
| Public SearXNG instances | Run by unknown operators; every query goes to them. Same engine blocks as self-hosted. |
| Nous Portal search | Paid subscription. |
| Bing Web Search API | Retired; closed to new users. |
| Google Custom Search JSON API | Closed to new users. |

## Baidu

Baidu has an official search API: Qianfan 百度搜索, `POST https://qianfan.baidubce.com/v2/ai_search/web_search`, `Authorization: Bearer <AppBuilder API key>`, body `{"messages": [{"role": "user", "content": "<query>"}], "search_source": "baidu_search_v2", "resource_type_filter": [{"type": "web", "top_k": 10}]}`. Optional `search_filter.match.site` and `search_recency_filter` (`week`, `month`, `semiyear`, `year`). 1,500 free calls per month (about 50 per day), then ¥0.036 per call, 3 QPS. Paid use needs Baidu Cloud real-name verification; whether a Hong Kong user can pass it is untested.

It is not wired up, and the reason is that mainland queries already work without it. `region=cn` takes Brave's ambiguous hits from 1/8 to 6/8 ([Place-name query rewrite](#place-name-query-rewrite)), and the fetch eval reads 5 of 7 mainland pages cleanly (`docs/web-fetch.md`). Build it against a use case that shows Brave is not enough — mainland forums are the likely one — and settle the real-name question first, because everything else depends on it.

## Keyless fallback ring

`web.keyless_rescue: false` in `config/config.yaml`: a failed `web_search` returns the error instead of retrying on the Exa/Parallel/Firecrawl free tiers. `web.keyless_fallback` stays on until a local `web_extract` backend exists, because `web_extract` with no `extract_backend` runs on Firecrawl's anonymous tier. That tier returns `403 Forbidden` on about 1 in 3 calls (hko.gov.hk, 6 runs); rescue does not apply to it.

## Testing search from this machine

Search engines localize by exit IP. Claude Code's shell sets `HTTPS_PROXY=http://127.0.0.1:8888`, which exits in Seattle. Hermes run from a normal terminal exits in Hong Kong. For any test of a keyless engine, unset `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy`. API providers with an explicit region parameter are not affected. `region_eval.py` prints the exit location at the start of each run.

Tools, all run with `uv run` from the repo root:

| Command | What it does |
| --- | --- |
| `evals/search/engine_check.py` | Each `ddgs` engine × 5 queries; shows which engines work from this network. |
| `evals/search/region_eval.py --provider <p> --no-noise` | Full query set, with and without region, for `ddgs`, `scrappa`, `serper`, `brave`, `brave-lang`. 92 calls with `--no-noise`. |
| `evals/search/region_eval.py --summarize <run>.jsonl` | Per-query table for one run. |
| `evals/search/region_eval.py --compare <run>.jsonl ...` | Side-by-side group table; no API calls. |

Raw runs are written to `evals/search/runs/` (not tracked).

## When the search engine is down

A dead backend must reach the user, and the model cannot be relied on to carry it. With a bad Brave key the tool answered `{"success": false, "error": "Brave Search returned HTTP 422"}`, and the 27B called the tool, read that, and then answered the question from memory with a source URL it had never opened and no mention that anything had failed.

The fix that worked is in the error text, and it is about conduct rather than wording. Every failed or empty search now carries:

> No results were retrieved, so nothing here has been verified against the web. Tell the user that the search failed and what you were looking for. Do not answer from memory as though the search had succeeded, and do not cite a source you have not opened.

With that text the model stops inventing citations and goes and fetches the pages instead — four dead-backend runs, four sets of real fetches in `FETCH_CASCADE_LOG`. It mentions the failure some of the time, not all of it.

A stronger version was tried, leading with "your reply MUST say, in its first sentence, that the web search failed". It made things worse: the model stopped fetching altogether and went back to answering from memory with unopened citations. Asked to do something this model acts; asked to relay something about its own tools it does not, and a demand about wording crowds out the one about evidence. Do not reach for a firmer instruction here — reach for a check that does not go through the model.

That check is `tools/facts.sh web`, which reads the outcome recorded on every call and reports `search_recent_ok`, `search_recent_empty` and `search_recent_failed`, with `search_last_ok` and `search_last_failed` beside them.

How to read it: the counts run over a recent window rather than the whole file, because three failures among ten thousand calls is history and three among the last twenty is an outage. `FACTS_WEB_WINDOW` changes the window; 20 is the default. The last-success time is the other half — a run of failures means much more when nothing has worked since yesterday, and a credit that has run out looks exactly like that.

## What to watch

Whether the model keeps choosing regions well is the open question about `region`, and it is answered from traffic rather than from a probe. `tools/facts.sh web` reports `search_recent_region` over the last calls; the log itself has the query beside the region chosen, which is what says whether the choice was right.

A Hong Kong default for calls that pass no region is the obvious next move and is not made, because the eval showed a pinned region answering for the wrong place on questions about somewhere else. Read the log before adding one: the numbers to beat are in [Place-name query rewrite](#place-name-query-rewrite).

## Audit trail

`SEARCH_REGION_LOG` makes the `search-region` plugin append one JSON line per call: the query as the model wrote it, the region it chose, the query actually sent, and the outcome (`ok`, `empty`, `failed`, `unreadable`) with the backend's error. Whether the model sets a region, and whether it sets the right one, is only answerable from real calls.

It is set in `~/.hermes/.env`, which is not tracked, so a fresh install collects nothing until it is added back:

```
SEARCH_REGION_LOG=$HOME/.hermes/logs/search-region.jsonl
```
