#!/usr/bin/env bash
# Weekly wrapper for the Stilta lead drafter.
# Runs the full pipeline and appends a timestamped log. The program's own dedup
# (seen_dockets.json) makes re-runs idempotent, so this is safe to run repeatedly.
set -euo pipefail

# Resolve the repo root from this script's location (schedule/ is one level down).
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
mkdir -p logs

TS="$(date +%Y%m%dT%H%M%S)"
# Draft the top 12 qualified leads by priority. Use --top-n 0 to draft them all.
/usr/bin/python3 run.py --top-n 12 >> "logs/weekly-${TS}.log" 2>&1
