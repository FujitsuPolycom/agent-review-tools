# codex-review

A CLI tool that sends recent agent work to an LLM (Codex, OpenAI, or any OpenAI-compatible endpoint) for a quick review. Designed for integration with [OMP (Oh My Pi)](https://github.com/) or any CLI agent.

## Features

- **One-shot review**: Pipe in context, get a review back
- **Multi-turn**: Reviewer can request more context via `NEED_CONTEXT:` protocol
- **Cron mode**: Periodic auto-review with state tracking — only reviews new content
- **Delivery**: stdout, file, OMP inbox, or Telegram (via Hermes cron)
- **Flexible backend**: Codex CLI, OpenAI API, or any OpenAI-compatible endpoint (LiteLLM, vLLM, etc.)

## Quick Start

```bash
# Install
chmod +x codex_review.py
ln -s /path/to/codex_review.py /usr/local/bin/codex-review

# One-shot
echo "I refactored auth.py to use JWT..." | codex-review

# Multi-turn (reviewer can ask for more context)
echo "..." | codex-review --multi-turn --context-cmd "cat /tmp/omp_session.log"

# Cron mode (periodic auto-review)
codex-review --cron --context-file /tmp/omp_session.log --deliver telegram,file
```

## Auth

Set one of:
- `OPENAI_API_KEY` — OpenAI API directly
- `CODEX_REVIEW_API_KEY` + `CODEX_REVIEW_API_BASE` — custom endpoint
- `codex login` — Codex CLI OAuth
- Auto-detects Hermes LiteLLM proxy from `~/.hermes/.env`

## OMP Integration

### As a pipe
```bash
omp_dump_context | codex-review --no-interactive
```

### Programmatic (Python)
```python
import subprocess
result = subprocess.run(
    ['codex-review', '--no-interactive'],
    input=session_text,
    capture_output=True, text=True, timeout=120
)
print(result.stdout)  # The review
```

### Cron (Hermes)
Create a Hermes cron job that runs:
```bash
codex-review --cron --context-file /tmp/omp_session.log --deliver telegram --verbose
```
Every N minutes, it collects new context, sends to the reviewer, and delivers the review.

## How Multi-Turn Works

1. Initial context sent to reviewer
2. If reviewer finds something interesting, it appends:
   ```
   NEED_CONTEXT: Show me the full auth.py file
   ```
3. Tool pulls more context (re-runs `--context-cmd`, greps for keywords, or reads more of the file)
4. Sends additional context back
5. Reviewer continues with full context
6. Repeats up to `--max-turns` (default: 3)

## Cron State Tracking

In `--cron` mode, the tool tracks:
- Content hash (to detect changes)
- Content length (to detect additions)
- Last review timestamp

On each cron tick:
- If context unchanged → skip
- If new content appended → review only the new portion
- `--force` overrides and re-reviews everything

## License

MIT
