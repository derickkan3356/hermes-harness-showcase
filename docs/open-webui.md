# Open WebUI

Open WebUI is the chat frontend on laptop and phone. It runs in WSL, talks to the Hermes gateway's OpenAI-compatible API server on `127.0.0.1:8642` through the Pipe function `hermes_session`, and is reached at `https://gpu-desktop-1-wsl.example-tailnet.ts.net` over the tailnet. Install, env file, unit and Tailscale Serve: `docs/services.md`.

On Android it is installed to the home screen from Chrome. Automation runs raise no notification while the app is closed; the chat is there when the app is opened.

## Scheduled posts are Open WebUI Automations

An Automation sends its prompt to `hermes_session` on a schedule, and each run opens a new chat (in the folder chosen for it). Follow-ups, edits and regenerates in that chat work like any other chat. The Automations scheduler runs inside the `open-webui` process.

Hermes cron is not used for these posts. Its delivery targets (`_KNOWN_DELIVERY_PLATFORMS` in `cron/scheduler.py`) do not include Open WebUI, so putting a Hermes cron result into an Open WebUI chat needs a webhook-to-chat service of our own and a second scheduler.

Create Automations in the UI. Open WebUI's `create_automation` chat tool is an Open WebUI tool, and Hermes does not run client tools. An Automation's schedule is an RRULE read in its owner's timezone — `user.timezone`, which the browser reports (`POST /api/v1/auths/update/timezone`); without one it falls back to the server's clock (`next_run_ns` in `open_webui/utils/automations.py`). `tools/facts.sh webui` prints the account's timezone (`user_timezone.<name>`) and every Automation's schedule.

## The Pipe

The chat model is a Pipe function. Its source is `open-webui/pipes/hermes_session.py` here; Open WebUI keeps functions in its database, so an edit to the file does nothing until it is pushed:

```fish
tools/owui-pipe.sh
```

That creates or updates both functions and leaves them active. `tools/facts.sh webui` prints `owui_pipe=hermes_session ok` and `owui_pipe=hermes_session_obliterated ok` when the installed copies still match the file here. It needs an Open WebUI account API key (Settings → Account → API keys) in `~/.config/open-webui/api-key`, and `HERMES_API_SERVER_KEY` in the Open WebUI env file (`docs/services.md`) — the API server refuses session continuation with 403 unless the request is authenticated.

The function id is the model id, in the chat and in Automations. Two functions are the same file, pushed by `tools/owui-pipe.sh`:

| id | picker name | model sent to Hermes |
| --- | --- | --- |
| `hermes_session` | Hermes Agent | `hermes-agent`, the gateway default (`qwen/qwen3.8-27b`) |
| `hermes_session_obliterated` | Hermes Obliterated | `qwen3.8-27b-obliterated` |

The second id is a `model_routes` alias on the API server (`platforms.api_server.extra` in `config/config.yaml`). Both use the LM Studio endpoint in `model.base_url`. One 27B fits on the card. LM Studio's just-in-time loading is on, so a turn on one unloads the other.

On `qwen3.8-27b-obliterated`, the `search-cap` plugin (`plugins/search_cap/`) counts `web_search` calls after the latest user message. At 20, the next LM Studio request goes out with no tools, and tool rounds whose calls were already made are dropped, plus one line telling the model to answer from the results it has. Hermes' mid-turn continue lines — the empty-response nudge, a cut-off continuation, a dropped tool call — stay on that same count. The next message the user sends in the same chat starts the count at zero. If that tool-free request does not end the turn, Hermes' halt at 50 searches does.

`hermes-agent` and the raw alias `qwen3.8-27b-obliterated` are switched off in the admin Models list. Switching one back on is a fallback if the Pipe breaks — a fallback without session continuity, where Hermes derives the session id from the system prompt plus the first user message instead (`_derive_chat_session_id`), as it does for anything else that arrives without the header.

The Pipe calls the API server itself, so that connection is not in its path. It forwards the messages and the sampling parameters. Open WebUI's own fields (`chat_id`, `features`, `tool_ids`, …) stay behind.

## How a chat maps to a Hermes session

The Pipe sends `X-Hermes-Session-Id: owui-<chat id>` with every turn. Hermes then drops the history in the request body and rebuilds the conversation from its own record in `~/.hermes/state.db`, tool calls and tool results included (the `provided_session_id` branch of `_handle_chat_completions`, `gateway/platforms/api_server.py`). Only the last user message of the body is used.

A follow-up therefore sees the searches and page extracts behind the earlier answers, not only their text, and can answer from them without searching again. In one probe the first turn ran six tool calls in 33 s; the follow-up answered in 2 s with no tool call, citing a figure that exists only in the first turn's tool result.

A plain connection cannot do this. It sends fixed headers, and one fixed session id would merge every chat into one transcript.

What a turn was given is one line in `~/.hermes/logs/agent.log`:

```
agent.turn_context: conversation turn: session=owui-<chat id> … platform=api_server history=14 msg='降雨機率幾多？'
```

`history` counts the restored messages, tool rows included — 0 on the first turn of a chat, 14 on a follow-up to a turn that ran five searches. Without the header a follow-up would show 2.

Being given the tool results is not the same as using them: three of the first four follow-ups searched again anyway. That choice is the model's, and nothing in the prompt pushes it towards its own transcript. The job here is to put the transcript in front of it; a question that wants something current is right to go and look again, and a rule that said otherwise would be wrong as often as it was right.

What follows from that:

- One chat is one Hermes session, and nothing else shares it. Two chats that open with the same first message are separate transcripts, so each run of an Automation is its own.
- The restored context is bounded, and grows only on turns that call tools: 17.5k tokens after a search-heavy first turn, 17.6k after two text-only turns, 20.1k after another search. The window is 65k, so three or four tool-heavy turns before compression fires.
- Edit and regenerate write both branches into the one session in time order, and that transcript is the model's context — so after an edit the model still sees the branch that was replaced. `POST /api/sessions/{id}/fork` copies a transcript into a child session and is the tool for splitting them; nothing calls it yet.
- A background task (title, tags, follow-ups) carries no session header. The Pipe sends it to LM Studio with this chat's weights, so it cannot write into the chat's transcript.
- A temporary chat gets a session like any other. Open WebUI does not keep it; Hermes records every turn either way.

## Compression

Hermes compresses inside a turn once the context passes `compression.threshold` in `config/config.yaml`, and may move the session to a child session (`parent_session_id` set on the child). The Pipe keeps sending the parent id: a streaming response writes `X-Hermes-Session-Id` into the SSE headers before the turn runs, so the new id cannot come back that way, and what the parent id restores after a rotation is not measured. Session listings carry `parent_session_id` and `_lineage_root_id`, so the Pipe could resolve the lineage tip after each turn if this starts to bite. `tools/facts.sh webui` counts the `api_server` sessions that have a parent (`api_server_child_sessions`); a fork lands in that count too.

Open WebUI's own Context Compaction (`chat.context_compaction.enable`) stays off. Hermes builds the context from its own record, so compacting the request body changes nothing the model sees, and the summary call would queue on the same GPU slot.

## Files from tools

A Hermes tool that makes a file for the user (the `cv` plugin's `cv_render`) writes it to `~/.hermes/outbox/<session id>/`, where the session id is the lineage root, i.e. the `owui-<chat id>` the Pipe sends; a tool handler resolves it from `state.db` because after compression it runs under the child id. After the turn's stream ends, the Pipe uploads each file there through Open WebUI's `upload_file_handler` (`process=False`, owned by `__user__`), links it to the chat message, emits a `files` event so it shows as a file button on the message, appends a plain download link per file (`/api/v1/files/<id>/content?attachment=true`), and moves it to `sent/`. The button opens a preview (DOCX and PDF render); its Content tab stays empty because nothing is extracted. A turn stopped with Stop never reaches the upload, so its files go out with the next reply in that chat. The model never sees a path. Valve `outbox_dir` moves the root.

## Background tasks go to LM Studio

Titles, tags and follow-up suggestions are Open WebUI background tasks. `task.model.external` is empty, so each task uses the chat's model (`get_task_model_id`, `utils/task.py`). The Pipe sees `__task__` and calls LM Studio itself with that pipe's weights — `qwen/qwen3.8-27b` for Hermes Agent, `qwen3.8-27b-obliterated` for Hermes Obliterated — and does not open a Hermes session. A title sent through Hermes would be a full agent turn queued ahead of the next message. A fixed external task model loads the other 27B, because just-in-time loading treats a different id as a different model.

`tools/facts.sh webui` prints `task_model=chat ok`. A non-empty `task.model.external` is the pin that loads both.

## Model settings for `hermes_session`

In the admin Models list, capabilities of `hermes_session`:

| Setting | State | Why |
| --- | --- | --- |
| Builtin Tools | off | Hermes ignores client `tools` on `/v1/chat/completions`; they do nothing. |
| Memory | off | Hermes has its own memory. |
| Web Search, Code Interpreter, Terminal, Image Generation | off | Open WebUI would run them before Hermes sees the prompt, next to Hermes' own tools. |
| File Upload, File Context | on | Hermes cannot read Open WebUI's uploads; File Context puts their text into the prompt. |
| Vision | on | Not tested with the loaded model. |

## Stop

The Stop button cancels the chat task; Open WebUI closes the Pipe's generator (`process_chat_response`, `utils/middleware.py`), the Pipe closes its connection to the API server, and Hermes ends the turn. In `agent.log`: "SSE client disconnected; interrupted agent task", then `Turn ended: reason=interrupted_by_user` when the interrupt lands between tool calls, or `reason=interrupted_during_api_call` when it lands inside one. Nothing keeps running in the background.

## Alternatives that were ruled out

- Hermes dashboard chat (`hermes dashboard`): the TUI in an xterm over a PTY. Unusable on a phone, no message editing.
- Hermes Desktop: no Android build, and its sidebar leaves out `cron` sessions.
- Hermes cron with glue code into Open WebUI chats: see above.
- Telegram, Slack or ntfy for the phone: Open WebUI is enough there, and notifications are not needed. Slack is where Hermes' continuable cron (a thread per brief) is most complete, if a messaging surface is ever wanted.
- Docker for Open WebUI: not installed in WSL, and a network layer between it and the API server. Worth another look if `uv tool upgrade` breaks on dependencies twice.
- `/v1/responses` chaining (`conversation` or `previous_response_id`) to carry tool results between turns: it restores them, but each stored response concatenates the prior history with the agent's returned transcript, which already contains that history, so the stored transcript doubles every turn. One three-turn chat went 14 messages, 30, then 65 — 25.8k characters, 54.7k, then 143k — and the third turn already filled the 64k window. `truncation: "auto"` does not help: it caps the message count, not tokens. The 100-response store cap (`MAX_STORED_RESPONSES`) is not configurable either. `X-Hermes-Session-Id` on `/v1/chat/completions` restores the same tool results from `state.db`, which stays linear.
