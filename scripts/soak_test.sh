#!/usr/bin/env bash
# Repeat the local unit suite for a bounded duration. This script has no network,
# credentials, order, wallet, or account operations.
set -euo pipefail
DURATION_SECONDS="${1:-300}"
TEST_COMMAND="${TEST_COMMAND:-python3 -m unittest discover -s tests -q}"
if ! [[ "$DURATION_SECONDS" =~ ^[0-9]+$ ]] || (( DURATION_SECONDS < 5 || DURATION_SECONDS > 900 )); then
  echo "duration must be an integer between 5 and 900 seconds" >&2
  exit 2
fi
start_epoch="$(date +%s)"
deadline=$((start_epoch + DURATION_SECONDS))
iterations=0
while (( "$(date +%s)" < deadline )); do
  if ! eval "$TEST_COMMAND"; then
    echo "[soak] failed_iteration=$((iterations + 1))" >&2
    exit 1
  fi
  iterations=$((iterations + 1))
  sleep 5
done
printf '{"status":"PASS","duration_seconds":%s,"iterations":%s,"safety":{"network_calls":0,"orders_submitted":0,"credentials_loaded":false}}\n' "$DURATION_SECONDS" "$iterations"
