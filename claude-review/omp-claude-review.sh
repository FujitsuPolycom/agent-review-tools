#!/usr/bin/env bash
# omp-claude-review — OMP plugin: get Claude's perspective on recent work
# 
# Usage in OMP:
#   review = "claude-review --no-interactive -v"
#   Or: omp_context | claude-review --no-interactive
#
# For cron mode (auto-review every X minutes):
#   claude-review --cron --context-file /tmp/omp_session.log --deliver telegram

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/claude_review.py" --no-interactive "$@"
