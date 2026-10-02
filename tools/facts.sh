#!/usr/bin/env bash
# Live facts for this machine. Print, do not paste into plans or CLAUDE.md.
# Usage: tools/facts.sh [config|lmstudio|hermes|net|plans|tools|web|webui|news|jobs]

set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PLANS_DIR="$ROOT/.cursor/plans"
export PATH="$HOME/.local/bin:$PATH"

HERMES="${HERMES:-$HOME/.local/bin/hermes}"
LMS="${LMS:-/mnt/c/Users/user/.lmstudio/bin/lms.exe}"
WINDOWS_TS_HOST=gpu-desktop-1
WSL_TS_HOST=gpu-desktop-1-wsl

ok() { printf '%s=%s\n' "$1" "$2"; }
fail() { printf '%s=%s\n' "$1" "ERROR: $2"; }

topic=${1:-summary}

case "$topic" in
  -h|--help|help)
    cat <<'EOF'
tools/facts.sh [topic]
  (none)     active plan, config symlink, model name
  config     hermes config values that change, and the symlinks into ~/.hermes
  lmstudio   loaded model, context, parallel
  hermes     installed version
  net        Tailscale IPs and LM Studio URL
  plans      status line of every .cursor/plans/*.plan.md
  tools      the toolsets and tool names each platform's agent gets
  web        search and fetch health from the plugin audit logs
  webui      Open WebUI and gateway units, Serve, task model, automations, chat Pipe
  news       AI news digest: ledger window and last post, eval timer, latest eval run
  jobs       HK job digest: ledger window, last run's source errors, posts, feedback
EOF
    exit 0
    ;;
esac

facts_plans() {
  local f status actives=0
  if [[ ! -d "$PLANS_DIR" ]]; then
    fail plans_dir "missing $PLANS_DIR"
    return
  fi
  shopt -s nullglob
  for f in "$PLANS_DIR"/*.plan.md; do
    status=$(awk 'NR==1 && /^status:/{print $2; exit}' "$f")
    if [[ -z "${status:-}" ]]; then
      ok "plan.$(basename "$f")" "MISSING_STATUS"
      continue
    fi
    local opens
    # A closed plan keeps the questions the workstream did not answer. New plans
    # are written against those, so they have to be findable without reading
    # every closed file.
    opens=$(awk '/^## Open/{o=1;next} /^## /{o=0} o && /^- /{n++} END{print n+0}' "$f")
    if [[ "$status" == "done" && "$opens" -gt 0 ]]; then
      ok "plan.$(basename "$f")" "$status ($opens open)"
    else
      ok "plan.$(basename "$f")" "$status"
    fi
    if [[ "$status" == "active" ]]; then
      ok active_plan "${f#"$ROOT"/}"
      actives=$((actives + 1))
    fi
  done
  if (( actives == 0 )); then
    ok active_plan NONE
  fi
}

facts_jobs() {
  # The job digest's own state (docs/hk-jobs.md). A pending batch means a run
  # that died before its answer; its entries are offered again. The last pull's
  # errors say whether CTgoodjobs is still behind its CAPTCHA, without a request.
  local db="${JOB_LEDGER_DB:-$HOME/.hermes/job_ledger.db}"
  if [[ -s "$db" ]]; then
    JOB_DB="$db" python3 - <<'EOF'
import json, os, sqlite3
from datetime import datetime, timedelta, timezone
db = sqlite3.connect(f"file:{os.environ['JOB_DB']}?mode=ro", uri=True)
meta = dict(db.execute("select k, v from meta"))
hkt = lambda s: datetime.fromisoformat(s).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M HKT") if s else "none"
print(f"jobs_high_water={hkt(meta.get('high_water'))}")
pending = meta.get("pending_batch")
print(f"jobs_pending_batch={'none' if not pending else hkt(pending) + ' ERROR: not recorded — the run died before its answer'}")
if "last_errors" in meta:
    print(f"jobs_last_pull_errors={'; '.join(json.loads(meta['last_errors'])) or 'none'}")
else:
    print("jobs_last_pull_errors=not recorded yet")
week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
seen = db.execute("select count(distinct batch || entry_id) from offered where seen = 1 and batch >= ?", (week,)).fetchone()[0]
posted = db.execute("select count(*) from posted where posted_at >= ?", (week,)).fetchone()[0]
print(f"jobs_7d=seen {seen}, posted {posted}")
if db.execute("select 1 from sqlite_master where name = 'ads'").fetchone():
    n_ads, last = db.execute("select count(*), max(last_pulled) from ads").fetchone()
    n_jd, n_none = db.execute("select count(*), count(*) - count(text) from ad_jd").fetchone()
    print(f"jobs_cache=ads {n_ads}, JDs {n_jd} ({n_none} pages without one), last pull {hkt(last)}")
fb =db.execute("select verdict, count(*) from feedback group by verdict").fetchall()
print(f"jobs_feedback={', '.join(f'{v} {n}' for v, n in fb) or 'none'}")
EOF
  else
    ok job_ledger "none yet: $db"
  fi
  # Verdicts belong in the ledger; one in memory steers every later prompt.
  local mem
  mem=$(cat "$HOME"/.hermes/memories/*.md 2>/dev/null | grep -ciE 'job digest|job preference|職位' || true)
  ok jobs_in_memory "${mem:-0}$( [[ "${mem:-0}" -gt 0 ]] && echo ' WATCH: job verdicts belong in the ledger, not memory')"
}

facts_config() {
  local link target expected
  expected="$ROOT/config/config.yaml"
  if [[ ! -e "$HOME/.hermes/config.yaml" ]]; then
    fail config_symlink missing
  elif [[ -L "$HOME/.hermes/config.yaml" ]]; then
    target=$(readlink -f "$HOME/.hermes/config.yaml")
    if [[ "$target" == "$expected" ]]; then
      ok config_symlink "ok -> $target"
    else
      fail config_symlink "points at $target (expected $expected)"
    fi
  else
    fail config_symlink "not a symlink"
  fi

  # Everything else authored here and linked into ~/.hermes or the user's systemd
  # directory. A regular file or a dangling link at one of these paths means the
  # live copy and the repo have parted, and an edit on either side no longer
  # reaches the other.
  local rel live
  for rel in config/SOUL.md plugins/*/ systemd/*; do
    rel=${rel%/}
    rel=${rel#"$ROOT"/}
    case "$rel" in
      config/SOUL.md) live="$HOME/.hermes/SOUL.md" ;;
      plugins/*) live="$HOME/.hermes/plugins/${rel#plugins/}" ;;
      systemd/*) live="$HOME/.config/systemd/user/${rel#systemd/}" ;;
    esac
    if [[ -L "$live" && "$(readlink -f "$live")" == "$ROOT/$rel" ]]; then
      ok "link.$rel" ok
    elif [[ -e "$live" && ! -L "$live" ]]; then
      fail "link.$rel" "$live is a regular file, not a link to the repo"
    else
      fail "link.$rel" "$live missing or points elsewhere"
    fi
  done

  if [[ ! -x "$HERMES" ]]; then
    fail hermes "not executable: $HERMES"
    return
  fi
  local key
  for key in model.default model.provider model.base_url web.search_backend web.extract_backend web.keyless_rescue web.keyless_fallback skills.external_dirs approvals.mode approvals.cron_mode approvals.unattended_mode approvals.single_query_mode; do
    if out=$("$HERMES" config get "$key" 2>/dev/null); then
      ok "$key" "$out"
    else
      fail "$key" "hermes config get failed"
    fi
  done
}

facts_hermes() {
  if [[ ! -x "$HERMES" ]]; then
    fail hermes "not executable: $HERMES"
    return
  fi
  local line
  line=$("$HERMES" --version 2>/dev/null | head -n1)
  ok hermes_version "${line:-unknown}"
  ok hermes_bin "$HERMES"
}

facts_net() {
  local wsl_ip win_ip
  if ! command -v tailscale >/dev/null; then
    fail tailscale missing
    return
  fi
  wsl_ip=$(tailscale ip -4 "$WSL_TS_HOST" 2>/dev/null || tailscale ip -4 2>/dev/null || true)
  win_ip=$(tailscale ip -4 "$WINDOWS_TS_HOST" 2>/dev/null || true)
  ok wsl_host "$WSL_TS_HOST"
  ok wsl_ip "${wsl_ip:-unknown}"
  ok windows_host "$WINDOWS_TS_HOST"
  ok windows_ip "${win_ip:-unknown}"
  if [[ -n "${win_ip:-}" ]]; then
    ok lmstudio_url "http://${win_ip}:1234/v1"
  else
    fail lmstudio_url "no Windows Tailscale IP"
  fi
}

facts_lmstudio() {
  if [[ -x "$LMS" ]]; then
    ok lms_bin "$LMS"
    echo "--- lms ps ---"
    "$LMS" ps || fail lms_ps "lms.exe ps failed"
    echo "---"
  else
    fail lms_bin "missing $LMS"
  fi

  # How much of the 4090 the loaded model leaves. The digest's embedding model
  # runs on the CPU because of this number (docs/ai-news.md).
  local smi=/usr/lib/wsl/lib/nvidia-smi
  if [[ -x "$smi" ]]; then
    ok gpu_memory_mib "$("$smi" --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | sed 's/, */ used of /')"
  else
    fail gpu_memory_mib "no $smi"
  fi

  local url code
  url=$("$HERMES" config get model.base_url 2>/dev/null || true)
  if [[ -n "${url:-}" ]]; then
    ok lmstudio_configured_url "$url"
    code=$(curl -sS -m 3 -o /dev/null -w '%{http_code}' "$url/models" 2>/dev/null) || true
    ok lmstudio_http "${code:-000}"
  fi
}

facts_tools() {
  # Which tools each platform's agent is handed, resolved by Hermes' own code
  # rather than read off config.yaml: a platform with no entry falls back to its
  # default toolset, and a toolset name expands to tools only in there.
  # `hermes tools` answers the same question but requires a TTY, so it is no use
  # from a script or an agent.
  local agent="$HOME/.hermes/hermes-agent"
  if [[ ! -x "$agent/venv/bin/python" ]]; then
    fail tools_python "missing $agent/venv/bin/python"
    return
  fi
  # Two different counts, both wanted. `tools.<platform>` is what the toolsets
  # allow; `schema.<platform>` is what the model is actually handed, which is
  # smaller whenever a tool gates itself on a key or an environment (image
  # generation without a provider key, kanban outside a kanban run).
  local plat js
  for plat in api_server cli; do
    js=$("$HERMES" prompt-size --platform "$plat" --json 2>/dev/null) || js=""
    if [[ -z "$js" ]]; then
      fail "schema.$plat" "hermes prompt-size failed"
    else
      ok "schema.$plat" "$(printf '%s' "$js" | python3 -c 'import json, sys
d = json.load(sys.stdin)["tools"]
print(d["count"], "tools,", d["json_bytes"], "B of schema")')"
    fi
  done

  ( cd "$agent" && ./venv/bin/python - "$HOME/.hermes/config.yaml" <<'PYEOF'
import sys, yaml
sys.path.insert(0, ".")
from hermes_cli.tools_config import _get_platform_tools
from toolsets import resolve_toolset

cfg = yaml.safe_load(open(sys.argv[1])) or {}
for platform in ("api_server", "cli"):
    names = sorted(_get_platform_tools(cfg, platform, include_default_mcp_servers=False))
    tools = sorted({t for n in names for t in resolve_toolset(n)})
    print(f"toolsets.{platform}={' '.join(names) or 'none'}")
    print(f"tools.{platform}={len(tools)}: {' '.join(tools) or 'none'}")
PYEOF
  ) || fail tools "resolution failed"
}

facts_web() {
  # Whether search and fetch are working *now*, read from the plugins' own audit
  # logs rather than from the agent. A model asked to relay that its search
  # engine is down does not reliably do it, so a quota that has run out looks
  # like a normal answer in chat and like a run of failures here.
  #
  # Counted over a recent window, not over the whole file: three failures among
  # ten thousand calls is history, three among the last twenty is an outage. The
  # last-success time is the other half — a window of failures matters much more
  # when nothing has worked since yesterday.
  local window=${FACTS_WEB_WINDOW:-20}
  local search_log="${SEARCH_REGION_LOG:-$HOME/.hermes/logs/search-region.jsonl}"
  local fetch_log="${FETCH_CASCADE_LOG:-$HOME/.hermes/logs/fetch-cascade.jsonl}"

  stamp_of() { sed -n 's/.*"at": "\([^"]*\)".*/\1/p'; }
  last_with() {
    local at
    at=$(grep "$1" "$2" 2>/dev/null | tail -n1 | stamp_of)
    printf '%s' "${at:-none}"
  }
  # Only lines that carry an outcome are counted, so the numbers always add up to
  # the window even while older lines written before outcomes existed are still
  # in the file.
  labelled() { grep '"outcome": "' "$1" 2>/dev/null; }

  if [[ -s "$search_log" ]]; then
    local total recent
    total=$(labelled "$search_log" | wc -l | tr -d ' ')
    recent=$(labelled "$search_log" | tail -n "$window")
    ok search_window "last $((total < window ? total : window)) of $total calls"
    ok search_recent_ok "$(grep -c '"outcome": "ok"' <<<"$recent" || true)"
    ok search_recent_empty "$(grep -c '"outcome": "empty"' <<<"$recent" || true)"
    ok search_recent_failed "$(grep -c '"outcome": "failed"' <<<"$recent" || true)"
    ok search_recent_region "$(grep -c '"region": "' <<<"$recent" || true)"
    ok search_last_ok "$(last_with '"outcome": "ok"' "$search_log")"
    local failed_at failed_err
    failed_at=$(last_with '"outcome": "failed"' "$search_log")
    if [[ "$failed_at" == none ]]; then
      ok search_last_failed none
    else
      failed_err=$(grep '"outcome": "failed"' "$search_log" | tail -n1 | sed -n 's/.*"backend_error": "\([^"]*\)".*/\1/p')
      ok search_last_failed "$failed_at — ${failed_err:-unknown}"
    fi
  else
    fail search_log "empty or missing: $search_log (set SEARCH_REGION_LOG in ~/.hermes/.env)"
  fi

  if [[ -s "$fetch_log" ]]; then
    local total recent
    total=$(labelled "$fetch_log" | wc -l | tr -d ' ')
    recent=$(labelled "$fetch_log" | tail -n "$window")
    ok fetch_window "last $((total < window ? total : window)) of $total layer attempts"
    ok fetch_recent_accepted "$(grep -c '"outcome": "accepted"' <<<"$recent" || true)"
    ok fetch_recent_escalated "$(grep -c '"outcome": "escalated"' <<<"$recent" || true)"
    ok fetch_recent_degraded "$(grep -c '"outcome": "degraded"' <<<"$recent" || true)"
    ok fetch_recent_failed "$(grep -c '"outcome": "failed"' <<<"$recent" || true)"
    ok fetch_last_accepted "$(last_with '"outcome": "accepted"' "$fetch_log")"
    ok fetch_last_failed "$(last_with '"outcome": "failed"' "$fetch_log")"
    local hosts
    hosts=$(grep -o '"url": "[^"]*"' <<<"$recent" | sed 's|.*://||; s|/.*||' | sort | uniq -c | sort -rn | head -n5 | awk '{printf "%s(%s) ", $2, $1}')
    ok fetch_recent_hosts "${hosts:-none}"
  else
    fail fetch_log "empty or missing: $fetch_log (set FETCH_CASCADE_LOG in ~/.hermes/.env)"
  fi
}

facts_webui() {
  # Open WebUI and the Hermes side it talks to. The checks at the end catch
  # failures that are silent in the UI (docs/open-webui.md): a pinned task model
  # loads a second 27B beside the chat, a Pipe that does not match its source in
  # this repo runs code nobody can read here, and a child api_server session is
  # where a compression rotation or a fork happened.
  local u
  for u in open-webui hermes-gateway; do
    ok "unit.$u" "$(systemctl --user is-active "$u" 2>/dev/null) $(systemctl --user is-enabled "$u" 2>/dev/null)"
  done
  ok open_webui_version "$("$HOME/.local/share/uv/tools/open-webui/bin/python" -c 'import importlib.metadata as m; print(m.version("open-webui"))' 2>/dev/null || echo 'ERROR: not installed')"
  ok open_webui_health "$(curl -sS --noproxy '*' -m 3 http://127.0.0.1:8080/health 2>/dev/null || echo 'ERROR: no answer')"
  ok tailscale_serve "$(tailscale serve status 2>/dev/null | head -n2 | tr '\n' ' ')"

  WEBUI_DB="${OPEN_WEBUI_DB:-$HOME/.local/share/open-webui/webui.db}" \
  HERMES_DB="$HOME/.hermes/state.db" \
  PIPE_FILE="$ROOT/open-webui/pipes/hermes_session.py" \
  python3 - <<'EOF'
import json, os, sqlite3

def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

try:
    w = ro(os.environ["WEBUI_DB"])
    cfg = lambda k: json.loads((w.execute("select value from config where key=?", (k,)).fetchone() or ["null"])[0])
    task = cfg("task.model.external") or ""
    # Empty means titles, tags and follow-ups use the chat's pipe, which calls
    # LM Studio with that pipe's weights. A fixed id loads a second 27B.
    problems = []
    if task: problems.append(f"pinned to {task!r}; a chat on the other model loads both")
    print(f"task_model={task or 'chat'} " + ("ok" if not problems else "ERROR: " + "; ".join(problems)))
    # An Automation's RRULE is read in its owner's timezone (docs/open-webui.md),
    # so the schedule means nothing without it.
    for name, tz in w.execute("select name, timezone from user"):
        print(f"user_timezone.{name}={tz or 'none — schedules fall back to the server clock'}")
    from datetime import datetime
    when = lambda ns: datetime.fromtimestamp(ns / 1e9).strftime("%Y-%m-%d %H:%M") if ns else "none"
    autos = w.execute("select name, is_active, data, last_run_at, next_run_at from automation").fetchall()
    if not autos:
        print("automations=none")
    for name, active, data, last, nxt in autos:
        d = json.loads(data)
        print(f"automation.{name}={'on' if active else 'off'} {d.get('rrule')} model={d.get('model_id')}"
              f" last={when(last)} next={when(nxt)}")

    # The chat model is a Pipe function, and Open WebUI keeps it in this
    # database: the repo file is the source, so a file edited and not pushed
    # leaves the chat running the older code with nothing in the UI to show it.
    pipe_file = os.environ["PIPE_FILE"]
    pipe_id = os.path.basename(pipe_file)[:-3]
    fn = w.execute("select content, is_active from function where id=?", (pipe_id,)).fetchone()
    mdl = w.execute("select is_active from model where id=?", (pipe_id,)).fetchone()
    problems = []
    if fn is None:
        problems.append("not installed")
    else:
        if not fn[1]: problems.append("function switched off")
        if fn[0] != open(pipe_file).read(): problems.append("differs from the repo file")
    if mdl is not None and mdl[0] == 0: problems.append("switched off in admin Models")
    print(f"owui_pipe={pipe_id} " + ("ok" if not problems else "ERROR: " + "; ".join(problems) + " — tools/owui-pipe.sh"))
    # Same source, second function id. The picker name is Hermes Obliterated.
    obl_id = "hermes_session_obliterated"
    obl = w.execute("select content, is_active, name from function where id=?", (obl_id,)).fetchone()
    obl_problems = []
    if obl is None:
        obl_problems.append("not installed")
    else:
        if not obl[1]: obl_problems.append("function switched off")
        if obl[0] != open(pipe_file).read(): obl_problems.append("differs from the repo file")
        if obl[2] != "Hermes Obliterated": obl_problems.append(f"name is {obl[2]!r}")
    print(f"owui_pipe={obl_id} " + ("ok" if not obl_problems else "ERROR: " + "; ".join(obl_problems) + " — tools/owui-pipe.sh"))

    # Who can get an account and get in. These are settings, so they rot in
    # prose: docs/security.md points here rather than restating them, and keeps
    # the part that does not rot. Signup staying off is a decision, not a taste.
    onoff = lambda v: "?" if v is None else ("on" if v else "off")
    signup = cfg("ui.enable_signup")
    admins = w.execute("select count(*) from user where role='admin'").fetchone()[0]
    login = (
        f"owui_login=signup:{onoff(signup)}"
        f" default_role:{cfg('ui.default_user_role') or '?'}"
        f" api_keys:{onoff(cfg('auth.enable_api_keys'))}"
        f" ldap:{onoff(cfg('ldap.enable'))}"
        f" admins:{admins}"
    )
    print(login + (" ERROR: signup is on (docs/security.md)" if signup else ""))
except Exception as e:
    print(f"open_webui_db=ERROR: {e}")

try:
    h = ro(os.environ["HERMES_DB"])
    n = h.execute("select count(*) from sessions where source='api_server' and parent_session_id is not null").fetchone()[0]
    # A child session is either a compression rotation or a fork.
    print(f"api_server_child_sessions={n}")
except Exception as e:
    print(f"hermes_state_db=ERROR: {e}")
EOF
}

facts_news() {
  # The digest's own state (docs/ai-news.md): the window it will cover next, and
  # whether the last candidates it handed out were ever posted. A batch pulled
  # after the last recorded one means a run that died before its post, or a post
  # that cited none of its stories' links.
  local db="${NEWS_LEDGER_DB:-$HOME/.hermes/news_ledger.db}"
  if [[ -s "$db" ]]; then
    NEWS_DB="$db" python3 - <<'EOF'
import os, sqlite3
from datetime import datetime, timedelta, timezone
db = sqlite3.connect(f"file:{os.environ['NEWS_DB']}?mode=ro", uri=True)
meta = dict(db.execute("select k, v from meta"))
hkt = lambda s: datetime.fromisoformat(s).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M HKT") if s else "none"
hw, pending = meta.get("high_water"), meta.get("pending_high_water")
print(f"news_high_water={hkt(hw)}")
print(f"news_last_batch={hkt(pending)} " + ("posted" if pending and pending == hw else "ERROR: not posted — the last digest run died or cited no links"))
week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
print(f"news_posted_7d={db.execute('select count(*) from posted where posted_at >= ?', (week,)).fetchone()[0]}")
last = db.execute("select title, posted_at from posted order by posted_at desc limit 1").fetchone()
print(f"news_last_posted={hkt(last[1]) + ' — ' + last[0][:70] if last else 'none'}")
EOF
  else
    ok news_ledger "none yet: $db"
  fi

  ok unit.feeds-eval.timer "$(systemctl --user is-active feeds-eval.timer 2>/dev/null) $(systemctl --user is-enabled feeds-eval.timer 2>/dev/null)"
  local run
  run=$(ls -d "$ROOT"/evals/feeds/runs/*/ 2>/dev/null | tail -n1)
  if [[ -n "$run" ]]; then
    ok feeds_eval_runs "$(ls -d "$ROOT"/evals/feeds/runs/*/ | wc -l | tr -d ' ')"
    ok feeds_eval_latest "$(basename "$run") failed: $(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
s = d['sources'] if isinstance(d, dict) else d
print(', '.join(x['id'] for x in s if x.get('status') == 'error') or 'none')" "$run/sources.json" 2>/dev/null || echo '?')"
  else
    ok feeds_eval_runs 0
  fi
}
facts_summary() {
  facts_plans | grep -E '^(active_plan|plan\.)' || true
  facts_config | grep -E '^(config_symlink|model\.default)=' || true
}

case "$topic" in
  summary)  facts_summary ;;
  plans)    facts_plans ;;
  tools)    facts_tools ;;
  config)   facts_config ;;
  hermes)   facts_hermes ;;
  net)      facts_net ;;
  lmstudio) facts_lmstudio ;;
  web)      facts_web ;;
  webui)    facts_webui ;;
  news)     facts_news ;;
  jobs)     facts_jobs ;;
  *)
    echo "unknown topic: $topic" >&2
    echo "try: tools/facts.sh help" >&2
    exit 2
    ;;
esac
