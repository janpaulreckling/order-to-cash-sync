#!/usr/bin/env bash
# Scripted terminal demo for the README GIF. Every command runs for real.
set -euo pipefail
export COLUMNS=96
type_cmd() {
  printf '\033[1;32m$\033[0m '
  local s="$1"; for ((i = 0; i < ${#s}; i++)); do printf '%s' "${s:i:1}"; sleep 0.03; done
  printf '\n'; sleep 0.4
}
# o2c sync exits 2 when records were dead-lettered; that is expected here.
run() { type_cmd "$1"; eval "$1" || true; sleep "${2:-1.5}"; }
note() { printf '\033[2m# %s\033[0m\n' "$1"; sleep 1.2; }

note "A rep wins a deal in Salesforce"
run "uv run python scripts/demo_new_deal.py" 1.5
note "One incremental sync: CRM -> ERP order, ERP status -> CRM"
run "uv run o2c sync" 3
note "Bad records were parked, not lost"
run "uv run o2c dlq list" 3
