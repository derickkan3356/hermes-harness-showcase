#!/usr/bin/env bash
# Install or update an Open WebUI function from this repo.
# Usage: tools/owui-pipe.sh [file [id [name]]]
#
# With no arguments, pushes open-webui/pipes/hermes_session.py twice: as
# hermes_session (Hermes Agent) and hermes_session_obliterated (Hermes
# Obliterated). Both functions are that one file. Open WebUI keeps functions
# in its database, so the file here is the source and this pushes it over the
# admin API. The function id is the model id in the chat and in Automations.
#
# The admin API key is an Open WebUI account key (Settings -> Account -> API
# keys), in $OWUI_API_KEY or ~/.config/open-webui/api-key.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
URL=${OWUI_URL:-http://127.0.0.1:8080}
KEY_FILE=${OWUI_API_KEY_FILE:-$HOME/.config/open-webui/api-key}
PIPE=$ROOT/open-webui/pipes/hermes_session.py

KEY=${OWUI_API_KEY:-}
if [[ -z "$KEY" && -f "$KEY_FILE" ]]; then
  KEY=$(tr -d '[:space:]' < "$KEY_FILE")
fi
if [[ -z "$KEY" ]]; then
  echo "no API key: set OWUI_API_KEY or write one to $KEY_FILE" >&2
  exit 1
fi

# Claude Code's shell has a proxy that exits in the US; Open WebUI is local.
api() {
  local method=$1 path=$2
  shift 2
  curl -sS --noproxy '*' -X "$method" "$URL/api/v1$path" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" "$@"
}

# push FILE ID NAME
# NAME empty means the title: line in the file.
push() {
  local file=$1 id=$2 name=${3:-}
  [[ -f "$file" ]] || { echo "no such file: $file" >&2; exit 1; }
  local payload
  payload=$(FILE="$file" ID="$id" NAME="$name" python3 <<'PY'
import json, os, re

content = open(os.environ["FILE"]).read()
head = content.split('"""')[1] if content.startswith('"""') else ""


def field(name, default):
    m = re.search(rf"^{name}:\s*(.+)$", head, re.M)
    return m.group(1).strip() if m else default


print(json.dumps({
    "id": os.environ["ID"],
    "name": os.environ["NAME"] or field("title", os.environ["ID"]),
    "content": content,
    "meta": {"description": field("description", ""), "manifest": {}},
}))
PY
)
  local action
  if api GET "/functions/id/$id" | grep -q '"id"'; then
    action=update
    api POST "/functions/id/$id/update" -d "$payload" > /dev/null
  else
    action=create
    api POST "/functions/create" -d "$payload" > /dev/null
  fi
  local active
  active=$(api GET "/functions/id/$id" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("is_active"))')
  if [[ "$active" != "True" ]]; then
    api POST "/functions/id/$id/toggle" > /dev/null
    active=$(api GET "/functions/id/$id" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("is_active"))')
  fi
  echo "$id $action active=$active"
}

if [[ $# -eq 0 ]]; then
  push "$PIPE" hermes_session
  push "$PIPE" hermes_session_obliterated "Hermes Obliterated"
else
  push "$1" "${2:-$(basename "$1" .py)}" "${3:-}"
fi
