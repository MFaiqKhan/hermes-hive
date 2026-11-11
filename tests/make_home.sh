#!/usr/bin/env bash
# make_home.sh DIR: a fresh throwaway HERMES_HOME at DIR with Hive installed (never your real ~/.hermes).
# Optional: MODEL_BASE_URL / MODEL_NAME to point the home at a local OpenAI-compatible model (e.g. Ollama).
set -euo pipefail
DIR=${1:?usage: make_home.sh /some/throwaway/dir}
HERE=$(cd "$(dirname "$0")" && pwd)
export HERMES_HOME="$DIR"
case "$(cd "$(dirname "$DIR")" 2>/dev/null && pwd)/$(basename "$DIR")" in "$HOME/.hermes") echo "refusing: that is your real Hermes home"; exit 1;; esac
if [ -d "$HERMES_HOME" ]; then hermes gateway stop >/dev/null 2>&1 || true; rm -rf "${HERMES_HOME:?}"; fi
mkdir -p "$HERMES_HOME/plugins/hive"; chmod 700 "$HERMES_HOME"
cp "$HERE"/../*.py "$HERE"/../plugin.yaml "$HERE"/../README.md "$HERMES_HOME/plugins/hive/"
hermes plugins enable hive < /dev/null >/dev/null 2>&1 || hermes plugins enable hive < /dev/null
if [ -n "${MODEL_BASE_URL:-}" ]; then
  hermes config set model.provider custom < /dev/null >/dev/null
  hermes config set model.base_url "$MODEL_BASE_URL" < /dev/null >/dev/null
  hermes config set model.default "${MODEL_NAME:?set MODEL_NAME too}" < /dev/null >/dev/null
fi
echo "$HERMES_HOME"
