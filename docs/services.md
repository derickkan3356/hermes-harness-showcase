# Services

Everything that must stay up runs as a systemd user unit. Linger is on for `user`, so user units run without a login session.

| Unit | Serves | Written by |
| --- | --- | --- |
| `hermes-gateway.service` | Hermes gateway, including the OpenAI-compatible API server on `127.0.0.1:8642` | Hermes (`hermes gateway install`) |
| `open-webui.service` | Open WebUI on `127.0.0.1:8080`, including its Automations scheduler | This repo, `systemd/open-webui.service` |
| `feeds-eval.timer` | `evals/feeds/feeds_eval.py` every six hours, measuring the AI news sources | This repo, `systemd/feeds-eval.{service,timer}` |

## Keeping WSL up

By default WSL stops the distro about 15 seconds after the last WSL window closes, and systemd services do not count as activity. The VM can stay up while the distro inside it restarts, so `uptime` does not show it: compare the start time of PID 1 (`ps -o lstart= -p 1`) instead.

`C:\Users\user\.wslconfig` turns both idle timeouts off:

```ini
[general]
instanceIdleTimeout=-1

[wsl2]
vmIdleTimeout=-1
```

It takes effect after `wsl --shutdown` from Windows. `instanceIdleTimeout` is the one that stops the distro; `vmIdleTimeout` alone is not enough (microsoft/WSL#13291).

Nothing starts WSL when Windows boots. After a reboot, open a WSL window (or VS Code on WSL) and the gateway starts with it; closing the window afterwards is fine.

## The gateway unit

Install on a fresh clone:

```fish
hermes gateway install --start-now --start-on-login
```

This writes `~/.config/systemd/user/hermes-gateway.service`, enables it, and turns on linger (no sudo needed here; polkit allows it).

Hermes owns that file. On every gateway start it compares the file with the unit it would generate and rewrites it if they differ (`refresh_systemd_unit_if_needed` in `hermes_cli/gateway.py`). The write follows symlinks, so a repo copy symlinked into place would be overwritten on the next Hermes update, and any edit to it is lost. Changes of ours go in a drop-in, which Hermes does not read or write: `systemd/hermes-gateway.service.d/<name>.conf` here, symlinked into `~/.config/systemd/user/hermes-gateway.service.d/`, then `systemctl --user daemon-reload`.

The unit bakes in the `PATH` of the shell that ran `hermes gateway install`. Run it from a normal login shell.

The API server reads `API_SERVER_ENABLED`, `API_SERVER_HOST`, `API_SERVER_PORT`, `API_SERVER_KEY` from `~/.hermes/.env`. Restart the unit after changing them.

The systemd user manager has no proxy variables, so the gateway reaches the web directly from the home line — the same exit Hermes should have. Do not add `HTTPS_PROXY` to a drop-in.

## The Open WebUI unit

Install on a fresh clone, after `uv tool install --python 3.11 open-webui` and writing the env file below:

```fish
ln -s ~/projects/hermes-harness/systemd/open-webui.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now open-webui
```

The unit reads `~/.config/open-webui/env` (mode 600, not tracked). It holds:

| Variable | Value |
| --- | --- |
| `WEBUI_SECRET_KEY` | `openssl rand -hex 32`, set once. Without it, `open-webui serve` writes a key file into its working directory, and a new key logs everyone out. |
| `DATA_DIR` | `/home/user/.local/share/open-webui` (database, uploads, vector store) |
| `OPENAI_API_BASE_URLS` | `http://127.0.0.1:8642/v1;<LM Studio URL>` — Hermes first, LM Studio second |
| `OPENAI_API_KEYS` | `<API_SERVER_KEY from ~/.hermes/.env>;` — LM Studio takes no key |
| `OPENAI_API_CONFIGS` | `{"1":{"enable":true,"model_ids":["<model LM Studio has loaded>"]}}` |
| `TASK_MODEL_EXTERNAL` | empty. Tasks follow the chat's pipe and hit that pipe's weights on LM Studio |
| `ENABLE_OLLAMA_API` | `false` |
| `WEBUI_URL` | `https://gpu-desktop-1-wsl.example-tailnet.ts.net` |
| `CORS_ALLOW_ORIGIN` | `https://gpu-desktop-1-wsl.example-tailnet.ts.net;http://localhost:8080;http://127.0.0.1:8080` — also the websocket's allowed origins. An origin missing here breaks chat streaming from that address. |
| `HERMES_API_SERVER_KEY` | the same `API_SERVER_KEY`. The `hermes_session` Pipe reads it from the process environment, so the key stays in this file instead of a second copy in Open WebUI's database. |

systemd keeps the JSON's inner quotes in `EnvironmentFile`; do not wrap the value in extra quotes.

`HERMES_API_SERVER_KEY` is read on every start, by the Pipe. Restart the unit after changing it.

Open WebUI reads the connection settings and `WEBUI_URL` from the env only on its first start. After that the database holds them (`config` table), and the admin UI is the place to change them. A change to the env file alone does nothing on a running install.

Why a title calls LM Studio through the pipe, and why `TASK_MODEL_EXTERNAL` stays empty: `docs/open-webui.md` (Background tasks go to LM Studio). `tools/facts.sh webui` checks it.

The chat model is a Pipe function, pushed from this repo by `tools/owui-pipe.sh`. That needs an Open WebUI account API key (Settings → Account → API keys) in `~/.config/open-webui/api-key`, mode 600, not tracked. What the Pipe does: `docs/open-webui.md` (The Pipe).

It listens on `127.0.0.1` only. Other devices reach it through Tailscale Serve.

## The feeds eval timer

`feeds-eval.timer` runs `evals/feeds/feeds_eval.py` at 00, 06, 12 and 18 (`docs/ai-news.md`). It is a oneshot service and its timer; install on a fresh clone:

```fish
ln -s ~/projects/hermes-harness/systemd/feeds-eval.service ~/projects/hermes-harness/systemd/feeds-eval.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now feeds-eval.timer
```

`Persistent=true` catches up a run missed while WSL was down.

## Tailscale Serve

Open WebUI is at `https://gpu-desktop-1-wsl.example-tailnet.ts.net`, tailnet only (Serve, not Funnel). Tailscale terminates HTTPS with a Let's Encrypt certificate it renews itself, and proxies to `http://127.0.0.1:8080`. The Android home-screen app needs HTTPS.

```fish
tailscale serve --bg --https=443 http://127.0.0.1:8080
tailscale serve status
```

The config lives in `tailscaled`'s state and survives reboots; there is nothing to track here. `user` is the Tailscale operator, so `tailscale serve` needs no sudo. On a fresh machine: `sudo tailscale set --operator=$USER` once, and Serve and HTTPS certificates must be enabled for the tailnet in the admin console (the first `tailscale serve` prints the link).

The first HTTPS request after enabling waits for the certificate and can time out. Retry.

Anyone who logs in to Open WebUI drives Hermes through the API server, and that platform's toolset has no shell, no file tools and no browser; what it can do is the typed tools `docs/security.md` lists. Reaching the tailnet is not the same as getting a chat: `tools/facts.sh webui` prints who can get an account and get in, as `owui_login=`.

## Everyday commands

```fish
hermes gateway status
systemctl --user restart hermes-gateway
journalctl --user -u hermes-gateway -f
systemctl --user restart open-webui
journalctl --user -u open-webui -f
```

Check the API server:

```fish
curl -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8642/v1/models
```

`$API_SERVER_KEY` is in `~/.hermes/.env`; it is not in the shell environment by default.
