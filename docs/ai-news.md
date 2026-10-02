# AI news digest

A daily post of the period's AI news in Traditional Chinese, as a new Open WebUI chat. Code decides which period, what has been posted, and which reports are one story; the model decides what is worth writing about and writes it.

| Part | Where |
| --- | --- |
| Sources | `plugins/news_ledger/sources.yaml` — one table, also what `evals/feeds` measures |
| Topics for relevance | `plugins/news_ledger/topics.yaml`; each wide-net source's floor is `min_relevance` in `sources.yaml` |
| Tools `news_candidates`, `news_read`; recording hook | `plugins/news_ledger/` (plugin `news-ledger`, toolset `news`, `post_llm_call` hook) |
| Pulling, merging, ledger | `plugins/news_ledger/workers/` — `feeds.py` names each source's URLs and parses what comes back, `keys.py` holds the match keys, `stories.py` merges and ranks, `news.py` is the worker and owns the ledger |
| Procedure and output format | `skills/ai-news-digest/SKILL.md` |
| Language of every Chinese reply | `config/SOUL.md` |
| Schedule | an Open WebUI Automation, below |
| Live state: window, last post, eval timer | `tools/facts.sh news` |
| Ledger | `~/.hermes/news_ledger.db` (SQLite, runtime, not tracked; `NEWS_LEDGER_DB` overrides) |
| Measurement | `evals/feeds/feeds_eval.py`, run six-hourly by `feeds-eval.timer`; relevance against labels: `evals/relevance/` |

Neither tool takes a URL, and neither asks for approval: nobody is present when the Automation runs. The worker runs under `uv run --script`, so feedparser and the embedding model stay out of Hermes' venv. Parsing is this plugin's — which fields a feed, an API or a newsletter carries — and fetching is not: `feeds.py` gets the bytes from the fetch cascade's HTTP layer (`docs/web-fetch.md`, Raw bytes).

The line between code and model is there because the 27B answers anyway when a tool fails (`docs/web-search.md`, When the search engine is down). Neither "has this been posted" nor "is this from this period" can be its judgement. "Is this later report on the same model news" is: no exact key tells a follow-up from a new event (Posted).

## The flow

```
pull every source → keep the window → merge items into stories → drop what was posted
  → relevance to the topics, floor on wide-net sources → rank by corroboration → the top stories to the model
model: choose a few → news_read / web_extract where the summary is thin → write, citing each story's link
hook: the finished answer → every offered story whose link is in it is posted
```

**Feeds, not search.** A feed item (RSS, Atom, or one of a few open APIs) carries a published timestamp and a stable URL, which make the window and the dedup exact operations in code. A `web_search` result has neither: upstream's Brave provider sends only `q` and `count` (`plugins/web/brave_free/provider.py`), so no `freshness`, and one story on five outlets comes back under five URLs and five headlines. Brave's free credit is also about 33 searches a day (`docs/web-search.md`), which a search per topic would use up, leaving none for chat.

**Buckets.** A routing label for where to look, not a quota (Selection). Which sources feed each: `sources.yaml`; what each is about, for relevance: `topics.yaml`.

| Bucket | Covers |
| --- | --- |
| `models` | LLM releases, benchmarks, provider company news |
| `agent-eng` | Agentic engineering, context engineering, agent design (tool use, MCP, memory, RAG), harnesses |
| `applied` | Document AI / OCR, database agents (text-to-SQL) |
| `open-weights` | Abliteration / uncensored, local deployment, quantization |

Context engineering is in `agent-eng`: it is the same field under another name, and two buckets would file one story twice.

**Window.** From the moment the last posted digest's candidates were pulled, at least 24 hours and at most 7 days. It advances only when a finished answer posted at least one offered story, or there was nothing to offer — so a run that dies midway is covered again by the next one. `hours_back` overrides it for an ad-hoc request. An item is in the window by its `published`, except from a source marked `dated_by: first_seen` — the arXiv queries — which is in by when the ledger first saw it (`first_seen` table), and only if it was submitted at most 7 days before that. arXiv's `published` is the submission, and a paper is readable only from the next announcement, half a day to four days later: windowed on submission, 82 of 186 new topic papers in a week were already outside the window when they first appeared. The 7 days keep out an old paper whose revised abstract newly matches a query. A source with nothing in `first_seen` is a backlog, and its items count as seen when published.

**Stories.** Items join a story on exact keys — the normalized link, the exact normalized title, an artifact the item points at (arXiv id, GitHub repo with its release tag, HuggingFace repo), a model name — and on a fuzzy title match between different publishers with equal version numbers. The rules and why each exists are in the docstring of `workers/stories.py`. A link that appears only in an item's body is a mention: it counts as corroboration and never merges, or one newsletter would glue forty stories together. One event under several headlines from one aggregator stays several stories: Techmeme's items are one family, which the fuzzy match never pairs, and they share no exact key. The skill has the model write two ids that are one event as one entry citing both, and both are recorded.

**Posted.** Recording is not the model's job. Hermes' `post_llm_call` hook takes the finished answer of a turn in which `news_candidates` itself ran and succeeded (`_offered_this_turn` in `tools.py` says how that is told apart), and the worker marks as posted every story from that batch whose link or artifact appears in the answer (`record_post`). A link in any other turn records nothing: there is no batch to match it against. An ad-hoc "今日有咩 AI 新聞" in an ordinary chat is recorded like the daily digest, since the reader has seen those stories. Do not give the model a tool to record with: its answer ends the turn, so it can only record before writing, and a list made before writing does not match the post — a story left out while writing is lost, one added is repeated.

**How much.** Every day posts, and the count floats: the skill has the model choose at most 8 stories, and at least 3 when there are 3 worth writing. A story offered and not written stays in the ledger unposted and can come back on a later day. The window stays wide (7 days) because the posted keys, not the window, are what stop repeats: a narrow window only loses stories.

What is posted is the items the post cited, not the whole story: a story merged on a model name can hold two events, and the one left out comes back. A cited item's links, titles and artifacts block for good, and they are checked item by item before merging (`already_posted`, `blocking` in `workers/news.py`), so a posted item cannot drag an unposted one out with it. A model name does not block: it names a subject, not an event, and "GPT-6.1 Astra scrapped" and "Introducing GPT-6.1 Sol" a day later share `model:gpt-6.1`. Whether a new report on a model already covered is news is the model's call. Code gives it the clue: each candidate and each `posted_recently` entry carries its `models`, and the entry its date.

**Relevance.** `BAAI/bge-small-en-v1.5` through `fastembed`, on the CPU, in a few seconds for a day's candidates. Not in LM Studio: the 27B at 131k leaves the 4090 almost no memory (`tools/facts.sh lmstudio` prints `gpu_memory_mib`), and a second model there competes with the agent's KV cache. The score filters per source, not per topic. A wide-net source (Techmeme, Hacker News, HuggingFace daily papers) carries much that is off the digest's subject, and its `min_relevance` drops a story that scores below it; a story also carried by a source without a floor passes. A curated source gets no floor: a release tag like `v2.130.0` or a lab post titled `Science` scores low for lack of words, and a per-topic threshold high enough to matter would drop them. Each floor sits where about 5% of that source's on-topic stories are lost, and together they drop about a quarter of the off-topic stories (`uv run evals/relevance/relevance_eval.py report`).

A score separates on-topic from off-topic well only inside one source, and only for some topics. Over the labelled week the `models` description barely beats chance (AUC 0.60) — model and lab news reads like any other tech news to it — and `local-models` is the closest topic for half of all stories. bge-base-en-v1.5 gained at most 0.05 AUC on any topic; a logistic regression on the embeddings, trained without the source it was tested on, did no better than the plain score. So the floors stay low, and whether a story matters is the model's call.

**Corroboration.** Distinct source families among a story's items and mentioners. A family is a publisher: the arXiv queries are one, the Hacker News queries are one. HuggingFace uploads count per uploader.

**Reading a story.** A candidate's `summary` is capped (`SUMMARY` in `workers/stories.py`) below the length of a typical arXiv abstract or release note, so most of one reaches the model without a fetch. `news_read(story_id)` returns what the feeds already carried beyond that, stored in the ledger when the candidates were pulled, so it fetches nothing: a short item whole, a long one — a newsletter covering forty things — cut to the paragraphs that link one of the story's links or artifacts or name its model, each with the paragraph after it, within `PER_ITEM` and `PER_STORY` in `workers/news.py`. A candidate's `readable` says whether there is anything beyond the summary. The sources fall into three kinds: newsletters carry whole issues (Latent Space, Import AI); release notes, paper abstracts, Simon Willison and r/LocalLLaMA carry the post itself; the lab blogs (OpenAI, DeepMind, Google Research, HuggingFace), Hacker News, Google News and HuggingFace uploads carry a title and little or nothing else. The lab blogs, which carry the model news that matters most, are the ones only `web_extract` can read. How much each source carries now: `uv run evals/feeds/feeds_eval.py --report`.

Both tools' results are fenced as untrusted in the form Hermes gives `web_extract` — Hermes' own list of fenced tools is fixed in upstream code, so the plugin fences them itself.

**Selection.** Best first within each bucket, buckets taken in turn, so a text-to-SQL paper carried by one source is not crowded out by a model release carried by five. A bucket is where to look, not a quota: a period with nothing in a bucket posts nothing from it. `web_search` does not fill an empty bucket — a search result carries no published date to window on and no stable URL to dedup on, and Brave's free credit is shared with chat (`docs/web-search.md`). Labs without a feed are covered by `gnews-*` and `hf-model-releases` (Sources).

## The Automation

Open WebUI keeps Automations in its own database, so this is the whole definition; recreate it from here in the UI (Automations → New):

| Field | Value |
| --- | --- |
| Name | `AI news digest` |
| Model | `hermes_session` (Hermes Agent) |
| Prompt | 用 ai-news-digest skill 做今日嘅 AI 新聞摘要。 |
| Schedule | `RRULE:FREQ=DAILY;BYHOUR=7;BYMINUTE=0` — 07:00 in the account's timezone (`docs/open-webui.md`), before arXiv's 08:00 announcement (Sources, arXiv's busy hours) |

The prompt names the skill because nothing else loads it: Open WebUI has no counterpart to the CLI's `-s`, and the model finds and loads it with `skill_view` when told its name. `tools/facts.sh webui` prints each Automation's schedule, last and next run.

`feeds-eval.timer` runs at 00, 06, 12 and 18, so it never pulls Reddit in the same minute as the 07:00 digest (Sources, r/LocalLLaMA). It is the sources' health check — `tools/facts.sh news` reports its latest run's failures — and its runs are the pool `evals/relevance` recalibrates the floors on.

## Language

The post is standard written Chinese (書面語), which the skill sets. Technical terms stay in English, untranslated and unbracketed, which `config/SOUL.md` sets for every Chinese reply on every path — it is how the reader reads, not a digest rule. The skill's section headings are English on purpose: Chinese headings taught the model the Chinese terms. The skill also names the terms the model slips on most, and a few still slip (調用 and 量化 most often). The model copies the wording of the question — English terms in a Cantonese question come back in English, Chinese ones in Chinese — so a follow-up's language says as much about the question as about the rule.

## Sources

None needs an API key. A source that fails is reported in the result's `errors` and the digest names it; it is never an empty day.

- **arXiv by topic, through the API.** The category feeds announce about 410 papers a day across cs.CL and cs.IR, all with one announcement timestamp; they are in the table as `control: true`, read only by the eval. The topic queries are keyword search over the abstract. Over the labelled week they returned 13 of the 38 new document-AI and text-to-SQL papers in cs.CL and cs.IR, at 0.76 precision. Reading the whole category through a relevance floor does worse: at the same recall it keeps three times as many papers, at 0.29 precision (`evals/relevance`, control section). The misses are papers that never say the query's words — `document parsers`, `scholarly PDFs`, `visual document retrieval` — so the recall is in the query terms, and each new term can be checked against the labelled papers. A category feed also announces revisions of old papers (`Announce Type: replace`, about a third of it), which are not news; the API queries, sorted by submission date, return new papers only.
- **arXiv's API needs curl's TLS handshake.** It sits behind Fastly, which answers `406 Not Acceptable` to Python's `ssl` handshake and `200` to curl's — same headers, query and IP, and not on every call. Every source is fetched through the fetch cascade's HTTP layer, which is libcurl (`docs/web-fetch.md`, Raw bytes). `rss.arxiv.org` does not have that rule. The API also asks for one call every three seconds, which `feeds.py` paces.
- **arXiv's busy hours.** New papers are announced at 20:00 US Eastern, Sunday to Thursday: 08:00 HKT Monday to Friday. For hours after that the API answers 429 or stalls past the 30 s timeout, to three paced calls: the 12:00 eval run failed on six of ten days, every one a weekday, and the 09:00 digest from 2026-09-29 to 10-02, while the 00:00, 06:00 and 18:00 runs never did. `rss.arxiv.org` is not affected. A digest before 08:00 reads the previous announcement; a failed pull costs nothing, since each query returns its 50 newest and an unseen paper is in by when it is first seen.
- **Labs without a feed.** Anthropic, Meta AI, Mistral and Qwen publish no working feed. Open-weight labs are read from their HuggingFace uploads (`hf-model-releases`). Closed labs are read through Google News restricted to their site (`gnews-*`): real headline and date. The link is a Google-encoded redirect that a browser follows to the article and plain HTTP does not — `web_extract` reads it, through the cascade's browser layer — and since it is not the article's URL, those items have no URL key for dedup.
- **r/LocalLLaMA** answers `429` to a second call within about a minute.

Why each source is set as it is — a dead feed kept, a site restricted to a path, a release feed's label — is in a comment beside it in `sources.yaml`.

## Running it by hand

The worker speaks JSON on stdin. From `plugins/news_ledger/workers`, with a throwaway ledger:

```fish
set -x NEWS_LEDGER_DB /tmp/test_ledger.db
echo '{"op":"candidates","limit":10}' | uv run --script --no-project --locked -q news.py
echo '{"op":"read","story_id":"<id>"}' | uv run --script --no-project --locked -q news.py
```

A finished post is recorded with `{"op":"record_post","post":"…"}`. A whole digest through the local model, with the Open WebUI path's toolsets — the CLI runs the recording hook too:

```fish
hermes chat --oneshot -t web,skills,news,memory,todo,session_search -s ai-news-digest -q "Run the AI news digest."
```

Without `NEWS_LEDGER_DB` both write the real ledger, and what they record will not be offered again.

After changing the worker's dependencies: `uv lock --script news.py` in that directory. The gateway loads plugins at start, so a change to `plugins/news_ledger/*.py` needs `systemctl --user restart hermes-gateway`; a change under `workers/` does not, since each call starts the worker fresh.
