#!/usr/bin/env bash
# Live demo on two throwaway Hermes homes (A and B) with the official `hermes`, Hive installed from GitHub,
# a local Nostr relay, and one PNG per step in $SHOTS. Never touches the real ~/.hermes.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=${HIVE_VIDEO_DIR:-/tmp/hive-video}
SHOTS=${SHOTS:?folder for the screenshots}
REF=${REF:?commit SHA to install}
REPO=${REPO:-https://github.com/MFaiqKhan/hermes-hive}
HERMES=${HERMES_BIN:-$HOME/.local/bin/hermes}
PY=$(ls -d "$BASE"/A/installs/*/environments/*/venv/bin/python 2>/dev/null | head -1)
mkdir -p "$SHOTS"; rm -f "$SHOTS"/*.png "$SHOTS"/*.txt
TEACH='Run `datactl sync`. If it fails, run `datactl cache rebuild --force`, then run `datactl sync` again. Tell me how many records were synced.'
ASK='Run `datactl sync` and get it working. Then tell me how many records were synced.'

on() {  # on A|B <command...>: run hermes as that machine
  local m=$1; shift
  ( cd "$BASE/$m/shell-home" && HERMES_HOME="$BASE/$m" HOME="$BASE/$m/shell-home" PATH="$HERE/demo:$PATH" \
    "$@" < /dev/null 2>&1 )
}
shot() {  # shot NAME TITLE FILE: render
  "$PY" "$HERE/shot.py" "$3" "$SHOTS/$1.png" && echo "   saved $1.png"
}
step() {  # step NAME MACHINE "shown command" real command...
  local name=$1 m=$2 shown=$3; shift 3
  { echo "Hermes Agent · machine $m"; echo "\$ $shown"; on "$m" "$@" | grep -v "RuntimeWarning"; } > "$SHOTS/$name.txt"
  shot "$name" "" "$SHOTS/$name.txt"
}
transcript() {  # transcript NAME MACHINE PROMPT: the newest chat as a readable log
  local name=$1 m=$2 prompt=$3
  { echo "Hermes Agent · machine $m · chat"; echo "\$ hermes chat -t terminal"; echo "> $prompt"; echo
    "$PY" - "$BASE/$m/state.db" <<'PY'
import json, sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
sid = db.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()[0]
calls = {}
for role, content, tc, tcid in db.execute("SELECT role, content, tool_calls, tool_call_id FROM messages "
                                          "WHERE session_id=? ORDER BY id", (sid,)):
    if role == "assistant" and tc:
        for c in json.loads(tc):
            a = c["function"]["arguments"]; a = json.loads(a) if isinstance(a, str) else a
            calls[c["id"]] = a.get("command")
    elif role == "tool" and tcid in calls:
        print(f"▶ terminal: {calls[tcid] or '(a call without a command)'}")
        try:
            out = json.loads(content.split("\n\n[Tool loop")[0]).get("output") or ""
        except Exception:
            out = content or ""
        for ln in out.strip().splitlines()[:12]:
            print("  " + ln)
    elif role == "assistant" and content:
        print(); print("Hermes: " + content.strip().replace("\n", " ")[:400])
PY
  } > "$SHOTS/$name.txt"
  shot "$name" "" "$SHOTS/$name.txt"
}
hinted() { grep -q "Hive: this error has been fixed before" "$SHOTS/$1.txt"; }
finished() { grep -q "synced 42 records" "$SHOTS/$1.txt"; }

"$PY" "$HERE/mock_relay.py" 7447 > "$BASE/relay.log" 2>&1 & RELAY=$!
trap 'kill $RELAY 2>/dev/null' EXIT
sleep 1
for m in A B; do on "$m" "$HERMES" gateway stop > /dev/null; rm -rf "$BASE/$m/datactl-state"; done

echo "== machine A"
step 01-version A "hermes --version" "$HERMES" --version
step 02-install A "hermes plugins install https://github.com/MFaiqKhan/hermes-hive --enable" \
     "$HERMES" plugins install "$REPO" --ref "$REF" --enable --force --yes-deps
{ echo "Hermes Agent · machine A"; echo "\$ hermes plugins list | grep -i hive"; on A "$HERMES" plugins list | grep -iE "hive" | head -2; } > "$SHOTS/03-plugins.txt"; shot 03-plugins "" "$SHOTS/03-plugins.txt"
on A "$HERMES" hive forget --all > /dev/null
{ echo "Hermes Agent · machine A"; echo "\$ hermes hive relays ws://127.0.0.1:7447"; on A "$HERMES" hive relays ws://127.0.0.1:7447
  echo "\$ hermes hive network share"; on A "$HERMES" hive network share; echo "\$ hermes hive status"; on A "$HERMES" hive status; } > "$SHOTS/04-network-share.txt"
shot 04-network-share "" "$SHOTS/04-network-share.txt"
on A "$HERMES" gateway stop > /dev/null
on A "$HERMES" chat -Q --oneshot -t terminal --reasoning none --max-turns 20 -q "$TEACH" > "$SHOTS/05-answer.log"
transcript 05-chat-A A "$TEACH"
step 06-hive-list A "hermes hive list" "$HERMES" hive list
step 07-outbox A "hermes hive outbox" "$HERMES" hive outbox
step 08-sync A "hermes hive sync" "$HERMES" hive sync

echo "== machine B"
step 09-install-B B "hermes plugins install https://github.com/MFaiqKhan/hermes-hive --enable" \
     "$HERMES" plugins install "$REPO" --ref "$REF" --enable --force --yes-deps
on B "$HERMES" hive forget --all > /dev/null
{ echo "Hermes Agent · machine B"; echo "\$ hermes hive relays ws://127.0.0.1:7447"; on B "$HERMES" hive relays ws://127.0.0.1:7447
  echo "\$ hermes hive network read"; on B "$HERMES" hive network read
  echo "\$ hermes hive settings --min-peers 1"; on B "$HERMES" hive settings --min-peers 1; } > "$SHOTS/10-network-read.txt"
shot 10-network-read "" "$SHOTS/10-network-read.txt"
for try in 1 2 3; do
  on B "$HERMES" gateway stop > /dev/null; rm -rf "$BASE/B/datactl-state" "$BASE/B/shell-home/datactl-state"
  if [ "$try" -gt 1 ]; then   # a fresh B each try: nothing a stopped try did on this machine carries over
    on B "$HERMES" hive forget --all > /dev/null; on B "$HERMES" hive settings --min-peers 1 > /dev/null; fi
  on B "$HERMES" chat -Q --oneshot -t terminal --reasoning none --max-turns 20 -q "$ASK" > "$SHOTS/11-answer.log"
  transcript 11-chat-B B "$ASK"; cp "$SHOTS/11-chat-B.txt" "$SHOTS/11-chat-B-try$try.txt"   # every try is kept
  if hinted 11-chat-B && finished 11-chat-B; then echo "   B got the Hive hint and finished (try $try)"; break; fi
  hinted 11-chat-B && echo "   try $try: B got the Hive hint but the model stopped before syncing; retrying" \
                   || echo "   try $try: no Hive hint (the model did not hit the error the usual way); retrying"
done
step 12-status-B B "hermes hive status" "$HERMES" hive status
SIG=$(on A "$HERMES" hive list | grep -oE "^[0-9a-f]{10}" | head -1)
[ -n "$SIG" ] && step 13-show-B B "hermes hive show $SIG" "$HERMES" hive show "$SIG"
echo "done: $SHOTS"
