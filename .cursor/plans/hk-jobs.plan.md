status: active

# HK job digest — daily Open WebUI post of Hong Kong job ads that fit the user's profile

The second use case on the fetch layer. Once a day an Open WebUI Automation asks Hermes for new Hong Kong job ads that match the user's profile; each run is a new chat. Same shape as the AI news digest (`docs/ai-news.md`): a plugin pulls fixed sources through the fetch cascade, code windows, dedups and ranks, the model judges the shortlist and writes, a `post_llm_call` hook records what was posted.

## Where things are

How the digest works, what code drops and why, how the model judges, the sources and what is not used: `docs/hk-jobs.md`. The user's profile and rule settings: `plugins/job_ledger/profile.yaml`. Labels: `evals/jobs/labels/` — `20260925.jsonl` judged on title and teaser, `20260925-jd.jsonl` judged after reading the JD; both were used to write the profile, so neither is a held-out test. Live state: `tools/facts.sh jobs`.

Sources, as of this workstream:

- JobsDB: in.
- CTgoodjobs: in. Its AWS WAF CAPTCHA (from 2026-09-25 about 05:00) was gone by the 2026-09-27 09:30 run, which pulled without an error; 6 probe requests on 2026-09-27/28 all answered 200.
- LinkedIn: deferred, the user's call — only if the other boards are too thin.
- JSearch (Google for Jobs): standby, if a board is lost.

## Hot

- Biggest unknown: whether CTgoodjobs stays open at one run a day — 7–8 search pages and 3–21 JDs, 5 s apart; open on 2026-09-28 and 09-29. Without it the digest has JobsDB alone, and 24 of the 26 jobs the user marked wanted were on CTgoodjobs only.
- [x] Pull by date, hold ads and JDs in the ledger, first pulled first (`docs/hk-jobs.md`, Period to Not done). Evidence, throwaway ledger 2026-09-28 04:42: run 1 sent CTgoodjobs 7 search + 15 JD requests, no error, 256 ads, 55 jobs past the rules, CTgoodjobs entries among the 5 shown; after `record_post`, run 2 sent 0 JobsDB JDs, showed the next 5, no repeat. Its 3 CTgoodjobs JD requests were pages with no JD (client-rendered), now held and not fetched again (offline check). The real ledger was seeded with that pull, so the 09:30 run fetches only what is new.
- [x] Posted records from the entry's heading or its url, and CTgoodjobs urls carry no slug (`docs/hk-jobs.md`, Period). On 10-01, J4's url came out one percent-escape wrong in the post, and J4 was not recorded; it is now added to the real ledger. Replay on ledger copies: the 10-01 post records J3 and J4, the 10-02 post (both dropped, "J1" in prose) records none. No other past post shows an unrecorded heading.
- [ ] Next step: the 2026-10-03 09:30 run, the first on the recording fix — every `### J` heading in the post is in `posted`, CTgoodjobs links are `/job/<id>`. Three runs in a row already posted on their own (09-30, 10-01, 10-02), `high_water` advanced each day, no job repeats in `posted`. So far on the new code: 2026-09-28 CTgoodjobs 7 search + 3 JD requests, no error, 3 of 20 entries CTgoodjobs, 1 posted; 09-29 8 + 21, no error, 13 of 20, 2 posted, both CTgoodjobs.
- Done when: the Automation posts on its own three runs in a row; no posted job breaks a hard deny or repeats an earlier day's; a failing source is named in the post, not silent.

## Open

- Is the model's judgement right? The user gives no verdicts unless a post is wrong enough to say so, and labels given for their own sake would be poor ones, so `feedback` stays empty. What the user does instead is a signal: on 2026-10-02 they asked for a tailored CV for 5 jobs, all 5 posted by the digest (of 9 posted). That says the kept ones are wanted; it says nothing about the dropped ones. Record a CV request against its job in the ledger as a verdict, and read dropped entries some other way?
- Query shape: the `人工智能` query on CTgoodjobs surfaces English titles the six keywords miss — Analyst Programmer (AI), Systems Analyst (Artificial Intelligence), Technology Officer (AI). Which query set covers a labelled day, and does a JobsDB classification (ICT) net with the no-AI-in-ad rule beat keywords?
- Capacity: with CTgoodjobs back, how many jobs pass the rules a day? Over the ledger's 7 days: 55 on 2026-09-28, 89 on 09-29; the queue (`more`) went 14 → 28. Steadily above `limit` (20) means a longer queue every day whatever the order; then raise `limit` (the code allows 40; 20 entries were a 67k-character tool result) or tighten the rules. The user can drain it by asking for the next entries in chat: a call within 6 h of the last pull reads the ledger alone (`docs/hk-jobs.md`, Period); on the 09-29 ledger that call gave the 8 waiting entries with no request. Does asking once more a day keep up, before a job ages out of the 7-day window unshown?
- RemoteJobsOne (JobsDB): no employer named on any of its ads. Is an unnamed employer acceptable to the user, or a mark the post should carry? Why it is not denied: `docs/hk-jobs.md` (What code drops).
- CTgoodjobs ads on a company's own template (`templateId` 4000) load their JD in the browser after the page, so they go to the model as `JD unread` — 3 of 18 CTgoodjobs JDs on 2026-09-28. Where does the page get it, and is that request worth one more per such ad?
- Agency ads hide the employer, so one job posted by an agency on one board and by the employer on the other is not recognised as one; how often, and whether it matters, is not measured.

## Rejected

What was rejected and why — Glassdoor, Indeed, OfferToday's rate limit, LinkedIn's terms, `web_search` as a source, the job APIs, getting past CTgoodjobs' CAPTCHA: `docs/hk-jobs.md` (Sources).

Background fetch job and skipping fetched ads in place of seen: `docs/hk-jobs.md` (Not done).

Denying RemoteJobsOne by advertiser, and a JD rule on hourly pay: `docs/hk-jobs.md` (What code drops).
