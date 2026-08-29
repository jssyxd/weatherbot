#!/usr/bin/env bash
# Start the tree12-allno observer on the host (fallback when Docker daemon
# cannot pull the base image). Uses the same ./data directory as the Docker
# compose volume, so a later switch to Docker keeps all state.
set -euo pipefail
cd "$(dirname "$0")/.."

# Only target OUR observer, never the other weatherbot Docker containers.
if [ -f data/observer.pid ]; then
  old="$(cat data/observer.pid 2>/dev/null || true)"
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    kill "$old" 2>/dev/null || true
    sleep 1
  fi
fi
rm -f data/observer.lock

export CHECKWX_API_KEY="${CHECKWX_API_KEY:-}"
if [ -z "$CHECKWX_API_KEY" ] && [ -f .env ]; then
  # shellcheck disable=SC2046
  export $(grep -v '^#' .env | xargs)
fi

nohup python3 -u metar_observer.py run --config config.json \
  > data/observer.log 2>&1 < /dev/null &
echo $! > data/observer.pid
echo "observer started, pid=$(cat data/observer.pid)"
