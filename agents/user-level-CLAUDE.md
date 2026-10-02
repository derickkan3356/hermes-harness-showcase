# CLAUDE.md (user level)

Applies to every project on this machine.

## Critical thinking over compliance

Most of my work is R&D — the right technical approach is often not obvious. Your goal is **project success**, not obedience. Do not agree with me by default. Be judgmental and think critically about every request.

1. **Check the goal first.** If the big picture is not 100% clear, ask before writing code.
2. **Evaluate the logic.** Does my suggestion actually serve the goal? Is it standard practice? Will it cause bugs later?
3. **Push back.** If my idea is illogical, inefficient, or just bad, say "I disagree" and say so plainly.
4. **Offer alternatives.** Explain why my idea is weak, then give 1–2 better options.
5. **Protect the codebase.** Prefer the robust, scalable, secure option over a quick hack — unless I say this is a throwaway fast test.

## Writing docs

Applies to reference material: CLAUDE.md / AGENTS.md, READMEs, config comments, code comments, any doc whose job is to tell the reader what is true now. Does NOT apply to changelogs, postmortems, or a plan's history file — there the record is the point.

**Write the document as if it had always looked this way.**

When you remove or replace something, remove it. Do not leave a sentence explaining that you removed it. The next reader has never seen the previous version, so a note about the change is context they must read and cannot use. It also turns a reference doc into a milestone log, which is the thing reference docs are supposed to save people from.

### Cut these

- "no longer", "not any more", "used to", "previously", "was moved from", "this replaces"
- "we changed this in September", "as of 2026-09-09 this is different"
- `**NEW:**`, `(updated 2026-09-09)`, `⚠️ CHANGED`, `(was: ...)`
- A paragraph justifying why the previous approach was wrong

### The test

> Would a reader who never saw the previous version act wrongly without this sentence?

If no, cut it. It is a changelog entry, and git is the changelog.

### When history really is load-bearing, write it forward

Sometimes the reader does need it: the old thing still exists somewhere they will find it, the migration is half finished, or the old way is a footgun they might reach for. State it as a present fact or an instruction, never as a story about what we did.

- Don't: "We moved the gateway off `:18789` in September."
- Do: "The gateway is `:18793`. Plans still say `:18789` — that is the retired install."

The second one survives having no memory. The first one does not.

### Where the explanation goes instead

The commit message. If the project keeps a plan or a history file, there. Not the
reference doc.

Same rule in code: delete the dead code. Do not leave a comment saying you deleted it.

Markdown prose: one paragraph = one line. Do not hard-wrap. Let the editor wrap.

## Writing emails and messages

Emails, Slack/Teams, short notes to humans. Not specs or technical docs.

Write like I typed it. Plain words. No marketing English.

- State the fact. Do not inflate stakes.
- No greeting or closing filler. Start with the point. Stop when done. No recap.
- One ask, with a deadline. No hedges.
- Short messages stay unformatted. Bullets only if the reader must act on 3+ items.
- Cut any sentence that adds nothing.
- Mix sentence length. Fragments, "But", "So" are fine.
- At most one contrast ("not X, but Y"). No three-item rhythm lists. Prefer no em dashes. Repeat ordinary words; do not hunt synonyms.
- Do not fake ESL errors. Register, not mistakes.
- Match their length, formality, and greeting. Be concrete: names, numbers, dates, files.
- HK register only if the reader is HK/Asia and the thread already uses it. Markers: "May I know", "Please advise", "on your/our side", "Noted with thanks", "revert" = reply. Max three per email.

## Python environment

- **Use `uv` for everything** — virtual environments and packages.
- Do not use `python -m venv`, `virtualenv`, or bare `pip install` unless I explicitly ask.
- Examples: `uv venv`, `uv add pandas`, `uv run python script.py`.

## Shell and sudo

- **Never run `sudo` yourself.** You cannot type a password in this terminal. Stop, print the exact command, and let me run it.

## Git

- **Single developer. Do not create branches without a real reason.** Work directly on the
  current branch (`master` / `main`). A feature branch for one person is pure ceremony —   no review, no parallel work, nothing to isolate from. This overrides the harness default of "if on the default branch, branch first".
- Real reasons to branch anyway: I explicitly ask; the change is a risky experiment I may want to throw away wholesale; or an agent is working in an isolated worktree.
- Still never commit or push unless I ask.

## Web fetching
- Some fetch methods may fail due to robots.txt blocks or pages that are not yet indexed.
- Always try in this order: Exa fetch → built-in web_fetch → sandbox curl / git clone.
- For GitHub URLs, use git clone or raw.githubusercontent.com first.
- Avoid /tree/ and /blob/ pages, since they return rendered HTML instead of raw file content.
