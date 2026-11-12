#!/usr/bin/env bash
# Live demo on two throwaway Hermes homes and a local mock Nostr relay (nothing is published publicly).
#   A meets an error whose message does not say how to fix it, finds the fix the hard way, Hive learns it and
#   publishes it to the local relay. B (another "machine") meets the same error and gets the fix from the hive.
# Hermes's normal approvals stay on (no --yolo): commands Hermes considers dangerous are not run unattended.
# Needs: a model the homes can reach (the sandbox uses qwen3.5:4b on Ollama at 127.0.0.1:11435),
#        MAKE_HOME (script that creates a home with Hive installed and prints its path), DEMO_BIN (datactl).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
MAKE_HOME=${MAKE_HOME:-$HERE/make_home.sh}      # called as: make_home.sh <dir>
DEMO_BIN=${DEMO_BIN:-$HERE/demo}
BASE=${BASE:-/tmp/hive-demo-homes}              # the two throwaway homes go here
PORT=${PORT:-7447}
OUT=${OUT:-/tmp/hive-live-demo}
mkdir -p "$OUT"
PROMPT='Run `datactl sync` in the terminal and get it working. Then tell me how many records were synced.'

python3 "$HERE/mock_relay.py" "$PORT" > "$OUT/relay.log" 2>&1 &
RELAY=$!
trap 'kill $RELAY 2>/dev/null' EXIT
sleep 1

count_calls() {  # terminal calls in the newest session of a home
  python3 - "$1/state.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
sid = db.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()[0]
rows = db.execute("SELECT content FROM messages WHERE session_id=? AND role='tool' AND tool_name='terminal' "
                  "ORDER BY id", (sid,)).fetchall()
hinted = sum("Hive: this error has been fixed before" in (r[0] or "") for r in rows)
print(f"{len(rows)} terminal calls, {hinted} with a Hive hint")
PY
}

run_chat() {  # $1 home, $2 label. HOME points inside the throwaway home: the agent's shell never sees the real one.
  echo "== $2: asking the agent"
  mkdir -p "$1/shell-home"
  HOME="$1/shell-home" HERMES_HOME="$1" PATH="$DEMO_BIN:$PATH" timeout 900 hermes chat -Q --oneshot -t terminal --reasoning none \
      --max-turns 20 -q "$PROMPT" < /dev/null > "$OUT/$2.answer" 2>&1
  tail -3 "$OUT/$2.answer" | sed 's/^/   answer: /'
  echo "   $(count_calls "$1")"
}

home() {  # REUSE=1 keeps an existing throwaway home (saves the runtime build) but resets Hive and the demo tool
  local dir="$BASE/$1"
  if [ "${REUSE:-0}" = 1 ] && [ -d "$dir/plugins/hive" ]; then
    dir=$(cd "$dir" && pwd)
    HERMES_HOME="$dir" hermes gateway stop < /dev/null > /dev/null 2>&1 || true   # a live gateway keeps old plugin code
    rm -rf "${dir:?}/plugin-data/hive" "${dir:?}/datactl-state" "${dir:?}/shell-home"
    cp "$HERE"/../*.py "$HERE"/../plugin.yaml "$dir/plugins/hive/"
    echo "$dir"
  else
    mkdir -p "$BASE"; "$MAKE_HOME" "$BASE/$1" | tail -1
  fi
}
A=$(home hv-a)
B=$(home hv-b)
for H in "$A" "$B"; do
  HERMES_HOME="$H" hermes hive relays "ws://127.0.0.1:$PORT" < /dev/null > /dev/null
done
HERMES_HOME="$A" hermes hive network share < /dev/null > /dev/null
HERMES_HOME="$B" hermes hive network read < /dev/null > /dev/null

run_chat "$A" machine-A
echo "== machine-A: what Hive learned"
HERMES_HOME="$A" hermes hive list < /dev/null | sed 's/^/   /'
HERMES_HOME="$A" hermes hive sync < /dev/null | sed 's/^/   /'

HERMES_HOME="$B" hermes hive settings --min-peers 1 < /dev/null > /dev/null   # two machines: one other install is enough (default 2)
run_chat "$B" machine-B
echo "== machine-B: the hint the agent saw"
python3 - "$B/state.db" <<'PY' | sed 's/^/   /'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for (c,) in db.execute("SELECT content FROM messages WHERE role='tool' AND content LIKE '%Hive: this error%' LIMIT 1"):
    print(c[:700])
PY
