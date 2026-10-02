"""Pulling JobsDB and CTgoodjobs, and reading each into one record shape.

Shared by the plugin's worker and by `evals/jobs/jobs_eval.py`, so the eval
measures the code the digest runs. The bytes come from the fetch cascade's HTTP
layer (`raw_client.fetch_raw`); which URLs to ask for and how to read what comes
back is this file's.

A record:

    source, source_id, url, title, company, advertiser, posted (ISO),
    salary_text, salary_top (HKD a month, or None when not stated),
    below_floor (True / False / None — None when the source cannot say),
    arrangement ("On-site" / "Hybrid" / "Remote" / None), work_types,
    career_level, teaser, bullets, classification, nets (which queries found it)

JobsDB states an arrangement on every ad, and a salary range on every ad even
when it shows none, so its salary filter is exact: an ad the topic query returns
without the filter and not with it has a range that tops out below the floor.
CTgoodjobs states neither field most of the time; its records carry what it
does state and nothing more.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "fetch_cascade" / "workers"))
from raw_client import fetch_raw, unset_proxy  # noqa: E402,F401 — unset_proxy is re-exported

HKT = timezone(timedelta(hours=8))

# --- JobsDB -------------------------------------------------------------------

JOBSDB_API = "https://hk.jobsdb.com/api/jobsearch/v5/search"
JOBSDB_PAGE = 100
JOBSDB_MAX_PAGES = 10
# The search takes only these day ranges; the window itself is applied to
# `listingDate` afterwards.
JOBSDB_RANGES = [1, 3, 7, 14, 31]


def _jobsdb_url(page: int, days: int, keywords: str = "", floor: int | None = None,
                arrangements: list[int] | None = None) -> str:
    q = {"siteKey": "HK-Main", "sourcesystem": "houston", "locale": "en-HK", "sortmode": "ListedDate",
         "pageSize": JOBSDB_PAGE, "page": page,
         "dateRange": next((r for r in JOBSDB_RANGES if r >= days), JOBSDB_RANGES[-1])}
    if keywords:
        q["keywords"] = keywords
    if floor:
        q["salaryrange"] = f"{floor}-"
        q["salarytype"] = "monthly"
    if arrangements:
        q["workarrangement"] = ",".join(str(a) for a in arrangements)
    return f"{JOBSDB_API}?{urlencode(q)}"


def _jobsdb_record(j: dict, net: str) -> dict:
    arr = [a.get("label", {}).get("text") for a in (j.get("workArrangements") or {}).get("data", [])]
    cls = (j.get("classifications") or [{}])[0]
    return {
        "source": "jobsdb", "source_id": str(j["id"]), "url": f"https://hk.jobsdb.com/job/{j['id']}",
        "title": j.get("title") or "", "company": j.get("companyName") or "",
        "advertiser": (j.get("advertiser") or {}).get("description") or "",
        "posted": j.get("listingDate"), "salary_text": j.get("salaryLabel") or "",
        "salary_top": None, "below_floor": None, "arrangement": arr[0] if arr else None,
        "work_types": list(j.get("workTypes") or []), "career_level": None,
        "teaser": j.get("teaser") or "", "bullets": list(j.get("bulletPoints") or []),
        "classification": " / ".join(filter(None, [(cls.get("classification") or {}).get("description"),
                                                  (cls.get("subclassification") or {}).get("description")])),
        "nets": [net],
    }


def _jobsdb_pull(query: dict, days: int, report: dict) -> tuple[list[dict], str | None]:
    """Every page of one JobsDB query: the ads, or an error."""
    first = fetch_raw([_jobsdb_url(1, days, **query)])[0]
    report["requests"] += 1
    if "error" in first:
        return [], first["error"]
    try:
        d = json.loads(first["body"])
    except ValueError as exc:
        return [], f"page 1 is not JSON: {exc}"
    ads = list(d.get("data") or [])
    pages = min(JOBSDB_MAX_PAGES, -(-int(d.get("totalCount") or 0) // JOBSDB_PAGE))
    rest = fetch_raw([_jobsdb_url(p, days, **query) for p in range(2, pages + 1)], concurrency=3)
    report["requests"] += len(rest)
    for r in rest:
        if "error" in r:
            return ads, f"a later page failed: {r['error']}"
        try:
            ads += json.loads(r["body"]).get("data") or []
        except ValueError as exc:
            return ads, f"a later page is not JSON: {exc}"
    return ads, None


def pull_jobsdb(profile: dict, days: int) -> tuple[list[dict], dict]:
    """The topic net (with and without the salary floor) and the wide net, merged by ad id."""
    cfg, floor = profile["jobsdb"], profile["salary_floor"]
    since = datetime.now(timezone.utc) - timedelta(days=days)
    records: dict[str, dict] = {}
    report = {"queries": 0, "requests": 0, "ads": 0, "errors": []}
    passing: set[str] = set()

    queries = [("topic", {"keywords": k}) for k in cfg["keywords"]] \
        + [("floor", {"keywords": k, "floor": floor}) for k in cfg["keywords"]] \
        + [("wide", {"floor": floor, "arrangements": cfg["wide_arrangements"]})]
    for net, query in queries:
        ads, err = _jobsdb_pull(query, days, report)
        report["queries"] += 1
        if err:
            report["errors"].append(f"jobsdb {net} {query.get('keywords', '')}: {err}".strip())
        for j in ads:
            posted = _parse_time(j.get("listingDate"))
            if posted is None or posted < since:
                continue
            sid = str(j["id"])
            if net == "floor":
                passing.add(sid)
                continue
            if net == "wide":
                passing.add(sid)
            if sid in records:
                if net not in records[sid]["nets"]:
                    records[sid]["nets"].append(net)
            else:
                records[sid] = _jobsdb_record(j, net)
    # A salary verdict only where a query with the floor could have returned the
    # ad — a failed floor query says nothing, so it leaves the verdict unknown.
    floor_failed = any(e.startswith("jobsdb floor") for e in report["errors"])
    for sid, rec in records.items():
        if not floor_failed or sid in passing:
            rec["below_floor"] = sid not in passing
    report["ads"] = len(records)
    return list(records.values()), report


# --- CTgoodjobs -----------------------------------------------------------------

CT_SEARCH = "https://jobs.ctgoodjobs.hk/jobs/{slug}-jobs"
CT_JOB = "https://jobs.ctgoodjobs.hk/job/"
# The search's own filters, read from its search state and checked against what
# came back: `post_date` 1 is the past 24 hours, 2 the past 3 days, 3 the past 7
# days, -1 any time; the `sort` cookie 1 is relevance, 2 newest first. 30 a page.
CT_POST_DATE = [(1, "1"), (3, "2"), (7, "3")]
CT_SORT_BY_DATE = {"Cookie": "sort=2"}
CT_PAGE = 30
# CTgoodjobs sits behind AWS WAF, which answers a burst of token-less requests
# from one line with a CAPTCHA (HTTP 405) that lasts for hours; a few hundred in
# three hours were enough. One request at a time, `CT_GAP` seconds apart, search
# pages and job pages alike; how much it tolerates is not measured.
CT_GAP = 5.0
_ct = {"last": 0.0, "walled": False, "requests": 0}


def _ct_fetch(specs: list) -> list[dict]:
    """CTgoodjobs pages one at a time, paced. A 405 is its CAPTCHA wall: nothing
    more goes to CTgoodjobs from this process, since every request after it only
    meets the wall again."""
    rows = []
    for spec in specs:
        url = spec if isinstance(spec, str) else spec["url"]
        if _ct["walled"]:
            rows.append({"url": url, "status": 405, "headers": {},
                         "error": "not sent: CTgoodjobs is showing a CAPTCHA to this line"})
            continue
        time.sleep(max(0.0, _ct["last"] + CT_GAP - time.monotonic()))
        r = fetch_raw([spec])[0]
        _ct["last"] = time.monotonic()
        _ct["requests"] += 1
        if r.get("status") == 405:
            _ct["walled"] = True
            r["error"] = "HTTP 405: CTgoodjobs is showing a CAPTCHA to this line"
        rows.append(r)
    return rows


def ct_requests() -> int:
    """Requests sent to CTgoodjobs by this process, search and job pages."""
    return _ct["requests"]


_FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')


def _ct_url(slug: str, page: int, post_date: str = "-1") -> str:
    q = {k: v for k, v in (("post_date", post_date), ("page", page)) if v not in ("-1", 1)}
    url = CT_SEARCH.format(slug=quote(slug))
    return f"{url}?{urlencode(q)}" if q else url


def _ct_stream(html: str) -> str:
    return "".join(json.loads(f'"{m}"') for m in _FLIGHT.findall(html))


def _ct_filters(html: str) -> tuple[str | None, str | None]:
    """The `post_date` and sort the page says it searched with, from its search state."""
    stream = _ct_stream(html)
    pd = re.search(r'"PostDate":"(-?\d+)"', stream)
    sort = re.search(r'"Sort":(\d+)', stream)
    return (pd.group(1) if pd else None), (sort.group(1) if sort else None)


def _ct_records(html: str) -> list[dict]:
    """The job records in a search page's Next.js flight data, with `$ref`s resolved."""
    stream = _ct_stream(html)
    rows: dict[str, object] = {}
    for line in stream.split("\n"):
        m = re.match(r"^([0-9a-f]+):(.*)$", line)
        if m:
            try:
                rows[m.group(1)] = json.loads(m.group(2))
            except ValueError:
                pass

    def resolve(v, depth=0):
        if isinstance(v, str) and v.startswith("$") and v[1:] in rows and depth < 6:
            return resolve(rows[v[1:]], depth + 1)
        if isinstance(v, list):
            return [resolve(x, depth + 1) for x in v]
        if isinstance(v, dict):
            return {k: resolve(x, depth + 1) for k, x in v.items()}
        return v

    return [resolve(v) for v in rows.values() if isinstance(v, dict) and "jobId" in v and "publishTime" in v]


def _ct_salary(s: dict | None) -> tuple[str, int | None]:
    """The salary as shown, and its top in HKD a month when the ad states one."""
    s = s or {}
    text = s.get("salaryValue") or ""
    if text == "N/A":
        return "", None
    try:
        top = float(s.get("salaryTo") or s.get("salaryFrom") or 0)
    except ValueError:
        return text, None
    unit = (s.get("salaryMonthHour") or "").upper()
    if not top or unit not in ("MON", "YEAR", "YR"):
        return text, None   # hourly or unknown: no monthly figure to compare
    return text, int(top / 12 if unit != "MON" else top)


def _ct_record(c: dict, slug: str) -> dict:
    strip = lambda s: re.sub(r"<[^>]+>", "", s or "")
    salary_text, top = _ct_salary(c.get("salary"))
    levels = [lv.get("name") for lv in c.get("careerLevels") or [] if isinstance(lv, dict)]
    return {
        # The page without its slug: a Chinese title's slug is a long run of
        # percent-escapes the model copies into the post, and one wrong digit
        # is a broken link that does not record as posted.
        "source": "ctgoodjobs", "source_id": str(c["jobId"]), "url": f"{CT_JOB}{c['jobId']}",
        "title": strip(c.get("jobTitle")), "company": strip(c.get("companyName")),
        "advertiser": strip(c.get("companyName")),
        "posted": ((c.get("publishTime") or {}).get("date") or None),
        "salary_text": salary_text, "salary_top": top, "below_floor": None,
        "arrangement": None,
        "work_types": [e.get("name") for e in c.get("empTypes") or [] if isinstance(e, dict)],
        "career_level": levels[0] if levels else None,
        "teaser": "", "bullets": [strip(h) for h in c.get("highlights") or []],
        "classification": " / ".join(c.get("jobareas") or []),
        "nets": [f"ct:{slug}"],
    }


def pull_ctgoodjobs(profile: dict, days: int) -> tuple[list[dict], dict]:
    """Each slug newest first, filtered to the period by the search itself; the
    next page only while the one before came back full, up to `pages`."""
    cfg, floor = profile["ctgoodjobs"], profile["salary_floor"]
    post_date = next((v for d, v in CT_POST_DATE if d >= days), "-1")
    # `publishTime.date` is a Hong Kong calendar day.
    first_day = (datetime.now(HKT) - timedelta(days=days)).date().isoformat()
    records: dict[str, dict] = {}
    report = {"pages": 0, "ads": 0, "errors": []}
    for slug in cfg["slugs"]:
        for page in range(1, cfg["pages"] + 1):
            r = _ct_fetch([{"url": _ct_url(slug, page, post_date), "headers": CT_SORT_BY_DATE}])[0]
            report["pages"] += 1
            if "error" in r:
                report["errors"].append(f"ctgoodjobs {slug} p{page}: {r['error']}")
                break
            page_html = r["body"].decode("utf-8", errors="replace")
            # A period with no new ad is an empty page, so an empty page proves
            # nothing; the search state says whether the filters were taken.
            if _ct_filters(page_html) != (post_date, "2"):
                report["errors"].append(f"ctgoodjobs {slug}: the page did not search newest first with post_date="
                                        f"{post_date} (it shows {_ct_filters(page_html)}) — the page format may have changed")
                break
            found = _ct_records(page_html)
            for c in found:
                if not c.get("publishTime") or (c["publishTime"].get("date") or "") < first_day:
                    continue
                sid = str(c["jobId"])
                if sid in records:
                    if f"ct:{slug}" not in records[sid]["nets"]:
                        records[sid]["nets"].append(f"ct:{slug}")
                    continue
                rec = _ct_record(c, slug)
                if rec["salary_top"] is not None:
                    rec["below_floor"] = rec["salary_top"] < floor
                records[sid] = rec
            if len(found) < CT_PAGE:
                break
        if _ct["walled"]:
            break
    report["ads"] = len(records)
    return list(records.values()), report


# --- the full ad ---------------------------------------------------------------

_BLOCK = re.compile(r"<\s*(br|/p|/li|/h[1-6]|/div|/tr)\b[^>]*>", re.I)


def _text(fragment: str) -> str:
    """HTML to plain text, one line per block, entities decoded."""
    s = _BLOCK.sub("\n", fragment or "")
    s = re.sub(r"<\s*li\b[^>]*>", "\n- ", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    lines = (" ".join(line.split()) for line in s.split("\n"))
    return "\n".join(line for line in lines if line)


def _jobsdb_jd(page: str) -> str:
    """The ad body from a JobsDB job page: the longest `"content"` string in its embedded JSON."""
    best = ""
    for m in re.finditer(r'"content":"', page):
        try:
            s, _ = json.JSONDecoder().raw_decode(page, m.end() - 1)
        except ValueError:
            continue
        if isinstance(s, str) and not s.lstrip().startswith("<style") and len(s) > len(best):
            best = s
    return _text(best)


def _ct_jd(page: str) -> str:
    """The ad body from a CTgoodjobs job page's `JobPosting` JSON-LD."""
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            return _text(d.get("description") or "")
    return ""


# Where the part of a JD about the work begins. Many open with a page about the
# company; AXA's first 2,400 characters are, and an excerpt from the top missed the job.
_WORK_HEADING = re.compile(
    r"^\W*(key |main |your |major )?(responsibilit|duties|what you('ll| will)|the role|about the role|"
    r"role overview|job description|job summary|position summary|requirements|qualifications|"
    r"職責|工作內容|工作职责|岗位职责|職位描述|職務|要求|任職要求)", re.I | re.M)
JD_EXCERPT = 4000


def jd_excerpt(text: str, limit: int = JD_EXCERPT) -> str:
    """The JD from its first heading about the work, at most `limit` characters.

    The whole text when no such heading appears.
    """
    m = _WORK_HEADING.search(text or "")
    body = (text or "")[m.start():] if m else (text or "")
    return body[:limit]


NO_TEXT = "no ad text found in the page"


def fetch_jd(ads: list[dict], concurrency: int = 4) -> dict[str, dict]:
    """The full text of each ad, keyed `source:source_id`: `{"text"}` or `{"error"}`.

    An empty text is an error, not an ad with nothing in it: the page came back
    in a shape this parser does not know (`NO_TEXT`). Some CTgoodjobs ads on a
    company's own template load their JD in the browser, after the page.
    """
    readers = {"jobsdb": _jobsdb_jd, "ctgoodjobs": _ct_jd}
    jobsdb = [a for a in ads if a["source"] == "jobsdb"]
    ct = [a for a in ads if a["source"] == "ctgoodjobs"]
    rows = fetch_raw([a["url"] for a in jobsdb], concurrency=concurrency) + _ct_fetch([a["url"] for a in ct])
    out = {}
    for a, r in zip(jobsdb + ct, rows):
        key = f"{a['source']}:{a['source_id']}"
        if "error" in r:
            out[key] = {"error": r["error"]}
            continue
        text = readers[a["source"]](r["body"].decode("utf-8", errors="replace"))
        out[key] = {"text": text} if text else {"error": NO_TEXT}
    return out


# --- shared -------------------------------------------------------------------

def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=HKT)


def pull_all(profile: dict, days: int) -> tuple[list[dict], dict]:
    jobsdb, jr = pull_jobsdb(profile, days)
    ct, cr = pull_ctgoodjobs(profile, days)
    return jobsdb + ct, {"jobsdb": jr, "ctgoodjobs": cr}
