status: done

# Security — take the capability away, stop trying to gate the command

The agent's job is reading pages anyone can write, and its `terminal` tool runs as `user`, who has passwordless sudo. Between those two facts sits Hermes' approval gate. That gate is a blocklist of ~97 regexes over a shell, and a blocklist over a shell cannot be made accurate: the command can be composed (`a; b && c`), built at run time (`X=rm; $X -rf`, base64 into `sh`), moved into another interpreter (`python3 -c`), or split across two harmless-looking steps (write a script, then run it). None of these are hypothetical — `.cursor/plans/frontend.plan.md` records a `python3 -c` inside a subshell that carried four `[HIGH]` Tirith findings and was auto-approved anyway. That plan's Open on command approval is what this plan picks up.

Shape: the Open WebUI path (`api_server`) gets tools that take **data**, not code — a URL, a query, a date. Shell stays on `cli`, where a human is present and approval prompts actually reach someone. Anything the agent must *do* beyond reading is a plugin with typed arguments, the way `fetch-cascade` already is; a skill is instructions over existing tools and grants no new capability.

This is the cheap moment to do it: `skills/` is empty, so nothing is written against the wider toolset yet.

## Hot

- Biggest unknown: none left. The capability is gone from `api_server`, and every remaining item is a write-up.
- [x] Graduate the durable conclusions to `docs/security.md`: the toolset table and how to re-resolve it, why a command blocklist cannot be the boundary, the Tirith measurement, the approvals block, root through WSL interop, and what still reaches the agent. `CLAUDE.md` carries the decision; `docs/services.md` carries the Open WebUI path.
- [x] Narrow `platform_toolsets.api_server` to `[safe, skills, memory, todo, session_search]` in `config/config.yaml`. Drops `terminal`, `process`, `execute_code`, `file`, `browser`, `delegate_task`, `cronjob`. `safe` is `web` + `vision` + `image_gen`; the `search-region` and `fetch-cascade` plugins ride on `web` and need no entry. `tools/facts.sh tools` resolves the live config through Hermes' own code and shows `api_server` with no `terminal`, `execute_code`, `write_file` or `browser_*`, against the full set on `cli`. Live on the restarted gateway: "run `id -un`" → `NO_SHELL_TOOL` plus that same tool list; a web_search + web_extract request still works. `hermes tools --summary` refuses to run non-interactively, so the resolver is the evidence. No Open WebUI Automation exists (`tools/facts.sh webui` → `automations=none`), and an Automation is an ordinary request on this platform, which the live test covers.

## Queued

- [x] **Write the `approvals` block.** `mode: manual`, `timeout` left at 300, `cron_mode`, `single_query_mode` and `unattended_mode` all `deny`. `load_config_readonly()` reads them back. Guards `cli` and whatever platform comes next. This also answers where `smart` came from: upstream's default for `approvals.mode` is `smart`, not `manual` (`hermes_cli/config_defaults.py`), so the auxiliary-model approvals in the frontend plan's log were the default doing its job with no block written.
- [x] **Mark untrusted text in `fetch-cascade`.** `mark_untrusted()` in `provider.py` fences every returned page under one sentence naming it as data; a marker in the page's own text is broken first, so the page cannot close the fence early. `strip_untrusted()` is the inverse and the fetch eval uses it, so char counts stay comparable with earlier runs. Live: `https://example.com` comes back fenced under the degraded warning; through the API server, a Wikipedia fetch still summarises correctly. How and why: `docs/web-fetch.md` (Page text is fenced as untrusted).
- [x] **Open WebUI signup.** Off. `webui.db` `config`: `ui.enable_signup = false`, `ui.default_user_role = "pending"`, `auth.enable_api_keys = false`, `ldap.enable = false`, one user, role `admin`.

## Rejected

- **The `browser` toolset on any unattended platform.** `browser_exec` runs Python on the host, and `fetch-cascade` already renders. `cli` keeps it. → `docs/security.md`
- **Turning Windows interop off.** It would close `wsl.exe -u root`, but the framework as it stands is enough and `/mnt/c` stays mounted either way. → `docs/security.md` (Root on this machine)
- **Removing passwordless sudo.** Does not close root on WSL; interop reaches uid 0 with no sudo involved. → `docs/security.md` (Root on this machine)
- **Gating the command string accurately.** Undecidable in practice over a shell; measured against Tirith. → `docs/security.md` (Why not gate the command instead)
- **Designing for a second Open WebUI account.** One person uses this harness, and nothing below the login separates two people anyway. Signup stays off. → `docs/security.md` (Open WebUI)
- **A `full` and a `safe` profile, so a model picker chooses the toolset.** Not a boundary. → `docs/security.md` (The boundary is the toolset)
- **Allowlist of argv vectors with no shell.** The only design that could gate accurately. Needs a patch to Hermes' terminal tool and an allowlist a general agent outgrows every week. → `docs/security.md`
