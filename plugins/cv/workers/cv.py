# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml", "docxtpl", "pypdf"]
# ///
"""The cv worker: the master for the agent, the draft per chat, its checks, and the files.

Runs as its own process under `uv run --script`, as the job and news workers
do. `cv.py.lock` pins the environment.

Protocol: one JSON object on stdin, one on stdout. Every op but `master` takes
`session`: the chat's session id, which the plugin resolves to the root of the
Hermes lineage. One draft per chat.

    in   {"op": "master"}
    out  {"roles", "projects", "education", "skills", "languages", "right_to_work",
          "notice_period", "derived"}

    in   {"op": "write", "session", "draft": {"target", "jd_terms", "summary", "blocks", "skills"}}
    in   {"op": "edit", "session", "ops": [...]}
    out  {"cv": str, "checks": [str], "jd_terms_missing": [str]}  or {"error"}

    in   {"op": "render", "session"}
    out  {"files": [name], "pages": int, "flagged": [str]}  or {"error"}

Company, titles, dates, education, contact, languages and right to work are
printed from the master; the draft only names projects and roles by id. A
bullet cites the facts it rests on, and only facts of the block it sits in.
Roles always appear, newest first, whether the draft gives them bullets or not:
a CV with a missing job reads as a gap. The user edits the master between
turns: a draft that cites what the master no longer has is reported by every
review and refused by `render` until it is rewritten.

The draft lives at `~/.hermes/cv/drafts/<session>.json` and the rendered files
in `~/.hermes/outbox/<session>/`, where the Open WebUI Pipe picks them up after
the turn. Both are runtime state, not tracked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import yaml

import checks

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
MASTER = Path(os.environ.get("CV_MASTER") or REPO / "cv" / "master.yaml")
TEMPLATE = Path(os.environ.get("CV_TEMPLATE") or REPO / "cv" / "template.docx")
HERMES = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
DRAFTS = HERMES / "cv" / "drafts"
OUTBOX = HERMES / "outbox"

_SESSION = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
PAGES_MAX = 2


class Refused(Exception):
    """A draft that cannot be stored as given. The message says what to change."""


# ---------------------------------------------------------------- master

def load_master() -> dict:
    m = yaml.safe_load(MASTER.read_text())
    m["_facts"] = {}
    m["_owner"] = {}
    for r in m["roles"]:
        for f in r.get("facts") or []:
            m["_facts"][f["id"]] = f["text"]
            m["_owner"][f["id"]] = r["id"]
    for p in m["projects"]:
        for f in p.get("facts") or []:
            m["_facts"][f["id"]] = f["text"]
            m["_owner"][f["id"]] = p["id"]
    m["_roles"] = {r["id"]: r for r in m["roles"]}
    m["_projects"] = {p["id"]: p for p in m["projects"]}
    return m


def _ym(v) -> tuple[int, int] | None:
    if v is None:
        return None
    y, mo = str(v).split("-")[:2]
    return int(y), int(mo)


def years_of_experience(m: dict) -> int:
    start = min(_ym(r["from"]) for r in m["roles"])
    today = date.today()
    return (today.year * 12 + today.month - (start[0] * 12 + start[1])) // 12


def op_master(_: dict) -> dict:
    m = load_master()
    roles = []
    for r in m["roles"]:
        roles.append({
            "id": r["id"], "company": r["company"], "about": r.get("company_about"),
            "titles": r["titles"], "from": str(r["from"]), "to": str(r["to"]) if r.get("to") else "present",
            "facts": {f["id"]: f["text"] for f in r.get("facts") or []},
            "projects": r.get("projects") or [],
        })
    projects = []
    for p in m["projects"]:
        entry = {
            "id": p["id"], "role": p.get("role"), "name": p["name"],
            "client": p.get("client"), "kind": p.get("kind"), "my_role": p.get("my_role"),
            "from": str(p["from"]), "to": str(p["to"]) if p.get("to") else "present",
            "facts": {f["id"]: f["text"] for f in p.get("facts") or []},
        }
        if p.get("seeds"):
            entry["seeds"] = p["seeds"]
        projects.append(entry)
    return {
        "roles": roles, "projects": projects,
        "education": [{k: str(v) for k, v in e.items()} for e in m["education"]],
        "skills": m["skills"], "languages": m["languages"],
        "right_to_work": m.get("right_to_work"), "notice_period": m.get("notice_period"),
        "derived": {"years_of_experience": years_of_experience(m)},
    }


# ---------------------------------------------------------------- draft

def _draft_path(session: str) -> Path:
    if not _SESSION.match(session or ""):
        raise Refused("no usable session id")
    return DRAFTS / f"{session}.json"


def _load(session: str) -> dict:
    path = _draft_path(session)
    if not path.exists():
        raise Refused("there is no draft in this chat yet: write one with cv_write first")
    return json.loads(path.read_text())


def _save(session: str, d: dict) -> None:
    path = _draft_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    tmp.replace(path)


def _text(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _facts(v, block: str | None, m: dict) -> list[str]:
    ids = [str(x).strip() for x in (v or []) if str(x).strip()]
    unknown = [i for i in ids if i not in m["_facts"]]
    if unknown:
        raise Refused(f"unknown fact ids {unknown}: cite ids from cv_master")
    if block is not None:
        foreign = [i for i in ids if m["_owner"][i] != block]
        if foreign:
            raise Refused(f"facts {foreign} do not belong to {block}: a bullet cites only facts of its own block")
    return ids


def _bullet(b: dict, block: str, d: dict, m: dict) -> dict:
    text = _text(b.get("text"))
    if not text:
        raise Refused(f"a bullet in {block} has no text")
    facts = _facts(b.get("facts"), block, m)
    if not facts:
        raise Refused(f"the bullet '{text[:60]}' cites no fact: every bullet rests on at least one fact id")
    d["next"] += 1
    return {"id": f"B{d['next']}", "text": text, "facts": facts}


def _block_id(v, m: dict) -> str:
    bid = str(v or "").strip()
    if bid not in m["_roles"] and bid not in m["_projects"]:
        raise Refused(f"unknown block {bid!r}: use a role id or a project id from cv_master")
    return bid


def _skills(groups, m: dict) -> list[dict]:
    spoken = {lang["name"].lower() for lang in m["languages"]}
    out = []
    for g in groups or []:
        label = _text(g.get("label"))
        items = [_text(i) for i in g.get("items") or [] if _text(i)]
        if any(i.lower() in spoken for i in items):
            raise Refused("spoken languages are printed from the master in their own section: "
                          "leave them out of skills")
        if label and items:
            out.append({"label": label, "items": items})
    return out


def op_write(req: dict) -> dict:
    m = load_master()
    src = req.get("draft") or {}
    target = src.get("target") or {}
    d = {
        "target": {"company": _text(target.get("company")), "title": _text(target.get("title"))},
        "jd_terms": [_text(t) for t in src.get("jd_terms") or [] if _text(t)],
        "next": 0, "flagged": {},
    }
    if not d["target"]["title"]:
        raise Refused("target.title is required: the job title from the JD")
    summary = src.get("summary") or {}
    d["summary"] = {"text": _text(summary.get("text")), "facts": _facts(summary.get("facts"), None, m)}
    if not d["summary"]["text"]:
        raise Refused("summary.text is required")
    d["blocks"] = []
    seen = set()
    for b in src.get("blocks") or []:
        bid = _block_id(b.get("of"), m)
        if bid in seen:
            raise Refused(f"block {bid} appears twice: put all its bullets in one block")
        seen.add(bid)
        d["blocks"].append({"of": bid, "bullets": [_bullet(x, bid, d, m) for x in b.get("bullets") or []]})
    d["skills"] = _skills(src.get("skills"), m)
    out = review(d, m)
    _save(req["session"], d)
    return out


def _find(d: dict, bullet_id: str) -> tuple[dict, int]:
    for blk in d["blocks"]:
        for i, b in enumerate(blk["bullets"]):
            if b["id"] == bullet_id:
                return blk, i
    raise Refused(f"no bullet {bullet_id} in this draft")


def _block(d: dict, bid: str) -> dict:
    for blk in d["blocks"]:
        if blk["of"] == bid:
            return blk
    blk = {"of": bid, "bullets": []}
    d["blocks"].append(blk)
    return blk


def _apply(op: dict, d: dict, m: dict) -> None:
    kind = op.get("op")
    if kind == "replace":
        blk, i = _find(d, str(op.get("id")))
        old = blk["bullets"][i]
        text = _text(op.get("text")) or old["text"]
        facts = _facts(op.get("facts"), blk["of"], m) if op.get("facts") else old["facts"]
        blk["bullets"][i] = {"id": old["id"], "text": text, "facts": facts}
    elif kind == "delete":
        blk, i = _find(d, str(op.get("id")))
        del blk["bullets"][i]
    elif kind == "add":
        bid = _block_id(op.get("block"), m)
        blk = _block(d, bid)
        new = _bullet(op, bid, d, m)
        after = str(op.get("after") or "")
        if after == "first":
            blk["bullets"].insert(0, new)
        elif after:
            ablk, i = _find(d, after)
            if ablk is not blk:
                raise Refused(f"{after} is not in block {bid}")
            blk["bullets"].insert(i + 1, new)
        else:
            blk["bullets"].append(new)
    elif kind == "move":
        blk, i = _find(d, str(op.get("id")))
        moving = blk["bullets"].pop(i)
        after = str(op.get("after") or "first")
        if after == "first":
            blk["bullets"].insert(0, moving)
        else:
            ablk, j = _find(d, after)
            if ablk is not blk:
                blk["bullets"].insert(i, moving)
                raise Refused(f"{after} is in another block: a bullet moves only within its block")
            ablk["bullets"].insert(j + 1, moving)
    elif kind == "summary":
        d["summary"] = {"text": _text(op.get("text")) or d["summary"]["text"],
                        "facts": _facts(op.get("facts"), None, m) if op.get("facts") else d["summary"]["facts"]}
    elif kind == "skills":
        d["skills"] = _skills(op.get("groups"), m)
    elif kind == "order":
        order = [_block_id(b, m) for b in op.get("blocks") or []]
        rank = {b: i for i, b in enumerate(order)}
        d["blocks"].sort(key=lambda blk: rank.get(blk["of"], len(rank)))
    elif kind == "drop_block":
        # Not checked against the master: a block the master no longer has must still be removable.
        bid = str(op.get("block") or "").strip()
        if bid not in [blk["of"] for blk in d["blocks"]]:
            raise Refused(f"no block {bid!r} in this draft")
        d["blocks"] = [blk for blk in d["blocks"] if blk["of"] != bid]
    else:
        raise Refused(f"unknown op {kind!r}")


def op_edit(req: dict) -> dict:
    m = load_master()
    d = _load(req["session"])
    ops = req.get("ops") or []
    if not ops:
        raise Refused("no ops given")
    for op in ops:
        _apply(op, d, m)
    out = review(d, m)
    _save(req["session"], d)
    return out


# ---------------------------------------------------------------- review

def _project_parts(p: dict) -> tuple[str, str]:
    """A project's name, and what is printed after it: the client, or the kind of project."""
    if p.get("role"):
        client = p.get("client") or ""
        return p["name"], (f"for {client}" if client and "internal" not in client else "")
    kind = {"capstone": "Capstone project", "personal": "Personal project"}.get(p.get("kind"), "")
    return p["name"], kind


def _project_name(p: dict) -> str:
    name, after = _project_parts(p)
    return f"{name}, {after}" if after else name


def _heading(bid: str, m: dict) -> str:
    if bid in m["_roles"]:
        return m["_roles"][bid]["company"]
    return _project_name(m["_projects"][bid])


def _placed(d: dict, m: dict) -> tuple[list, list]:
    """The draft's blocks in print order: roles newest first with their projects, then other projects."""
    by_id = {blk["of"]: blk for blk in d["blocks"]}
    order = [blk["of"] for blk in d["blocks"]]
    roles = []
    for r in m["roles"]:
        projects = [by_id[b] for b in order if b in m["_projects"] and m["_projects"][b].get("role") == r["id"]]
        roles.append((r, by_id.get(r["id"], {"of": r["id"], "bullets": []}), projects))
    others = [by_id[b] for b in order if b in m["_projects"] and not m["_projects"][b].get("role")]
    return roles, others


def cv_text(d: dict, m: dict) -> str:
    lines = [f"Target: {d['target']['title']}" + (f" at {d['target']['company']}" if d["target"]["company"] else ""),
             "", f"SUMMARY: {d['summary']['text']}", "", "EXPERIENCE"]
    roles, others = _placed(d, m)

    def bullets(blk):
        for b in blk["bullets"]:
            lines.append(f"  {b['id']}: {b['text']}  [{', '.join(b['facts'])}]")

    for r, own, projects in roles:
        titles = " / ".join(t["title"] for t in reversed(r["titles"]))
        lines.append(f"- {r['company']}: {titles}")
        bullets(own)
        for blk in projects:
            lines.append(f" * {blk['of']}: {_heading(blk['of'], m)}")
            bullets(blk)
    if others:
        lines.append("PROJECTS")
        for blk in others:
            lines.append(f" * {blk['of']}: {_heading(blk['of'], m)}")
            bullets(blk)
    lines.append("SKILLS")
    for g in d["skills"]:
        lines.append(f"  {g['label']}: {', '.join(g['items'])}")
    return "\n".join(lines)


def _stale(d: dict, m: dict) -> list[str]:
    """What the draft cites that the master no longer has. The user edits the master between turns."""
    out = []
    gone = [f for f in d["summary"]["facts"] if f not in m["_facts"]]
    if gone:
        out.append(f"summary: cites facts no longer in the master {gone}. Rewrite it with cv_edit (summary, with facts).")
    for blk in d["blocks"]:
        if blk["of"] not in m["_roles"] and blk["of"] not in m["_projects"]:
            out.append(f"block {blk['of']}: no longer in the master. Remove it with cv_edit (drop_block).")
            continue
        for b in blk["bullets"]:
            gone = [f for f in b["facts"] if f not in m["_facts"]]
            if gone:
                out.append(f"{b['id']}: cites facts no longer in the master {gone}. The user removed them: "
                           "rewrite it from facts that are left (replace, with facts) or delete it.")
    return out


def _sources(ids: list[str], m: dict) -> list[str]:
    return [m["_facts"][f] for f in ids if f in m["_facts"]]


def review(d: dict, m: dict) -> dict:
    notes = _stale(d, m)
    flagged = d.setdefault("flagged", {})
    years = [float(years_of_experience(m))]

    summary_nums = checks.unsupported_numbers(d["summary"]["text"], _sources(d["summary"]["facts"], m), years)
    if summary_nums:
        notes.append(f"summary: numbers {summary_nums} are not in the facts it cites.")
    for s in checks.style(d["summary"]["text"]):
        notes.append(f"summary: {s}.")

    for blk in d["blocks"]:
        if blk["of"] not in m["_roles"] and blk["of"] not in m["_projects"]:
            continue
        context = _heading(blk["of"], m)
        for b in blk["bullets"]:
            sources = _sources(b["facts"], m)
            nums = checks.unsupported_numbers(b["text"], sources)
            if nums:
                if b["id"] in flagged and flagged[b["id"]] != b["text"]:
                    notes.append(f"{b['id']}: numbers {nums} are still not in its facts after a rewrite. "
                                 "Leave it; name it to the user in your reply.")
                else:
                    flagged.setdefault(b["id"], b["text"])
                    notes.append(f"{b['id']}: numbers {nums} are not in the facts it cites. "
                                 "Rewrite it once with cv_edit, using only the facts' numbers.")
            names = checks.unsupported_names(b["text"], sources + [context])
            if names:
                notes.append(f"{b['id']}: names {names} are not in its facts (fine if it is the same thing in other words).")
            for s in checks.style(b["text"]):
                notes.append(f"{b['id']}: {s}.")

    items = [i for g in d["skills"] for i in g["items"]]
    unknown = checks.skill_gaps(items, m["skills"])
    if unknown:
        notes.append(f"skills: {unknown} are not in the master's skills.")

    roles, _ = _placed(d, m)
    empty = [r["company"] for r, own, projects in roles if not own["bullets"] and not any(p["bullets"] for p in projects)]
    if empty:
        notes.append(f"roles with no bullets (printed with title and dates only): {empty}.")

    return {"cv": cv_text(d, m), "checks": notes,
            "jd_terms_missing": checks.jd_gaps(d["jd_terms"], _printed(d, m))}


def _printed(d: dict, m: dict) -> str:
    """Every word the CV prints, for the JD-term check."""
    parts = [d["summary"]["text"]]
    for r in m["roles"]:
        parts += [r["company"]] + [t["title"] for t in r["titles"]]
    for blk in d["blocks"]:
        if blk["of"] not in m["_roles"] and blk["of"] not in m["_projects"]:
            continue
        parts.append(_heading(blk["of"], m))
        parts += [b["text"] for b in blk["bullets"]]
    parts += [g["label"] + " " + " ".join(g["items"]) for g in d["skills"]]
    parts += [e["degree"] for e in m["education"]]
    parts += [lang["name"] for lang in m["languages"]]
    return "\n".join(parts)


# ---------------------------------------------------------------- render

def _month(v) -> str:
    ym = _ym(v)
    return f"{_MONTHS[ym[1] - 1]} {ym[0]}" if ym else "Present"


def _span(a, b) -> str:
    return f"{_month(a)} – {_month(b)}"


def _project_out(p: dict, blk: dict) -> dict:
    name, after = _project_parts(p)
    return {"heading": name, "after": f", {after}" if after else "", "bullets": [b["text"] for b in blk["bullets"]]}


def context(d: dict, m: dict) -> dict:
    c = m["contact"]
    # A link prints without its scheme: shorter, and Word still makes it clickable.
    links = [re.sub(r"^https?://(www\.)?", "", u) for u in (c.get("linkedin"), c.get("github")) if u]
    contact = [c.get("location"), c.get("phone"), c.get("email"), *links]
    roles_out = []
    roles, others = _placed(d, m)
    for r, own, projects in roles:
        titles = list(reversed(r["titles"]))
        roles_out.append({
            "company": r["company"], "location": r.get("location") or "",
            "dates": _span(r["from"], r.get("to")),
            "titles": [{"title": t["title"], "dates": _span(t["from"], t.get("to"))} for t in titles],
            "bullets": [b["text"] for b in own["bullets"]],
            "projects": [_project_out(m["_projects"][blk["of"]], blk) for blk in projects if blk["bullets"]],
        })
    projects_out = []
    for blk in others:
        if not blk["bullets"]:
            continue
        p = m["_projects"][blk["of"]]
        projects_out.append({**_project_out(p, blk), "dates": _span(p["from"], p.get("to")),
                             "url": p.get("url") or ""})
    education = [{"degree": e["degree"], "school": e["school"], "dates": _span(e["from"], e.get("to")),
                  "gpa": f", GPA {e['gpa']}" if e.get("gpa") else ""} for e in m["education"]]
    return {
        "name": c["name"], "contact": " | ".join(x for x in contact if x),
        "summary": d["summary"]["text"], "roles": roles_out, "projects": projects_out,
        "education": education,
        "skills": [{"label": g["label"], "names": ", ".join(g["items"])} for g in d["skills"]],
        "languages": ", ".join(f"{lang['name']} ({lang['level']})" for lang in m["languages"]),
        "right_to_work": m.get("right_to_work") or "", "notice_period": m.get("notice_period") or "",
    }


def _file_stem(d: dict, m: dict) -> str:
    who = re.sub(r"[^A-Za-z0-9]+", "_", m["contact"]["name"]).strip("_")
    target = d["target"]["company"] or d["target"]["title"]
    what = re.sub(r"[^A-Za-z0-9]+", "_", target).strip("_")[:40]
    return f"{who}_CV" + (f"_{what}" if what else "")


def _soffice() -> str:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if not found:
        raise Refused("LibreOffice (soffice) is not installed, so no PDF can be made")
    return found


def op_render(req: dict) -> dict:
    from docxtpl import DocxTemplate
    from pypdf import PdfReader

    m = load_master()
    d = _load(req["session"])
    stale = _stale(d, m)
    if stale:
        raise Refused("the draft cites what the master no longer has; fix it with cv_edit first: " + " ".join(stale))
    if not TEMPLATE.exists():
        raise Refused(f"the template {TEMPLATE.name} is missing")
    stem = _file_stem(d, m)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        docx = work / f"{stem}.docx"
        tpl = DocxTemplate(str(TEMPLATE))
        tpl.render(context(d, m), autoescape=True)
        tpl.save(str(docx))
        profile = HERMES / "cv" / "lo-profile"
        proc = subprocess.run(
            [_soffice(), f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
             "--convert-to", "pdf", "--outdir", str(work), str(docx)],
            capture_output=True, text=True, timeout=180,
        )
        pdf = work / f"{stem}.pdf"
        if not pdf.exists():
            raise Refused(f"the PDF step failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
        pages = len(PdfReader(str(pdf)).pages)
        box = OUTBOX / req["session"]
        box.mkdir(parents=True, exist_ok=True)
        # Only the latest render waits for delivery; an earlier one this turn is superseded.
        for old in box.iterdir():
            if old.is_file():
                old.unlink()
        for f in (docx, pdf):
            shutil.move(str(f), box / f.name)

    flagged = []
    for blk in d["blocks"]:
        for b in blk["bullets"]:
            nums = checks.unsupported_numbers(b["text"], _sources(b["facts"], m))
            if nums:
                flagged.append(f"{b['id']}: {nums}")
    return {"files": [f"{stem}.docx", f"{stem}.pdf"], "pages": pages,
            "pages_max": PAGES_MAX, "flagged": flagged}


OPS = {"master": op_master, "write": op_write, "edit": op_edit, "render": op_render}


def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    op = OPS.get(req.get("op"))
    if op is None:
        out = {"error": f"unknown op {req.get('op')!r}"}
    else:
        try:
            out = op(req)
        except Refused as exc:
            out = {"error": str(exc)}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
