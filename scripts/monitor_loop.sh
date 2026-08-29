#!/usr/bin/env bash
# Run the 15-minute weatherbot runtime report in a loop.
set -euo pipefail
cd "$(dirname "$0")/.."
while true; do
  python3 scripts/monitor_report.py || true
  sleep 900
done
