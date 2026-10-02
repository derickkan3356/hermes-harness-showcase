"""The cv tools and the call into the worker.

Everything is decided in `workers/cv.py`, in its own `uv` environment. This
file validates arguments, adds the chat's session id, runs the worker, and
words each result so the model does the right thing with it.

The draft is keyed on the root of the Hermes session's lineage, which for Open
WebUI is the Pipe's `owui-<chat id>`: one draft per chat, and a new chat starts
empty. Compression can move a turn to a child session; the tools still see the
chat's draft, and the files still land where the Pipe looks. The model never
names a draft, a path or a file.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from tools.registry import tool_error, tool_result

WORKERS = Path(__file__).resolve().parent / "workers"
BUDGET = 240.0

_BULLETS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The bullet as printed. Opens with a past-tense verb; no 'I'."},
            "facts": {"type": "array", "items": {"type": "string"},
                      "description": "Fact ids from cv_master that this bullet rests on, all from this block."},
        },
        "required": ["text", "facts"],
    },
}

_SKILLS = {
    "type": "array",
    "description": "Skill lines, most relevant first: a label and the master's skills under it that the JD asks for.",
    "items": {
        "type": "object",
        "properties": {"label": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
        "required": ["label", "items"],
    },
}

CV_MASTER_SCHEMA = {
    "name": "cv_master",
    "description": (
        "The user's master CV: every true fact, by id, under its role or project, plus "
        "skills, education, languages, right to work, notice period, years of experience, "
        "and `seeds`, the user's own phrasing or framing for a project, where they gave it. Read it before writing "
        "a CV draft. A CV states only what is here."
    ),
    "parameters": {"type": "object", "properties": {}},
}

CV_WRITE_SCHEMA = {
    "name": "cv_write",
    "description": (
        "Write this chat's CV draft for one job, replacing any draft before it. You choose "
        "the facts, order them and phrase them; company, titles, dates, education, contact, "
        "languages and right to work are printed from the master. Every role is printed, "
        "newest first. A block is a project (or a role, for its own facts); its bullets cite "
        "only that block's facts. Returns the draft as text with bullet ids (B1, B2, …), "
        "`checks` and `jd_terms_missing`. Then call cv_render."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "The hiring company as the JD names it; empty if unnamed (an agency ad)."},
                    "title": {"type": "string", "description": "The job title from the JD."},
                },
                "required": ["title"],
            },
            "jd_terms": {"type": "array", "items": {"type": "string"},
                         "description": ("The JD's key terms, each one to four words exactly as the JD writes them: "
                                         "skills, tools, domains (e.g. 'RAG', 'vector databases', 'LLM APIs'). "
                                         "Not sentences. 10 to 25.")},
            "summary": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Two or three sentences, from the cited facts."},
                    "facts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "facts"],
            },
            "blocks": {
                "type": "array",
                "description": "Projects (and roles) in the order you want them, most relevant first within each role.",
                "items": {
                    "type": "object",
                    "properties": {
                        "of": {"type": "string", "description": "A project id or role id from cv_master."},
                        "bullets": _BULLETS,
                    },
                    "required": ["of", "bullets"],
                },
            },
            "skills": _SKILLS,
        },
        "required": ["target", "jd_terms", "summary", "blocks", "skills"],
    },
}

CV_EDIT_SCHEMA = {
    "name": "cv_edit",
    "description": (
        "Change this chat's CV draft by id. Anything not named stays exactly as it is. Ops: "
        "`replace` (id, text, and facts if they change), `delete` (id), `add` (block, text, facts, "
        "after: a bullet id or 'first'; default last), `move` (id, after: a bullet id in the same "
        "block or 'first'), `summary` (text, facts), `skills` (groups: the whole skills list), "
        "`order` (blocks: block ids in the new order), `drop_block` (block). Returns the draft "
        "and its checks, as cv_write does. Then call cv_render."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["replace", "delete", "add", "move", "summary",
                                                          "skills", "order", "drop_block"]},
                        "id": {"type": "string", "description": "A bullet id, e.g. B4."},
                        "block": {"type": "string"},
                        "after": {"type": "string"},
                        "text": {"type": "string"},
                        "facts": {"type": "array", "items": {"type": "string"}},
                        "groups": _SKILLS,
                        "blocks": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["op"],
                },
            },
        },
        "required": ["ops"],
    },
}

CV_RENDER_SCHEMA = {
    "name": "cv_render",
    "description": (
        "Make this chat's CV draft into a Word file and a PDF, and attach both to your reply. "
        "Returns the file names and the page count."
    ),
    "parameters": {"type": "object", "properties": {}},
}


def _uv() -> Optional[str]:
    """Absolute path to uv. Hermes may run under a PATH without `~/.local/bin`."""
    override = os.environ.get("CV_UV", "").strip()
    if override:
        return override if Path(override).exists() else None
    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uv"
    return str(fallback) if fallback.exists() else None


def worker_available() -> bool:
    return (WORKERS / "cv.py").exists() and _uv() is not None


def _run(request: Dict[str, Any]) -> Dict[str, Any]:
    uv = _uv()
    if uv is None:
        return {"error": "uv not found — set CV_UV to its path"}
    try:
        proc = subprocess.run(
            [uv, "run", "--script", "--no-project", "--locked", "-q", str(WORKERS / "cv.py")],
            input=json.dumps(request), capture_output=True, text=True, timeout=BUDGET, cwd=str(WORKERS),
        )
    except subprocess.TimeoutExpired:
        return {"error": f"the cv worker timed out after {BUDGET:.0f}s"}
    if proc.returncode != 0:
        return {"error": f"the cv worker failed: {(proc.stderr or '').strip()[-400:] or proc.returncode}"}
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        return {"error": f"the cv worker returned unreadable output: {exc}"}


def _session(kw: dict) -> str:
    """The first session of this one's lineage: the id the Pipe sends for the chat."""
    current = str(kw.get("session_id") or "").strip()
    db = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "state.db"
    if not current or not db.exists():
        return current
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            seen = set()
            while current not in seen and len(seen) < 100:
                seen.add(current)
                row = conn.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
                if not row or not row[0]:
                    break
                current = row[0]
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return current


def handle_cv_master(args: dict, **kw) -> str:
    out = _run({"op": "master"})
    if "error" in out:
        return tool_error(out["error"])
    return tool_result(out)


def _reviewed(out: Dict[str, Any]) -> str:
    if "error" in out:
        return tool_error(f"{out['error']}. Nothing was changed.")
    out["note"] = (
        "`checks` are statements, not errors. Rewrite a bullet once when its numbers are not in "
        "its facts; the rest is your call. Then call cv_render."
    )
    return tool_result(out)


def handle_cv_write(args: dict, **kw) -> str:
    session = _session(kw)
    if not session:
        return tool_error("CV drafts need a chat session; there is none here.")
    draft = {k: args.get(k) for k in ("target", "jd_terms", "summary", "blocks", "skills")}
    return _reviewed(_run({"op": "write", "session": session, "draft": draft}))


def handle_cv_edit(args: dict, **kw) -> str:
    session = _session(kw)
    if not session:
        return tool_error("CV drafts need a chat session; there is none here.")
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        return tool_error("ops must be a non-empty list")
    return _reviewed(_run({"op": "edit", "session": session, "ops": ops}))


def handle_cv_render(args: dict, **kw) -> str:
    session = _session(kw)
    if not session:
        return tool_error("CV files need a chat session; there is none here.")
    out = _run({"op": "render", "session": session})
    if "error" in out:
        return tool_error(f"{out['error']}. No file was made: tell the user.")
    notes = ["Both files are attached to your reply after this turn. Do not write a path or a link to them."]
    if out["pages"] > out["pages_max"]:
        notes.append(f"{out['pages']} pages is too long: cut the least relevant bullets with cv_edit and render again.")
    if out["flagged"]:
        notes.append("These bullets have numbers that are not in their facts: name them to the user so they can check.")
    out["note"] = " ".join(notes)
    return tool_result(out)
