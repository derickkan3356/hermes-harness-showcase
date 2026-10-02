"""One job from many ads, what drops it for certain, and what the post marks.

The line (`docs/hk-jobs.md`, What code drops): drop an ad the user would reject
for certain, keep one that might fit. So a deny reads what an ad states; an
unstated salary or arrangement keeps the ad. The one absence that counts is AI
missing from the whole ad, JD included — the JD is the ad's own account of the work:

  on-site        JobsDB's `workArrangements` says On-site. It is the hirer's
                 choice, not a default (the plan's Hot has the evidence).
  below floor    the salary range tops out under `salary_floor`: JobsDB's own
                 filter says so, or CTgoodjobs states a monthly figure.
  seniority      the title carries a prefix in `deny_titles`.
  no AI in ad    title, teaser, highlights and JD together name none of
                 `ai_terms`. The title alone is not enough: read after the JD,
                 an SRE and a data science analyst ad were AI agent and RAG
                 work the user wanted.
  experience     the JD states a minimum of `experience_deny_from` years or more
                 (`required_years`).
Both read the JD, so they run only on jobs the rules above keep; a job whose JD
could not be read is kept and marked.

A job seen on both boards is one job, and a deny stated on either board holds
for both: a CTgoodjobs ad that is a JobsDB On-site ad is on-site. Travel,
overseas and an on-site line in the JD are for the model, which reads the JD.

Marks, not denies: agency, outsourcing (a firm placing its own staff at a
client), contract, arrangement unstated, salary unstated.

`collapse` then folds one company's role posted once per product ("Product
Engineer - AI Finance App", "… - AI Neobank App") into one entry.
"""

from __future__ import annotations

import re
import unicodedata

_LEGAL = re.compile(
    r"\b(limited|ltd|co|company|corp|corporation|inc|group|holdings?|international|"
    r"hong kong|hk|asia|asia pacific|the|sar)\b")


def _company_words(name: str) -> list[str]:
    s = unicodedata.normalize("NFKC", name or "").lower()
    s = re.sub(r"\(.*?\)|（.*?）", " ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return _LEGAL.sub(" ", s).split()


def company_key(name: str) -> str:
    """A company name with case, punctuation and legal and place suffixes gone.

    CTgoodjobs lists one firm under two spellings ("TEKsystems" and "TEKsystems
    Hong Kong", "A-field Tech Limited" and "A-Field Tech").
    """
    return "".join(_company_words(name))


def title_key(title: str) -> str:
    s = unicodedata.normalize("NFKC", title or "").lower()
    s = re.sub(r"[\s\-–—|]+hong kong\s*$", "", s)
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def _any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(0)
    return None


def _named(names: list[str], *candidates: str) -> str | None:
    """The first of `names` whose words appear, in order and whole, in a candidate's."""
    for n in names:
        want = _company_words(n)
        for c in candidates:
            have = _company_words(c)
            if want and any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1)):
                return n
    return None


def merge(records: list[dict]) -> list[dict]:
    """Ads grouped into jobs by company and title; JobsDB first within a job."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        key = (company_key(r["company"] or r["advertiser"]), title_key(r["title"]))
        groups.setdefault(key, []).append(r)
    # One employer under a longer and a shorter name ("Sun Life" and "Sun Life
    # Financial, Inc."): same title, and one name starts the other.
    for ck, tk in sorted(groups, key=lambda k: len(k[0])):
        if (ck, tk) not in groups or len(ck) < 4:
            continue
        for other in [k for k in groups if k[1] == tk and k[0] != ck and k[0].startswith(ck)]:
            groups[(ck, tk)] += groups.pop(other)
    jobs = []
    for (ck, tk), ads in groups.items():
        ads.sort(key=lambda a: (a["source"] != "jobsdb", a["posted"] or ""))
        lead = ads[0]
        jobs.append({
            "key": f"{ck}|{tk}", "title": lead["title"], "company": lead["company"] or lead["advertiser"],
            "posted": max(a["posted"] or "" for a in ads) or None,
            "sources": sorted({a["source"] for a in ads}),
            "ads": ads,
        })
    return jobs


def judge(job: dict, profile: dict) -> dict:
    """The job with `deny` (every reason that applies, empty when it stays) and `marks` set."""
    ads = job["ads"]
    deny, marks = [], []

    if any(a["arrangement"] == "On-site" for a in ads):
        deny.append("on-site")
    if any(a["below_floor"] is True for a in ads):
        deny.append("below floor")
    prefix = _any(profile["deny_titles"], job["title"])
    if prefix:
        deny.append(f"seniority: {prefix.strip()}")

    text = " ".join([job["title"]] + [a["teaser"] for a in ads] + [b for a in ads for b in a["bullets"]])
    agency = _named(profile["agencies"], *[a["advertiser"] for a in ads], *[a["company"] for a in ads]) \
        or _any(profile["agency_phrases"], text)
    outsourcing = _named(profile["outsourcing"], *[a["advertiser"] for a in ads], *[a["company"] for a in ads]) \
        or _any(profile["outsourcing_phrases"], text)
    if outsourcing:
        marks.append("outsourcing")
    elif agency:
        marks.append("agency")
    if any(re.search(r"contract|temp|freelance", w, re.I) for a in ads for w in a["work_types"]):
        marks.append("contract")
    stated = [a["arrangement"] for a in ads if a["arrangement"]]
    if not stated:
        marks.append("arrangement unstated")
    elif "Remote" in stated:
        marks.append("remote")
    elif "Hybrid" in stated:
        marks.append("hybrid")
    if all(a["below_floor"] is None for a in ads) and not any(a["salary_text"] for a in ads):
        marks.append("salary unstated")

    return {**job, "deny": deny, "marks": marks}


_YEARS = re.compile(
    r"(?:(?:at least|minimum(?: of)?|min\.?|over|more than|no less than)\s+)?(\d{1,2})\s*\+?\s*"
    r"(?:or more|or above)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b[^.\n]{0,60}?\bexperience"
    r"|experience\s+(?:of\s+)?(?:at least|minimum(?: of)?|over|more than)?\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)"
    r"|(\d{1,2})\s*年(?:或)?以上[^。\n]{0,20}(?:經驗|经验)", re.I)
_PREFERRED = re.compile(r"prefer|advantage|a plus|desirable|nice to have|優先|优先", re.I)


def required_years(text: str) -> tuple[int, str] | None:
    """The lowest minimum of years of experience a JD requires, with the sentence it came from.

    A range counts by its lower end ("5-12 years" is 5). A requirement whose own
    clause says "preferred", "a plus" or the like is not one.
    """
    found = []
    for m in _YEARS.finditer(text or ""):
        n = int(next(g for g in m.groups() if g))
        clause = re.split(r"[.;,\n。；，]", text[m.end():m.end() + 80], maxsplit=1)[0]
        if 0 < n < 30 and not _PREFERRED.search(m.group(0) + clause):
            found.append((n, " ".join(text[max(0, m.start() - 30):m.end() + 30].split())))
    return min(found) if found else None


def judge_jd(job: dict, profile: dict, jd: dict[str, dict]) -> dict:
    """The rules that read the JD, on a job the other rules kept."""
    bodies = [jd.get(f"{a['source']}:{a['source_id']}", {}).get("text") for a in job["ads"]]
    if not any(bodies):
        return {**job, "marks": job["marks"] + ["JD unread"]}
    ads = job["ads"]
    short = " ".join([job["title"]] + [a["teaser"] for a in ads] + [b for a in ads for b in a["bullets"]])
    if not _any(profile["ai_terms"], short + " " + " ".join(b for b in bodies if b)):
        return {**job, "deny": job["deny"] + ["no AI in ad"]}
    req = required_years("\n".join(b for b in bodies if b))
    if req is None:
        return job
    years, quote = req
    if years >= profile["experience_deny_from"]:
        return {**job, "deny": job["deny"] + [f"experience: {years}+ years"], "evidence": quote}
    if years > profile["experience_years"]:
        return {**job, "marks": job["marks"] + [f"requires {years} years"], "evidence": quote}
    return job


def judge_all(records: list[dict], profile: dict, read_jd=None) -> list[dict]:
    """Every job judged. With `read_jd` (ads → `sources.fetch_jd`'s map), the JDs of
    the jobs the other rules keep are read for the rules that need them; a job
    already denied costs no fetch."""
    jobs = [judge(j, profile) for j in merge(records)]
    if read_jd is None:
        return jobs
    jd = read_jd([a for j in jobs if not j["deny"] for a in j["ads"]])
    return [judge_jd(j, profile, jd) if not j["deny"] else j for j in jobs]


def role_key(title: str) -> str:
    """The title before its first " - ": the role, without the product it is for."""
    return title_key(re.split(r"\s+[-–—]\s+", title or "", maxsplit=1)[0])


def collapse(jobs: list[dict]) -> list[dict]:
    """Survivors as entries: one company's role posted once per product becomes one.

    Agency ads never collapse: an agency's "AI Engineer - Bank" and "AI Engineer
    - Insurer" are two clients' jobs, not one role twice.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for j in jobs:
        if j["deny"]:
            continue
        key = (j["key"].split("|")[0], role_key(j["title"]))
        if "agency" in j["marks"] or key[1] == title_key(j["title"]):
            key = (j["key"], "")   # nothing after " - " to vary, or an agency: stays alone
        groups.setdefault(key, []).append(j)
    entries = []
    for members in groups.values():
        members.sort(key=lambda j: j["posted"] or "", reverse=True)
        lead = members[0]
        entries.append({**lead, "variants": [j["title"] for j in members],
                        "keys": [j["key"] for j in members],
                        "marks": list(dict.fromkeys(m for j in members for m in j["marks"]))})
    return entries
