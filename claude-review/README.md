# claude-review

A CLI tool that sends recent agent work to Claude (via Claude Code CLI or Anthropic API) for a quick review. Designed for integration with OMP (Oh My Pi) or any CLI agent.

Same architecture as [codex-review](../codex-review/) but uses Claude as the reviewer instead of Codex/OpenAI.

## Features

- **One-shot review**: Pipe in context, get a review back
- **Multi-turn**: Claude can request more context via `NEED_CONTEXT:` protocol
- **Cron mode**: Periodic auto-review with state tracking — only reviews new content
- **Delivery**: stdout, file, OMP inbox, or Telegram (via Hermes cron)
- **Flexible backend**: Claude Code CLI (`-p` print mode) or Anthropic Messages API directly

## Quick Start

```bash
# Install
chmod +x claude_review.py
ln -s /path/to/claude_review.py /usr/local/bin/claude-review

# One-shot
echo "I refactored auth.py to use JWT..." | claude-review

# Multi-turn (Claude can ask for more context)
echo "..." | claude-review --multi-turn --context-cmd "cat /tmp/omp_session.log"

# Cron mode (periodic auto-review)
claude-review --cron --context-file /tmp/omp_session.log --deliver telegram,file
```

## Auth

Set one of:
- `ANTHROPIC_API_KEY` — Anthropic API directly
- `CLAUDE_REVIEW_API_KEY` + `CLAUDE_REVIEW_API_BASE` — custom endpoint
- `claude auth login` — Claude CLI OAuth (Pro/Max subscription)
- Auto-detects Hermes `.env` for `ANTHROPIC_API_KEY`

## Key Differences from codex-review

| | codex-review | claude-review |
|---|---|---|
| Reviewer | Codex / OpenAI | Claude / Anthropic |
| CLI tool | `codex exec` | `claude -p` (print mode) |
| API format | OpenAI Chat Completions | Anthropic Messages API |
| System prompt | As first message | Top-level `system` parameter |
| Auth header | `Authorization: Bearer` | `x-api-key` |
| API version | Not needed | `anthropic-version: 2023-06-01` |
| Git repo | Required by Codex CLI | Not required by Claude `-p` |
| Default model | `o4-mini` / `ai01-glm5.2` | `claude-sonnet-4-20250514` |
| State file | `/tmp/codex-review-state.json` | `/tmp/claude-review-state.json` |

## Claude CLI Advantages

- **No git repo required** — `claude -p` works anywhere (Codex CLI needs a git repo)
- **No PTY needed** — print mode (`-p`) is fully non-interactive
- **No sandbox issues** — no bubblewrap/namespace requirements
- **Model variety** — `sonnet`, `opus`, `haiku` available via `--model`

## License

MIT
