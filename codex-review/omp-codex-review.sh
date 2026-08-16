#!/usr/bin/env bash
# omp-codex-review — OMP plugin: get Codex's perspective on recent work
# 
# Usage in OMP:
#   review = "codex-review --no-interactive -v"
#   Or: omp_context | codex-review --no-interactive
#
# For cron mode (auto-review every X minutes):
#   codex-review --cron --context-file /tmp/omp_session.log --deliver telegram

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/codex_review.py" --no-interactive "$@"
