---
name: hk-job-digest
description: "Daily Hong Kong AI job digest in Traditional Chinese, from job_candidates."
version: 0.1.0
author: hermes-harness
platforms: [linux]
metadata:
  hermes:
    tags: [Jobs, Digest, Scheduled]
---

# Hong Kong job digest

Post the new Hong Kong job ads that fit the user. The tools decide the period, pull the boards, remove every ad that is certainly wrong and every job already shown. You judge what is left against the user's profile and write the post. What you show and post is recorded afterwards from your answer — you record nothing yourself.

## When to Use

- The daily Automation asks for the job digest.
- The user asks for new jobs that fit them ("今日有冇啱我嘅工"), or for the next ones after a digest ("再嚟", "下一批"). Jobs already shown are not offered again, so this is a new digest.
- The user, in a digest's chat, says an entry was wrong or right for them, or asks about one.

## Procedure

1. Call `job_candidates` once, with no arguments.
2. If it returns an error: write one line saying the job digest could not run and why. Do not write any job from memory. Stop.
3. If `entries` is empty: write one line saying there are no new jobs this period, and name any failed sources. Stop.
4. Judge every entry as **Judging** says. Decide keep or drop for each before you write anything.
5. For a kept entry whose `jd` stops before you can tell what the work is, call `job_read` with its id. At most five `job_read` calls.
6. Write the post (format below) as your answer. Every statement about a job comes from its `jd` or `job_read` text — never from its title alone, never from memory. Every kept entry cites its `url`: that is how it is recorded as posted.

## Judging

Judge against the profile in the `job_candidates` result and nothing else — not memory, not earlier chats. Code has already removed every ad that is certainly wrong for this person (on-site, below their salary floor, a management or intern title, no AI anywhere in the ad, six or more years of experience required). Your job is the last step: for each entry, decide whether this person would want to open the ad.

Judge each entry by what the ad says the person will do day to day. A title often says little about the work, so never keep or drop an entry because of its title. Keep an entry when the work could be what their profile describes, even if some details are unknown. Drop it when the work is clearly something else. An item in the not-wanted list applies only when it is the main work of the role, not when it is one duty among others. An unstated salary, work arrangement or seniority is never a reason to drop.

## Format

Markdown, Traditional Chinese, standard written Chinese (書面語) — not written Cantonese: 「的」「是」「現在」, never 「嘅」「係」「而家」. Technical terms stay in English, untranslated, as your standing instructions say. Job titles and company names stay exactly as the ad writes them.

```
## 香港 AI 職位 · <window.date_hkt>

### <id> · <title>
<company> · <salary, or 薪金未列明> · <marks>
<一至兩句：日常工作做甚麼，出自 JD；與這位用家的經驗或想做的工作吻合在哪裡。>
[查看職位](<url>)
```

- Entries in the order `job_candidates` gave them. Use each entry's own `id`: the user answers with it.
- `marks`, written as: `agency` 中介刊登 · `outsourcing` 外判派駐 · `contract` 合約 · `hybrid` hybrid · `remote` remote · `arrangement unstated` 工作模式未列明 · `requires N years` 要求 N 年經驗 · `JD unread` JD 未能讀取. Leave out `salary unstated`: the salary slot says it.
- If `also_posted_as` is not empty, add 「（同一職位亦以 N 個產品刊登）」 after the title.
- End with one line: 「今次檢視 <entries 的數目> 個職位，保留 <kept> 個。如某個職位不合適，可回覆例如「J3 不合適，因為……」。」
- If `more` is above 0, add one line: 「另有 <more> 個職位留待下次檢視。」
- If `pull.new` is false, add one line: 「職位來源上次讀取於 <pull.at_hkt>，今次沒有重新讀取。」
- If `errors` is not empty, add one line: 「以下來源今次未能讀取：…」.
- If you kept none: write the heading, then 「今次檢視的 <n> 個職位都不合適。」 and the lines above. Cite no url.
- No introduction, no closing summary.

## Feedback

When the user says an entry was wrong for them ("J3 唔啱，佢其實係做 IT support"), call `job_feedback` with that id, `verdict: wrong` and their reason in their words; `verdict: right` when they say a kept one was a good find. Record only what the user said, and only there: never in memory. Feedback changes nothing by itself — the profile is changed by the user after reviewing it — so confirm in one line that it is recorded, and promise nothing about future digests. If they ask about an entry, answer from `job_read`.
