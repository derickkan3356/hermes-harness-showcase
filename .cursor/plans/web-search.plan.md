status: done

# Web search and fetch — Brave primary, Baidu for mainland, local fetch cascade, failures surface

`web_search` runs on the Brave Search API (decision in `CLAUDE.md`, measurements in `docs/web-search.md`). Mainland China queries get a second tool that hits Baidu. A dead or empty engine must show up as a failure the agent reports — not an empty `success: true`, and not a silent hop onto the keyless Exa/Firecrawl ring.

Hermes ships Brave as `brave-free` (key `BRAVE_SEARCH_API_KEY`, already in `~/.hermes/.env`). Live config: `tools/facts.sh config`.

Hermes default is one `web_search` tool and no `country` parameter. A second tool is allowed (`register_tool` under a new name). Put keepers in this repo, not only under `~/.hermes`.

## Fetch (`web_extract`)

Brave is search-only. `web.extract_backend: local-cascade` runs the repo plugin `fetch-cascade`: layers escalate inside one backend, first one past a shared quality gate wins, because the agent will not retry on another engine by itself. Structure, the gate's rules and the worker's uv environment: `docs/web-fetch.md`.

Layer order:

1. HTTP GET + `trafilatura` (`pypdf` for PDFs). Built.
2. Local Chromium, reusing the Playwright build Hermes' browser tool already downloaded. Built.

Layer 3 (Camofox) is not built — the eval put it at two URLs in thirty. Reason in `docs/web-fetch.md`.

A page nothing could fetch is an error. A page fetched but never past the gate comes back as the best text with a warning on top — that is what keeps a false rejection costing time instead of content, and it is why the gate can be tuned to catch bad reads.

## Hot

- Biggest unknown: whether Brave's free credit (about 1,000 searches/month) covers real use.
- [x] `web.keyless_rescue: false`. Evidence: `tools/facts.sh config`; a dead-proxy `web_search` returns `success: false` with no "keyless rescue" text.
- [x] Provider eval (ddgs, Scrappa, Brave) with `evals/search/region_eval.py`. Results and method: `docs/web-search.md`.
- [x] `web.search_backend: brave-free`. Evidence: `tools/facts.sh config` shows `web.search_backend=brave-free`; `hermes -t web -z` with a HK weather query answered from hko.gov.hk; a bad key returns `{"success": false, "error": "Brave Search returned HTTP 422"}` with no rescue. Brave's 0-hit and bad-key behaviour: `docs/web-search.md` (Brave behaviour).
- [x] Search plugin `plugins/search_region/`, symlinked to `~/.hermes/plugins/search_region`. It overrides the `web_search` tool and leaves `web.search_backend: brave-free` upstream. Evidence: the registered schema carries `region` with enum `hk|cn|jp`; `weather forecast` returns US sites bare and hko.gov.hk with `region=hk`; `東京 週間天気` with `region=jp` returns Tokyo, not Hong Kong; a 0-hit query returns `success: false`. Structure, both override gates and `SEARCH_REGION_LOG`: `docs/web-search.md`.
- [x] The 27B uses `region` unprompted: 3/3 through `hermes -t web -z` — HK wage query `hk`, Tokyo weather `jp`, a Python question no region. Small sample; `SEARCH_REGION_LOG` collects more over real use.
- [x] Query rewrite scored with `evals/search/region_eval.py --provider brave-rewrite` (92 calls, 0 errors, HK egress). Ambiguous hits 1/8 -> 7/8 HK, 1/8 -> 6/8 CN, 0/8 -> 5/8 JP; local sets unchanged. A single region applied to every call breaks queries about elsewhere (`東京 週間天気` answers for Hong Kong), which is why the region is a per-call tool parameter. Numbers: `docs/web-search.md` (Place-name query rewrite).
- [x] Fetch plugin layer 1, `plugins/fetch_cascade/`, symlinked to `~/.hermes/plugins/fetch_cascade`, with `web.extract_backend: local-cascade`. Evidence: `tools/facts.sh config`; three cold-cache `web_extract` calls on news.rthk.hk each logged `via: http`, `status: 200`, 3,256 chars to `FETCH_CASCADE_LOG`; a 403 URL returns an error naming the layer; `arxiv.org/pdf/1706.03762` returns 39,524 chars through the `pypdf` branch. No Firecrawl: `_rescue_eligible()` is false whenever `web.keyless_rescue` is false.
- [x] `web.keyless_fallback: false`. Evidence: `tools/facts.sh config`.
- [x] Layer 1 alone passes 7/10 on a ten-URL probe. The gate rejects the hko 9-day forecast (unfilled `{0}` slots — a JS-rendered page that clears the character floor), the gov.cn front page (183 chars) and example.com (113 chars, a genuine short page). Table: `docs/web-fetch.md`.
- [x] Layer 2, local Chromium (`workers/fetch_browser.py`, `playwright` pinned to the build in `~/.cache/ms-playwright`). Evidence: the hko 9-day forecast escalates on 5 unfilled template slots and comes back from `chromium` with 4,231 chars and filled temperatures; the seven URLs that passed layer 1 still resolve at layer 1; 10-URL batch in 12.8s with one browser start. Table: `docs/web-fetch.md`.
- [x] Gate rejection with text in hand returns that text with a warning paragraph instead of an error; transport failure stays a hard error. Evidence: example.com and gov.cn come back as `chromium (degraded)` with the warning, a 403 URL returns `error` naming both layers.
- [x] `renderable()` skips escalation for PDFs and unreadable content types — a browser buys the same answer more slowly.
- [x] Fetch eval, `evals/fetch/` — 30 URLs, 48s: layer 1 18, layer 2 5, degraded 5, hard failure 2. hk-gov 5/5, hk-news 5/5, pdf 2/2 clean. Per-URL text kept under `evals/fetch/runs/<ts>/pages/` for reading against the live page. Numbers and the per-layer breakdown: `docs/web-fetch.md`.
- [x] Extraction keeps image alt text and drops the source URL, by extracting twice and keeping the longer result. Changing the markup moves trafilatura's idea of the main content, in both directions — `include_images` cost rthk 52 of its 69 headlines, span-rewriting cost thestandard 6,561 characters — so neither can be used alone. Gains: scmp +1,585, rthk +975, sina +476, hko +449, info.gov.hk +400, hkej +396, 36kr +294, no page shorter. Numbers: `docs/web-fetch.md` (Images).
- [x] `MIN_CHARS: 300` stays. It is what caught censtatd (291 chars), thestandard (204) and hkej (104) — three HK sites that layer 2 then read properly, and that would otherwise have been short plausible reads the agent could not doubt.
- [x] Baseline against Exa's `/contents` API on the same 30 URLs, same gate (`--provider exa`, `--compare`): local 23 clean / 5 degraded / 2 failed, Exa 21 / 0 / 9, at $0.027 for the set. Exa serves from its cache, so it returns the hko forecast with the `{0}` slots still in it. Table: `docs/web-fetch.md` (Against a paid fetch API).
- [x] Template rule requires a `{0}` present. Citation markers and mathematics carry `{1}`, `{2}`, `{18}` and no zero slot; without that condition the rule threw away the arxiv paper.
- [x] Readability measured against trafilatura: nothing on index pages by design, behind it on six article pages of seven. Not adopted. Table: `docs/web-fetch.md` (Readability, and what the probe set was measuring).
- [x] `SEARCH_REGION_LOG` and `FETCH_CASCADE_LOG` point at `~/.hermes/logs/` in `~/.hermes/.env`. Neither was set, so nothing had been collected from real use and the Open items that wait on that data could never have come due.
- [x] A dead engine reaches the user. Evidence: with a bad key in `~/.hermes/.env`, `hermes -t web -z` answered "the web search backend was down (Brave Search kept returning HTTP 422), so I pulled this directly from the live Wikipedia page", and `tools/facts.sh web` shows `search_recent_failed=3` and `search_last_failed=<time> — Brave Search returned HTTP 422`. The failed-search text now instructs the model on conduct, and the search audit records the outcome. Reasoning and the instruction that backfired: `docs/web-search.md` (When the search engine is down).
- [x] `tools/facts.sh web` — search and fetch health from the audit logs, read without the model in the path, over a recent window (`FACTS_WEB_WINDOW`, default 20) plus the last success and last failure times. Whole-file totals would bury a live outage under old successes. This is what makes an exhausted Brave credit visible.
- [x] Articles in the eval — 6 pinned in `urls.yaml` plus `--discover N`, which harvests fresh ones per source from `evals/fetch/sources.yaml` at run time. The summary splits landing pages from articles; `--compare` skips harvested entries. Evidence: 18 articles, 17 clean, **all 17 served by layer 1** — no article needed the browser, so Chromium, every escalation and both hard failures are landing-page costs. The one article under `MIN_CHARS` is a `news.cn` series stub that rendering did not help either. Table: `docs/web-fetch.md` (Landing pages against articles).
- [x] LM Studio unchanged: `tools/facts.sh lmstudio` shows context 65024, parallel 1, 17.74 GB. Generation over the last three requests in the server log: 78.4, 81.5, 90.5 tok/s — at the edge of the 80+ band rather than inside it, and worth a second look if it keeps drifting down (`docs/lm-studio.md` records 83-98).
- Done when: `web_search` is Brave with region support; a dead/empty engine returns an error the agent must report (usage-file + LM Studio log). `web_extract` is the local cascade plugin with `web.keyless_fallback: false`, and the fetch eval's per-layer hit rate is written down. `tools/facts.sh lmstudio` still shows context 65024 / parallel 1, generation in the 80+ tok/s band (`docs/lm-studio.md`).

## Rejected

- Search providers and Brave `search_lang`: `docs/web-search.md` (Rejected providers, Brave behaviour).
- SOUL/prompt text as the only failure alert: `docs/web-search.md` (When the search engine is down).
- Requiring the model to announce a tool failure: `docs/web-search.md` (When the search engine is down).
- Several fetch engines as separate tools for the agent to pick: `docs/web-fetch.md` (Why one backend and not several tools).
- Browser as the first fetch layer: `docs/web-fetch.md` (Why one backend and not several tools).
- Anonymous vendor fetch tiers (Firecrawl, Exa, Parallel) as the backend: `docs/web-fetch.md` (Current choice).
- RSS as a general way in for news: `docs/web-fetch.md` (Look for the endpoint before improving the extraction).
- Readability as a second extractor: `docs/web-fetch.md` (Readability, and what the probe set was measuring).
- Gating on extracted characters per source byte: `docs/web-fetch.md` (What the character floor cannot see).
- Layer 3, Camofox, for now — 2 URLs in 30, neither on the primary job: `docs/web-fetch.md` (Layer 3, and why it is not built).
- A Baidu tool, for now — mainland queries work on Brave with `region=cn`: `docs/web-search.md` (Baidu).

## Open

- Brave's LLM Context API (`/res/v1/llm/context`) returns extracted page text for a *query*, not for a URL, so it is a candidate to replace `web_search` + `web_extract` on the search path only — it cannot fetch a URL the user hands over. Verified working on the current key: `q=hong kong weather today&country=hk` returned 9 URLs and 15,845 characters, HTTP 200, no plan upgrade. Worth an eval against the two-step path: accuracy against context spent, and the `search-region` plugin would have to move to that endpoint.
