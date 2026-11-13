# hermes-harness

Personal AI agent: Hermes Agent (Nous Research) driving a local model on a 4090, reachable from laptop and phone. Primary job: web search, scraping, and fetching.

This file is the operating contract — only what every agent turn needs to not act wrongly. How-to and measurements: `docs/`. Check `git log`; the other agent may have moved.

Live numbers come from `tools/facts.sh`, not from this file or a plan. Run it when the task needs current state. Do not paste its output into markdown.

## Topology

```
Windows 11 host "gpu-desktop-1"        WSL2 "gpu-desktop-1-wsl"   ← all dev happens here, this repo
  RTX 4090                               Hermes gateway, API server 127.0.0.1:8642
  LM Studio :1234  ←────────────────────   ↑
                                         Open WebUI 127.0.0.1:8080
                                           ↑ Tailscale Serve, HTTPS :443
                    tailnet example-tailnet.ts.net
                              ↑
                     laptop / phone (browser, home-screen app)
```

WSL is its own node on the tailnet. No Windows `netsh portproxy`. Only Open WebUI is on the tailnet, through Serve (not Funnel); the API server and Open WebUI listen on loopback.

LM Studio is on the Windows host, port 1234, path `/v1`. Use that host's **Tailscale** IP (`tools/facts.sh net`). Do not use `172.20.176.1` (the WSL NAT gateway): it works, but that address changes on every reboot.

## Decisions

- **Local model only.** The agent's LLM is the 4090 through LM Studio. No cloud API model, not even as a fallback when the local one is slow, weak, or down — a task the local model cannot do is a task this harness does not do yet. Non-LLM cloud services (Brave Search) are unaffected.
- **Hermes, not OpenClaw.** Python, `uv`, and 16–24 GB VRAM as a documented local target.
- **Open WebUI first.** It is the chat on laptop and phone: in WSL, behind Tailscale Serve, talking to the Hermes API server through the `hermes_session` Pipe, one Hermes session per chat. Scheduled posts are Open WebUI Automations, not Hermes cron — one scheduler. Add a messaging platform (Telegram, Slack) only if Open WebUI is not enough. How it maps onto Hermes sessions, and its limits: `docs/open-webui.md`.
- **Brave Search API for `web_search`.** `ddgs` fails on about half the calls from the home line. Scrappa results were off-topic. If Brave's monthly free credit runs out, pay Brave or eval a new provider. Measurements: `docs/web-search.md`.
- **One fetch layer, many use cases on top.** `web_extract` (the `fetch-cascade` plugin) is the generic and ad-hoc way to read a page, and every use case — a news digest, job ads, forums — is a skill built on it, never its own fetcher. A wall, a render, a PDF, a timeout are the same problem whatever the goal, and three copies of that code means fixing every bug three times. The boundary: **getting the bytes is shared, deciding what to go and get is not.** Transport and extraction live in the cascade; which URLs to visit, how to paginate, and what to do with the result live in the skill. A wanted change in the shape of the extracted text is a parameter on the shared layer, not a fork of it. Structure and measurements: `docs/web-fetch.md`.
- **The Open WebUI path gets data tools, not code.** `platform_toolsets.api_server` is `[web, image_gen, skills, memory, todo, session_search, news, jobs, cv]` — no shell, no file tools, no browser, no code execution, no cron, no delegation (`tools/facts.sh tools` resolves what each platform actually gets). It lists `web` and `image_gen` rather than the `safe` composite that carries them, because `safe` also carries `vision` and the vision projector is not loaded. That path takes requests from a browser and text from pages anyone can write, with nobody present to answer an approval prompt, so the capability is absent rather than gated. A new thing the agent must *do* there is a plugin tool with typed arguments, the way `fetch-cascade`, `news-ledger`, `job-ledger` and `cv` are — never the toolset back. A skill is instructions over existing tools and grants nothing new. Shell stays on `cli`, where a human answers the prompt. Why the boundary is the toolset and not the command: `docs/security.md`.
- **No per-site special cases in the fetch layer.** A site with its own feed or API — the Observatory's forecast is the example, `docs/web-fetch.md` (Look for the endpoint) — is handled by the skill that wants it, or by a site-adapter table the skills share. The cascade stays a general reader, or it becomes a pile of site hacks that no one dares change.
- **Code does what a fixed rule does reliably; judgement is the agent's, and code feeds it clues.** The test for any decision: can a fixed rule get it right every time from the data it has? If yes, it is code, and the agent is not asked. If it takes a guess or a heuristic, it is the agent's, and code's job is to hand it the facts that make the call easy. Code must not quietly make such a call before the agent sees the item: a heuristic that drops or hides something on a guess removes it from the agent's judgement without anyone noticing. When the agent judges badly, give it better clues; do not replace the judgement with a heuristic. If better clues do not fix it, the task is one this harness cannot do yet.

## Plans, docs, this file

`tools/facts.sh` is live state. This file is the contract. `docs/` is how-to. Plans are the current workstream. Do not copy between them.

**This file.** Only a constraint that would make an agent act wrongly if missing. Not how-to, not measurements, not live numbers, not a duplicate of a user rule. Reversing a Decision here: ask me first.

**`docs/`.** How, measurements, technical why. Read when the task needs it. Working LM Studio recipe: `docs/lm-studio.md`. Search providers and how to test them: `docs/web-search.md`. What the agent can and cannot do on each path, and why: `docs/security.md`. Changing one: patch the sentences that changed. A rewritten section drops the facts nobody noticed were load-bearing.

**Plans.** `.cursor/plans/*.plan.md`. First line `status: active` or `status: done`. Each `active` plan is one workstream, and several can be active at once — read only the one your task belongs to (`tools/facts.sh plans` lists them). No separate index.

A plan is running state: next step, unknown, done-when. Not a milestone log of finished work, and not a durable decision (those graduate).

When writing or updating a plan:

- **Hot:** biggest unknown, the next one step, done-when (command + expected result). Not a dump of live config.
- One workstream per file. One next step at a time.
- Check off only with evidence (command output or a log path).
- Patch, do not rewrite the whole file.
- A reject is a durable conclusion. While the evidence is still coming in, keep it as one line in the plan. Once it is settled, move the reason to `docs/` (or this file, if every session must follow it) and leave one pointer line in the plan. A plan marked `done` holds no reject that exists only there.
- When the workstream is finished, `status: done`. Do not leave it `active`.
- Before `status: done`, review what the workstream made false — including files it never opened. The drift that survives is never in the file you edited.
- **Open** is a question this workstream has not answered — not a decision, not a deferral. It can also be a question this workstream ran into that belongs to another area; it carries forward like any other Open. An Open leaves when something settles it: into the Hot list if it turns out to be in scope, into Rejected if it settles against (and then graduates like any reject), into `docs/` if it becomes a thing to watch. Otherwise it stays.
- Opens stay in the closed plan. They are the backlog the next plan is written against. Do not open a plan for one — a plan is planned, not spawned because an Open exists.
- When a later plan settles an Open that lives in a closed plan, delete it from that Open section. An Open is backlog; one with an answer is not backlog, and `tools/facts.sh plans` counts it as though it were. The answer has to be in `docs/` (or this file) before the Open goes — if it is nowhere durable, write it there first. Settle only the part that is answered: an Open that was half answered is rewritten down to the question that is left. Editing a closed plan this way is not re-opening it.
- Before writing a new plan, read the Open sections of the `done` plans. That is where the last workstream left its unfinished questions, and `tools/facts.sh plans` shows which plans still carry any.
- Do not re-open a `done` plan. Write a new one and point at `docs/`.
- Durable conclusions leave the hot plan: how/findings → `docs/`; a constraint every session must follow → this file.
- Do not paste facts-script output into any of these files. If a script can print it, or it will go stale and cannot be scripted, look it up when needed.

## This repo vs `~/.hermes/`

A clone of this repo must be enough to rebuild what we built. Anything we authored under `~/.hermes` (including `profiles/<name>/`) belongs here, with a symlink into the live path. Do not leave those edits only on the live side. Do not git-init `~/.hermes/`.

Hermes code at `~/.hermes/hermes-agent` (`hermes update` owns it) is upstream, not ours — do not track it here and do not treat edits there as harness work.

Runtime and secrets stay in `~/.hermes`, not tracked: `.env`, `auth.json`, `cron/jobs.json`, `memories/`, sessions, logs, cache, `state.db`. Cron and memories are agent-mutated; a symlink would dirty this tree on every run.

| | |
| --- | --- |
| Custom skills | `skills/` here. `skills.external_dirs` points Hermes at them. Do **not** symlink into `~/.hermes/skills/` — that tree is bundled skills that `hermes update` syncs. Agent `skill_manage create` still writes there; move keepers into this repo. Same split for a named profile: keepers under `profiles/<name>/skills/` here, `external_dirs` on that profile. |
| `config.yaml`, `SOUL.md`, hooks, plugins, skill-bundles | Canonical copy here, symlink into `~/.hermes/` or `~/.hermes/profiles/<name>/`. Hermes' `atomic_replace` writes through the symlink — `hermes config set` will not replace the link with a regular file. Do not pre-create empty SOUL/hooks/plugins copies; add them when the first real file exists. |
| `.env`, `auth.json`, `cron/jobs.json`, `memories/`, sessions, logs, cache, `state.db` | Stay in `~/.hermes/`, not tracked. Secrets never enter git. |

Do **not** overwrite a live symlink with `cp` or an editor "save as". In-place edits and `hermes config set` are fine.

## Where things live

| | |
| --- | --- |
| Hermes code | `~/.hermes/hermes-agent` (upstream clone, `hermes update` owns it) |
| Its venv | `~/.hermes/hermes-agent/venv` — not `.venv` |
| Config | `config/config.yaml` here; `~/.hermes/config.yaml` is a symlink to it |
| Persona | `config/SOUL.md` here; `~/.hermes/SOUL.md` is a symlink to it. Every session's system prompt opens with it, on every platform |
| Secrets | `~/.hermes/.env` (not tracked) |
| Binary | `~/.local/bin/hermes` |
| Bundled skills | `~/.hermes/skills/` (`hermes update` owns them) |
| Custom skills | `skills/` here, via `skills.external_dirs` |
| Open WebUI functions | `open-webui/` here. Open WebUI keeps the live copy in its own database, so an edit here does nothing until `tools/owui-pipe.sh` pushes it |

Never load with **parallel > 1** — it multiplies KV cache, it does not split it. Hermes needs context **>= 64k**. The working recipe on this 4090 (exact ctx, KV quant, eval batch) is `docs/lm-studio.md`. What is actually loaded right now: `tools/facts.sh lmstudio`. Do not assume the GUI still matches the recipe.

## Conventions

- This machine has passwordless sudo. The Hermes installer already used it (ripgrep, ffmpeg, Playwright libs).
- Claude Code's shell sets `HTTPS_PROXY=http://127.0.0.1:8888`, which exits in the US. Hermes exits in Hong Kong. Unset the proxy variables before testing any search engine that localizes by IP, or the results are not what Hermes sees.
- A service meant to stay up runs as a systemd user unit. A unit we write: canonical copy in this repo, symlinked into `~/.config/systemd/user/`. The gateway unit is Hermes': `hermes gateway install` writes it and Hermes rewrites it on every gateway start, so it is not tracked and not symlinked; our changes to it go in a drop-in (`hermes-gateway.service.d/*.conf`) here. Starting a service from an agent's shell is for a probe only: stop it when the probe is done, and say it was stopped. How: `docs/services.md`.
- `.claude/settings.json` sets `autoMemoryEnabled: false`: agent memory must land in this repo, not in `~/.claude`, or Cursor and git can't see it.
- "Can we do X?" means technically possible (API, plugin, extra tool, a patch in this repo). Hermes not shipping it out of the box is a default, not a no. Do not talk as if a missing config key or a thin client wrapper make it techically impossible.
