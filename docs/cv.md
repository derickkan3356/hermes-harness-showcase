# CV per job

The `cv` plugin turns the master CV of facts (`cv/master.yaml`) into a CV for one job, as a Word file and a PDF attached to the Open WebUI reply. The agent's procedure and writing rules are the `cv-tailor` skill; how the files reach the chat is `docs/open-webui.md` (Files from tools).

## How a CV is made

Three layers. The **master** (`cv/master.yaml`, written by the user) holds every true fact: roles, projects, and per project facts with ids, each one specific claim (numbers, tools, scale), not a CV sentence; optional `seeds` per project are the user's own phrasing for an angle, or a note on how to frame it. The **draft** (JSON, one per chat, written by the agent through the tools) holds the target job, the JD's key terms, the summary, and blocks of bullets, each bullet with an id and the fact ids it rests on. The **template** (`cv/template.docx`) is the look.

The line between code and agent is `CLAUDE.md` (Decisions, last item). Code prints what must not be paraphrased: company, official titles, dates, education, contact, languages, right to work, notice period, and the file name. Every role is printed, newest first, with or without bullets: a missing job reads as a gap. A bullet cites only facts of its own block. Spoken languages never go in skills. The agent chooses the facts, their order, and the words. The summary describes the user by what the facts show, not by the target job's title: the user does not hold that title, and asked to carry it, the agent wrote "Targeting the Senior AI Engineer role, an AI engineer …".

`cv_write` and `cv_edit` return plain statements, never errors, and none asks for a reply:

- a number in a bullet that is not in the facts it cites: rewrite once; if a rewrite still has one, the agent names the bullet to the user. Nothing is dropped.
- a name (tool, product) not in the cited facts: a notice, since other words for the same thing are fine. Each part of a joined name (`PostgreSQL/pgvector`, `second-LLM`) and a plural (`APIs`) count as found when the facts have them.
- JD terms (one to four words each, the JD's own wording) that do not appear in the CV text, case and hyphens normalised: the agent uses the JD's word where a fact supports it, and tells the user the rest as gaps. The terms must be short: when an agent listed sentences, 21 of 24 showed as missing. With short terms, a GenAI engineer ad had 5 of 23 missing; a bank's GenAI architect ad had 17 of 22 missing, all tools and methods the master does not have (Kubernetes, LangChain, vLLM, LoRA). The count is a fit signal as much as a wording one.
- banned marketing words, em dashes, first person, skills not in the master.

A qualifier the agent drops ("more than 20" written as "about 20") passes the number check; the skill's writing rules are what guard it.

`cv_render` makes the `.docx` with docxtpl, converts it to PDF with LibreOffice so both always match, and counts pages: two at most. All six CVs made while building this came to two pages with 11 to 14 bullets; a one-page aim would need about eight. A draft that cites a fact, project or role the user has since removed from the master is reported by every check and refused by `cv_render` until the agent rewrites or drops those bullets.

Edits go by bullet id through `cv_edit`, so what the user did not mention stays as it was. One chat per application, not inside a job digest chat: a 20-entry `job_candidates` result alone fills tens of thousands of characters.

## What the checks cannot see

The checks catch what a fixed rule can: numbers, names, terms, words. They cannot judge the choice: whether the most relevant facts lead, whether a relevant fact was left out, whether a qualifier was softened. That is read by the user. Six CVs for AI engineer and AI architect ads were read and all OK; other kinds of job (data scientist, a non-technical role) have not been tried.

Nothing re-reads it on its own, so a weaker model passes every check unnoticed. When the model, its quantization or the `cv-tailor` skill changes, make CVs again for a few JDs already used and read them against the earlier drafts: the drafts stay in `~/.hermes/cv/drafts/`, and a digest JD stays in the job ledger (`job_read`).

## Files

| | |
| --- | --- |
| `cv/master.yaml` | Every true fact, written by the user. The only source of what a CV says. |
| `cv/make_template.py` | Writes `cv/template.docx`: Cambria, navy headings over a rule, grey dates. |
| `cv/template.docx` | The look the worker fills (docxtpl tags). |
| `cv/fonts/` | Caladea, for the PDF step (below). OFL licensed. |
| `plugins/cv/workers/cv.py` | Draft, checks, render. Its own `uv` environment, pinned by `cv.py.lock`. |
| `~/.hermes/cv/drafts/<session>.json` | One draft per chat. Runtime, not tracked. |
| `~/.hermes/outbox/<session>/` | Rendered files waiting for the Pipe; `sent/` once attached. Runtime, not tracked. |

## Template

```fish
uv run --script cv/make_template.py            # into cv/template.docx
uv run --script cv/make_template.py out.docx   # somewhere else, to compare
```

Rerunning overwrites a template restyled by hand in Word. Every `{{ }}` and `{%p %}` tag must survive a hand edit, each `{%p %}` in a paragraph of its own.

Single column, real heading styles, no text boxes, no layout tables: ATS software reads the file in order, and an agency can paste it into their own template.

## Setup

LibreOffice makes the PDF, headless, with only its Writer part:

```fish
sudo apt install -y libreoffice-writer-nogui
```

The plugin is linked into Hermes like the others (`ln -s ~/projects/hermes-harness/plugins/cv ~/.hermes/plugins/cv`), enabled under `plugins.enabled`, and its toolset `cv` is on `platform_toolsets.api_server`. The worker's Python packages come from `cv.py.lock` on first run.

## Fonts

The `.docx` is opened on the recruiter's machine, so it names Cambria, which Word has on Windows and macOS. The PDF is made here by LibreOffice, which substitutes Caladea, a metric-compatible twin: same widths, so the same line breaks and page count. Caladea is in `cv/fonts/`, linked into the user font directory, which needs no sudo:

```fish
ln -sfn ~/projects/hermes-harness/cv/fonts ~/.local/share/fonts/hermes-cv
fc-cache -f ~/.local/share/fonts
```

Check that a rendered PDF used the twin and not a fallback: its embedded fonts (PyMuPDF `page.get_fonts()`) name Caladea. A fallback such as DejaVu changes line breaks, so the page count the agent is told would be wrong.
