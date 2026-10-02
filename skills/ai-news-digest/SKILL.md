---
name: ai-news-digest
description: "Daily AI news digest in Traditional Chinese, from news_candidates."
version: 0.1.0
author: hermes-harness
platforms: [linux]
metadata:
  hermes:
    tags: [News, Digest, AI, Scheduled]
---

# AI news digest

Write one post of the period's AI news in Traditional Chinese. The tools decide which period, what is new, and which reports are the same story. You decide what is worth writing about, and you write it. What you post is recorded afterwards from the links in your answer — you do not record anything yourself.

## When to Use

- The daily Automation asks for the AI news digest.
- The user asks for today's AI news, or the AI news since some time ("過去三日嘅 AI 新聞" → `hours_back: 72`).

## Procedure

1. Call `news_candidates` once. No arguments for the daily digest. Use `hours_back` only when the user named a period.
2. If it returns an error: write one line saying the digest could not run and why. Do not write any news from memory. Stop.
3. If `stories` is empty: write one line saying there is no new AI news this period. Stop.
4. Choose **at most 8** stories, and at least 3 when there are 3 worth writing. Fewer is fine on a quiet day; never pad. Count them before writing.
   - Prefer higher `corroboration`: more independent sources carried it.
   - Compare `relevance` only between stories with the same `topic`.
   - Skip a story that reports the same news as an entry in `posted_recently`, such as a later write-up of a launch already posted. A story whose `models` share a name with an entry's `models` is the one to check. A new development about the same model — another release, a withdrawal, a pricing change — is new news.
   - Skip help requests, polls, "what do you think" threads, minor patch releases, and marketing without news in it.
   - One entry per `id`. If two ids are clearly the same event (a launch and its system card), write one entry and cite both links.
5. For a story whose `summary` does not say what is actually new:
   - if its `readable` is true, call `news_read` with its `id` — the text the sources already carried, no fetch; at most five `news_read` calls in the digest;
   - otherwise open **one** of its `urls` with `web_extract`; at most three `web_extract` calls in the digest.
   If neither gives you the facts, write from the summary or drop the story — never fill the gap from memory.
6. Write the post (format below) as your answer. Every fact must come from a `summary`, a `news_read` text, or a page you opened with `web_extract`. Every entry cites its story's `link` — that link is how the story is marked as posted. An entry without it will be offered again tomorrow.

## Format

Markdown, Traditional Chinese, standard written Chinese (書面語) — not written Cantonese: 「的」「是」「現在」, never 「嘅」「係」「而家」. Technical terms stay in English, untranslated, as your standing instructions say: 「context engineering」, not 「上下文工程」 or 「上下文工程 (context engineering)」. The ones digests slip on: model (not 模型), weights (權重), quantization (量化), deploy (部署), framework (框架), call (調用、呼叫), hijack (劫持), verify (驗證), parallel (並行). Model, product, company and repo names stay as written: `DeepSeek-V4.1-Flash`, `llama.cpp`.

```
## AI 新聞摘要 · <window.date_hkt>

### Model 與供應商
**<中文標題>**
<兩至三句：發生了甚麼，與之前有何不同，為何值得留意。>
來源：[<source name>](<link>)

### Agent engineering
...

### 應用：Document AI 與 text-to-SQL
...

### Open weights 與 local deploy
...
```

- One heading per bucket that has a chosen story, in that order: `models`, `agent-eng`, `applied`, `open-weights`. Use the headings exactly as written above, and put each story under the heading of its own `bucket`. Leave out a bucket with nothing chosen.
- Cite the story's `link`, exactly as given. A Google News link is fine to cite: it opens the article in a reader's browser.
- If the result has a `note` about failed sources, end with one line: 「以下來源今次未能讀取：…」.
- No introduction, no closing summary.
