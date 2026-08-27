#!/usr/bin/env bash
# Repeat a deterministic test command for a fixed interval. It performs no API
# calls and stops at the first failure, preserving the complete test log.
set -euo pipefail

if [[ $# -lt 3 || "$1" != "--duration-seconds" ]]; then
  echo "usage: $0 --duration-seconds <positive-integer> -- <test-command...>" >&2
  exit 2
fi
DURATION=$2
shift 2
[[ "$1" == "--" ]] || { echo "missing -- before test command" >&2; exit 2; }
shift
[[ "$DURATION" =~ ^[1-9][0-9]*$ ]] || { echo "duration must be a positive integer" >&2; exit 2; }
[[ $# -gt 0 ]] || { echo "test command required" >&2; exit 2; }

START=$(date +%s)
DEADLINE=$((START + DURATION))
ITERATION=0
while (( $(date +%s) < DEADLINE )); do
  ITERATION=$((ITERATION + 1))
  printf '[%s] soak iteration=%d\n' "$(date -u +%FT%TZ)" "$ITERATION"
  "$@"
  # Avoid needless CPU burn while still catching state leakage and flaky tests.
  sleep 1
done
printf '[%s] soak PASS iterations=%d elapsed_seconds=%d\n' \
  "$(date -u +%FT%TZ)" "$ITERATION" "$(( $(date +%s) - START ))"
