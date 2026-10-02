status: done

# CV per job — a tailored CV from a master of facts, PDF and Word, reviewed and edited in one Open WebUI chat

The user keeps one master CV of facts. For a job they want to apply for, they ask Hermes in Open WebUI; the agent reads the JD (pasted, or a job digest entry through `job_read`), picks the facts that fit, orders them, and writes them in the JD's terms. The goal is passing HR screening: ATS (applicant tracking system, the software that filters CVs by keyword) and the human skim after it. The user reviews and asks for edits in the same chat; nothing carries across chats.

## Design

How a CV is made (layers, what code prints and what the agent writes, checks, edits, render): `docs/cv.md`. How the files reach the chat: `docs/open-webui.md` (Files from tools).

## Hot

- Biggest unknown: whether the local model's choice and phrasing of facts pass the user's read. Nothing measures it until the eval.
- [x] Probe the attachment path. A throwaway `probe_file` tool and a `hermes_probe` Pipe delivered `probe.docx` onto the assistant message on laptop and phone; preview and download both worked, and the file opened (2026-10-02, `journalctl --user -u open-webui` lines `[hermes_probe] attached`). Probe plugin, Pipe and config lines removed.
- [x] Master schema and a filled draft: `cv/master.yaml` from the old resume and past project plans (82 facts, ids unique, parses with PyYAML, 2026-10-02).
- [x] The user confirmed every open item in `cv/master.yaml` (roles, dates, client names never printed, contact); `grep -c ASK cv/master.yaml` prints 0 (2026-10-02).
- [x] LibreOffice for the PDF step: `libreoffice-writer-nogui` 24.2.7 and `fonts-crosextra-carlito` (metric-compatible with Calibri) are installed; `soffice --headless --convert-to pdf` turned a python-docx test file into a PDF whose text pypdf reads back exactly (2026-10-02). `pdftotext` is not installed; pypdf is enough.
- [x] Plugin `cv` (`cv_master`, `cv_write`, `cv_edit`, `cv_render`), skill `cv-tailor`, a starter `cv/template.docx` from `cv/make_template.py`, outbox delivery in the Pipe; `cv` on `api_server` (`tools/facts.sh tools` lists the four `cv_*` tools, 2026-10-02). Handlers run through the Hermes venv: write, edit, the fact-ownership refusal, render to a 1-page PDF in the outbox.
- [x] One CV from a real JD in a new Open WebUI chat on the laptop: a CTgoodjobs link read with `web_extract`, then `skill_view` cv-tailor, `cv_master`, `cv_write`, `cv_edit`, `cv_render`; both files on the reply and opened (2026-10-02, `journalctl --user -u open-webui` lines `[hermes_session] attached`, session `owui-26eaf051-…`). Seen there: a bullet turned the fact's "more than 20" into "about 20", which the number check cannot see; the skill now says to keep qualifiers.
- [x] Phone: the files previewed and downloaded; an edit asked in the same chat changed B1 only ("about 20" → "more than 20"); the draft JSON before and after differs in that bullet alone, order, summary and skills unchanged (2026-10-02, 4 `[hermes_session] attached` lines).
- [x] Template look: the user compared three looks rendered from one draft (Jake's Resume, Harvard, modern sans) and chose the modern layout in Cambria: navy headings over a rule, grey dates, the client after a project name in light weight (`cv/make_template.py`, 2026-10-02). The PDF embeds Caladea, Cambria's metric twin (`docs/cv.md`).
- [x] Eval, smaller than planned: four real JDs (one through the API server, three through Open WebUI), the user's read OK on each. Every draft re-reviewed against the current master (2026-10-02): no number outside its facts, no banned word, no em dash in a bullet; every render 2 pages. One draft listed JD terms as sentences, so 21 of 24 showed as missing; the tool description and the skill now ask for one to four words, not re-measured. Two drafts cited `office-ai.users` after the user removed it, which crashed review; a draft citing a removed fact, project or role is now reported and refused by render (`docs/cv.md`).
- Done when: from an Open WebUI chat on the phone, a real JD gives a PDF and a Word file that download there; no number in either is missing from the master; an edit asked in the same chat changes only what was asked; and the eval JDs pass the checks above.
