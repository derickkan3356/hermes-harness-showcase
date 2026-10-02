# Hong Kong job digest

A daily post of new Hong Kong job ads that fit the user, in Traditional Chinese, as a new Open WebUI chat. Code pulls the boards, drops every ad that is certainly wrong and every job already shown; the model reads what is left against the user's profile and writes about the ones that fit.

| Part | Where |
| --- | --- |
| The user's profile, queries and rule settings | `plugins/job_ledger/profile.yaml` |
| Pulling and reading JobsDB and CTgoodjobs, the JD | `plugins/job_ledger/workers/sources.py` |
| One job from many ads, hard denies, marks, one role per product | `plugins/job_ledger/workers/rules.py` |
| The worker and the ledger | `plugins/job_ledger/workers/jobs.py` |
| Tools `job_candidates`, `job_read`, `job_feedback`; recording hook | `plugins/job_ledger/` (plugin `job-ledger`, toolset `jobs`) |
| How the model judges, and the post's format | `skills/hk-job-digest/SKILL.md` |
| Schedule | an Open WebUI Automation, below |
| Ledger: pulled ads, read JDs, what was offered, posted, feedback | `~/.hermes/job_ledger.db` (SQLite, runtime, not tracked; `JOB_LEDGER_DB` overrides) |
| Live state: last digest, last pull's source errors, posts, what the ledger holds, feedback, memory check | `tools/facts.sh jobs` |
| Measurement | `evals/jobs/jobs_eval.py` (sources and rules), `evals/jobs/model_eval.py` (the model), labels in `evals/jobs/labels/` |

## The flow

```
JobsDB (three nets) + CTgoodjobs, last 3 days newest first → into the ledger (at most once every 6 hours)
ledger's ads of the last 7 days → one job per company and title across boards
  → hard denies on the ad's fields → the JD of what is left (ledger, else fetched and kept) → hard denies on the JD
  → fold a role posted per product into one entry → drop jobs already seen
  → first pulled first, then newest posted → entries J1…Jn
model: judge every entry against the profile on its JD → write the kept ones, citing each url
hook: the finished answer → every entry of the run is seen; posted, each whose `### J3` heading or url is in it
```

A kept entry records from its heading as well as its url, because the model copies a url by hand and can get it wrong. CTgoodjobs urls go to the model without the slug (`/job/<id>`, the same page) for the same reason: a Chinese title's slug is a long run of percent-escapes.

**Period.** Every run pulls the last 3 days (`PULL_DAYS`) from both boards into the ledger, and takes its entries from the ledger's ads posted in the last 7 days (`WINDOW`). The overlap between daily pulls is what makes a failed pull cost nothing: the next day's covers it. A first run has only its own 3-day pull. A run less than 6 hours after the last pull (`PULL_EVERY`) reads the ledger alone and carries that pull's errors, so the user asking in chat for the next entries after the 09:30 digest sends no search page to CTgoodjobs; the post says when the boards were last read. A pull that failed still counts: another one soon after only adds to the requests CTgoodjobs' WAF counts toward its CAPTCHA.

**Ledger.** Each board's ad is kept by `source:source_id` with the time it was first pulled, and its JD once read, for 30 days (`CACHE_KEEP`). A JD is fetched once per ad, and only for an unseen job the field rules keep; the rules run again over every held ad each run, so a changed `profile.yaml` applies at once without a fetch. A job page that came back with no JD in it is kept as such and not fetched again — some CTgoodjobs ads on a company's own template (`templateId` 4000) load their JD in the browser after the page, and are marked `JD unread`. A failed request is tried again next run. An ad merges with its twin on the other board whichever day each was pulled.

**Order and limit.** Entries go first pulled first, then newest posted, so what did not fit under `limit` (20) comes before newer ads next run; `more` says how many wait, and the post says it. If more jobs pass the rules each day than `limit`, the wait grows every day whatever the order.

**Not done.** No background fetch job: with the search's own date filter a day's CTgoodjobs is about 7 search pages and 10–20 JDs, which fits in the 09:30 run (about two minutes), and the 3-day pull already covers a failed day. No skipping an ad because its JD was once fetched, in place of seen: that drops for good an entry that did not fit under `limit`, and hides a denied job from a changed rule; the held JD saves the same requests.

**Seen.** A run's entries are seen once a finished answer comes back from the turn that asked for them, kept or not: the model read them, and showing them again is noise. A seen job is not offered again for 30 days (`SEEN_BLOCK`); a repost after that is worth another look. A run that dies before answering leaves its entries unseen, and the next run offers them.

## What code drops

The line: drop an ad the user would reject for certain, keep one that might fit. So a deny reads what an ad states, and an ad that does not state something is kept and marked.

| Deny | Reads | Why it can be trusted |
| --- | --- | --- |
| on-site | JobsDB `workArrangements` On-site | SEEK makes the field mandatory for hirers and infers it only for integrations without it; link-out ads were 90% On-site and hirer-picked ones 87%; the JD agreed in 29 of 30 sampled. A CTgoodjobs ad that is the same job as a JobsDB On-site ad is on-site too. Across all of JobsDB Hong Kong, 4% of ads are Hybrid or Remote. |
| below floor | JobsDB's own salary filter; a CTgoodjobs monthly figure | JobsDB requires a range on every ad, shown or not (`salaryrange=0-` dropped none of 281); the filter keeps a range whose top reaches the floor. |
| seniority | a title prefix in `deny_titles` | The user wants an individual-contributor role. Manager is denied even where Hong Kong uses it as a grade for IC work — the user's call. Lead, Principal and Officer can be IC or not, so the model decides from the JD (`not_wanted`: leading a team as the main job). |
| no AI in ad | title, teaser, highlights and JD together name no `ai_terms` | The title alone is not enough: read with the JD, an SRE and a data science analyst ad were the AI agent and RAG work wanted. |
| experience | the lowest minimum the JD requires, `experience_deny_from` (6) years or more | 4–5 years is kept and marked. A range counts by its lower end; a clause with "preferred" or "a plus" is not a requirement. |

The JD rules run only on jobs the field rules keep, so a denied job costs no fetch. A job whose JD could not be read is kept and marked.

**Not a deny: an advertiser, or pay per hour.** RemoteJobsOne (JobsDB) posts over a hundred ads a week with no employer named, every one tagged Full time. Of 50 with a JD, 34 stated pay per hour — AI-training gigs — and 16 pay per year, among them a Forward Deployed Engineer the digest posted. Denying the advertiser drops the salaried ones. Denying a JD that states hourly pay hit none of 60 other advertisers' JDs, but an ad offering a salaried and an hourly mode would go whole. The model drops the gigs (`not_wanted`: producing AI training data); what they cost is `limit` slots, and the user can ask for the next entries (Period).

Marks: agency (a list of recruiters, or "our client" in the ad), outsourcing (a list of firms that place their own staff), contract, hybrid or remote, arrangement unstated, salary unstated, requires N years.

## What the model judges

The skill's **Judging** section, against `description` and `not_wanted` in the profile, which `job_candidates` returns with the entries. Three things decide how well it works:

- **The JD, not the title.** The user's own judgements from title and teaser disagreed with their judgements after reading the JD on 7 of 20 ads — a "Product Engineer" that was product management, an "AI Solution Specialist" that was low-code. Each entry carries its JD from its first heading about the work (`sources.jd_excerpt`), because many open with a page about the company: AXA's first 2,400 characters were, and a 1,500-character excerpt from the top made the model drop a job the user wanted.
- **The profile names work, never titles.** A title written in the profile becomes a reason in itself. `not_wanted` describes the day-to-day work that is out, and the model applies an item only when it is the main work of the role.
- **One source of truth.** The model judges against the profile alone. `job_feedback` keeps the user's verdicts in the ledger and its result tells the model not to save them to memory: a verdict in memory reaches every later prompt and steers the digest past the profile. The profile changes when the user reviews the feedback.

There is no embedding step. The model reads every entry of a run (at most `limit`), and on the first labels no embedding — `bge-small-en-v1.5`, `bge-base-en-v1.5`, `jina-embeddings-v2-small-en`, `paraphrase-multilingual-MiniLM-L12-v2`, on title, teaser or JD — separated what the user wanted better than a plain title rule; embedding the JD did worst, its boilerplate outweighing the role.

Measured on 20 ads the user judged after reading the JD, with the profile written from their reasons, `qwen/qwen3.8-27b` at `reasoning_effort: medium` kept the 3 wanted and dropped the 17 not wanted in 5 of 6 runs of the final profile; the other run dropped a wanted SRE whose JD is part R&D. Since the profile was written from those 20, that checks its wording, not the model. `model_eval.py` asks for the effort `config/config.yaml` sets, so a rerun measures what the digest runs.

## Sources

**JobsDB.** SEEK's v5 search JSON, `hk.jobsdb.com/api/jobsearch/v5/search`, keyless, 100 a page, newest first (`sortmode=ListedDate`), `dateRange` in days (1, 3, 7, 14 or 31). Three nets: the profile's keywords with the salary floor, the same without it (the difference is the below-floor set), and Hybrid or Remote across every classification with the floor, for AI roles the keywords miss. Every ad states an arrangement; the On-site ones are pulled so that a CTgoodjobs ad for the same job can be denied. The JD is the longest `"content"` string in the job page's embedded JSON.

**CTgoodjobs.** The board that matters most: of 26 jobs the user marked wanted in a week, 24 were on CTgoodjobs only. No API. The search page `jobs.ctgoodjobs.hk/jobs/<slug>-jobs` embeds full records in its Next.js flight data (`self.__next_f`), 30 a page. Its own filters, taken from the page's search state and checked against what came back: `post_date` 1 is the past 24 hours, 2 the past 3 days, 3 the past 7 days, -1 any time; a cookie `sort` 1 is relevance (the default), 2 newest first. The page echoes both in its search state (`"PostDate"`, `"Sort"`), and code checks the echo, since a period with no new ad is an empty page and proves nothing. With `post_date=2` and newest first, one page a slug covers the pull (`人工智能`, the widest slug, had 26 ads in 3 days); the next page is asked for only while one comes back full. `publishTime.date` is a Hong Kong day. The JD is the job page's `JobPosting` JSON-LD. It sits behind AWS WAF on CloudFront: after a few hundred requests in three hours of testing it answered every page with HTTP 405 and a CAPTCHA (`x-amzn-waf-action: captcha`), while the user's own browser on the same line, which carries a WAF token, loaded it normally. Requests go one at a time, five seconds apart (`CT_GAP`), search and job pages alike, and after the first 405 nothing more goes to CTgoodjobs in that run. Getting past the CAPTCHA — a headless browser, a borrowed token, another IP — is not done: the wall is the site saying no to this client.

**Not used.** Glassdoor and Indeed answer with a captcha. LinkedIn's guest endpoint works, but its terms forbid automated access and without a login it carries no work arrangement. OfferToday (BOSS Zhipin's Hong Kong board, JSON behind a client-rendered page) refuses at every pace: `evals/offertoday/offertoday_watch.py` ran 90 times over 2026-09-25 to 10-02, 15 runs at each spacing of 2, 5, 10, 20, 40 and 60 s, the mix of search and job-detail calls a digest sends, and every run got HTTP 429 within 2 to 8 calls (median 3 to 5, no `Retry-After`), then a block of 1 to 10 minutes; the blocks did not grow over the week. A day's search pages and JDs is tens of calls, so OfferToday is not a source. Getting past the limit — rotating IPs, a browser — is not done, for the reason CTgoodjobs' CAPTCHA is not. `web_search` is not a job source: a result has no listing date and no stable id, and Brave's credit is shared with chat. Of the job APIs, Careerjet wants the real end user's IP and user agent on every call, Jooble an application with no published terms, and Adzuna has no Hong Kong. JSearch (Google for Jobs, `country=hk`, 200 free calls a month) is the paid-tier option if a board is lost.

## Feedback

In a digest's chat the user answers with an entry id ("J3 唔啱，佢其實係做 IT support"); the model calls `job_feedback`. To read it:

```fish
sqlite3 ~/.hermes/job_ledger.db "select at, entry_id, verdict, title, reason from feedback order by at"
```

Verdicts on posted entries are the held-out labels: judged after reading the post, on jobs the profile was not written from.

## The Automation

Open WebUI keeps Automations in its own database, so this is the whole definition; recreate it from here in the UI (Automations → New):

| Field | Value |
| --- | --- |
| Name | `HK job digest` |
| Model | `hermes_session` (Hermes Agent) |
| Prompt | 用 hk-job-digest skill 做今日嘅香港 AI 職位 digest。 |
| Schedule | `RRULE:FREQ=DAILY;BYHOUR=9;BYMINUTE=30` — 09:30, apart from the AI news digest at 07:00, since both use the one LM Studio slot |

## Running it by hand

The worker speaks JSON on stdin. From `plugins/job_ledger/workers`, with a throwaway ledger:

```fish
set -x JOB_LEDGER_DB /tmp/test_jobs.db
echo '{"op":"candidates"}' | uv run --script --no-project --locked -q jobs.py
echo '{"op":"read","entry_id":"J3"}' | uv run --script --no-project --locked -q jobs.py
```

A whole digest through the local model, with the Open WebUI path's toolsets — the CLI runs the recording hook too:

```fish
hermes chat --oneshot -t web,skills,jobs,memory,todo,session_search -s hk-job-digest -q "Run the Hong Kong job digest."
```

Without `JOB_LEDGER_DB` both write the real ledger, and what they show will not be offered again. A run on a fresh ledger costs CTgoodjobs its search pages and a page per surviving ad — 7 and 15 on 2026-09-28 — and one on a ledger that holds the JDs costs about the search pages alone: do not loop either. `counts.requests` in the output says what a run sent.

The gateway loads plugins at start, so a change to `plugins/job_ledger/*.py` needs `systemctl --user restart hermes-gateway`; a change under `workers/`, to `profile.yaml` or to the skill does not.
