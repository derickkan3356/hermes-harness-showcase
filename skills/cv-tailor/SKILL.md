---
name: cv-tailor
description: "A CV tailored to one job, from the user's master of facts: Word and PDF attached to the reply, edited in the same chat."
version: 0.1.0
author: hermes-harness
platforms: [linux]
metadata:
  hermes:
    tags: [CV, Jobs, Writing]
---

# CV for one job

Write the user a CV for one job ad, from their master CV of facts. The goal is to pass screening: ATS software (applicant tracking system, which filters CVs by keyword) and then a recruiter who skims the first page for under a minute. Everything on the CV is a true fact from the master. You choose which facts, in what order, and the words. The tools print company, titles, dates, education, contact, languages and right to work from the master, and check what you wrote.

## When to Use

- The user pastes a JD (job description) or a job ad link's text and asks for a CV, or asks for a CV for an entry of the job digest ("幫我整份CV申請J3").
- In a chat that already has a CV draft, the user asks for a change ("第二點改短啲", "加返 OCR 嗰個 project", "summary 唔好提 Azure").

If the user names a digest entry (J3), get its JD with `job_read`. If they give a link, read it with `web_extract`; ask them to paste the JD only when the page does not give it (a login wall, an empty page). If the JD is already in this chat, do not fetch it again. If the chat is a job digest chat, ask them to start a new chat for the CV: a digest fills the context.

## Procedure: a new CV

1. Read the JD. Note:
   - the job title and the hiring company (empty if an agency hides it);
   - the hard requirements: years, degree, languages, right to work, certifications;
   - the key terms, 10 to 25, each one to four words exactly as the JD writes them: skills, tools, domains ("RAG", "vector databases", "LLM APIs"). Not sentences: a term is checked by whether those exact words appear in the CV, and a sentence never does.
   - application instructions: expected salary, Word only, a reference number, a cover letter, a deadline.
2. Call `cv_master`.
3. Choose. For each JD duty and requirement, find the facts that show it. Most relevant first: the recruiter reads the top third of page one. Leave out what does not help this job, even if it is impressive. Two pages at most: that is this skill's rule, not the JD's, so do not tell the user the JD wants it. About 10 to 14 bullets in all.
4. Write the draft with `cv_write`:
   - `summary`: two or three sentences. Describe the user as what the facts show ("AI engineer"), not as the target title: they do not hold it, and the official titles are printed under each role. State the years from `derived.years_of_experience` and the two or three strengths this JD wants most, each backed by a cited fact.
   - `blocks`: the projects you chose, best first. A role id as a block holds that role's own facts. A project the JD does not need is simply absent. A capstone or personal project goes in only if it shows something the work projects do not.
   - each bullet: one claim, one to two lines, citing the facts it rests on. You may combine two facts of the same block in one bullet.
   - `skills`: the master's skills the JD asks for, under short labels, most relevant line first. Use the JD's wording only where it names the same thing. Programming languages go under "Programming", never "Languages": the CV prints spoken languages under that word.
5. Read `checks`. Rewrite a bullet once with `cv_edit` if its numbers are not in its facts. Decide on the rest yourself: a name not in the facts is fine when it is the same thing in other words, and wrong when it adds a claim.
6. Look at `jd_terms_missing`. Where a fact supports a missing term, use the JD's word in that bullet. A gap with no fact behind it stays a gap: never add a term the facts do not show.
7. Call `cv_render`. If it says too many pages, cut bullets and render again.
8. Reply to the user, in their language:
   - one line on the angle you took for this job;
   - the JD terms still missing, as a short list: these are gaps the user may want to address in an interview or a cover letter;
   - hard requirements the master does not meet (for example, 5 years wanted, 3 shown);
   - the application instructions from step 1;
   - any bullets `cv_render` flagged, by id.
   Do not paste the CV back: the files are attached. Do not write a file path or link.

## Procedure: an edit

1. Change only what the user asked, with `cv_edit`, by bullet id. Never rewrite the whole draft for a small change. If you do not know which bullet they mean, the ids are in the last `cv_write` or `cv_edit` result; ask if still unclear.
2. Handle `checks` as in a new CV, step 5.
3. Call `cv_render`, and reply in one or two lines with what changed.

If the user asks for a fact the master does not have (a new number, a tool they used, a project), say it is not in the master and that they can add it to `cv/master.yaml`. Do not write it.

## Writing

- Plain words. State the fact. No inflated stakes. Short.
- Bullets open with a past-tense verb (Built, Cut, Ran, Wrote, Led). No "I", "my", "me". The current role uses the past tense too.
- Concrete: names, numbers, tools, scale, from the facts. Every number comes from a cited fact as written, with its qualifier: "more than 20" stays "more than 20", never "about 20"; "about 13 s" stays "about". Do not compute new numbers (no percentages from two times).
- The result first when the fact has one: "Cut processing time per document from about 40 s to about 13 s by …", not "Optimised the pipeline, which reduced …".
- No three-item rhythm lists for effect. No em dashes. Repeat ordinary words instead of hunting synonyms.
- No marketing words (spearheaded, leveraged, passionate, robust, innovative, cutting-edge and the like). `checks` names any it finds.
- Use the JD's word for a thing when the fact is the same thing (JD "RAG", fact "retrieval-augmented generation": write "retrieval-augmented generation (RAG)" once).
- Follow a project's `seeds` when given: the user's own phrasing for an angle, or how they want the project framed.
- Client names never appear. The master already describes clients in general terms; keep it that way.
- Write the English of a fluent professional. Do not fake errors, and do not polish into marketing English.
