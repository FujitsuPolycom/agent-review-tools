# agent-review-tools

A suite of CLI tools that let any agent (OMP, Hermes, or any CLI bot) get LLM-powered code/work reviews. Three tools, composable into a two-pass pipeline.

## Tools

| Tool | Reviewer | What it does |
|------|----------|-------------|
| `codex-review` | Sol (Codex / OpenAI) | One-shot or multi-turn review |
| `claude-review` | Fable (Claude / Anthropic) | One-shot or multi-turn review |
| `dual-review` | Fable → Sol | Two-pass: Fable reviews first, Sol reviews Fable's review + original context, Sol's final verdict goes to caller |

## Pipeline

```
                    ┌─────────────┐
                    │   Context    │
                    │ (OMP's work)  │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
      ┌───────────┐ ┌───────────┐ ┌───────────┐
      │codex-review│ │claude-    │ │dual-review│
      │   (Sol)   │ │review     │ │           │
      │           │ │(Fable)    │ │           │
      └─────┬─────┘ └─────┬─────┘ └─────┬─────┘
            │             │             │
            ▼             │             ▼
      ┌───────────┐       │      ┌───────────┐
      │ Sol's     │       │      │ Pass 1:   │
      │ review    │       │      │ Fable     │
      └───────────┘       │      │ reviews   │
                          │      └─────┬─────┘
                  ┌───────────┐        │
                  │ Fable's   │        ▼
                  │ review    │  ┌───────────┐
                  └───────────┘  │ Pass 2:   │
                                 │ Sol sees  │
                                 │ Fable +   │
                                 │ original  │
                                 └─────┬─────┘
                                       │
                                       ▼
                                 ┌───────────┐
                                 │ Sol's     │
                                 │ FINAL     │
                                 │ review    │
                                 └───────────┘
```

## Features (all three tools)

- **NEED_CONTEXT protocol**: Reviewer can request more context mid-review. Tool pulls relevant sections from the context source and sends them back. Up to N turns.
- **Cron mode**: Periodic auto-review with state tracking. Only reviews new content. Skips if unchanged. `--force` overrides.
- **Delivery**: stdout, file, OMP inbox, or Telegram (via Hermes cron).
- **Flexible backends**: CLI tools (Codex CLI, Claude CLI) or APIs (OpenAI, Anthropic, any OpenAI-compatible endpoint like LiteLLM/vLLM).
- **Zero dependencies**: Python 3.8+ stdlib only. No pip installs. No database. No daemon.

## Quick Start

```bash
# One-shot review (any tool)
echo "I refactored auth.py to use JWT..." | codex-review --no-interactive
echo "I refactored auth.py to use JWT..." | claude-review --no-interactive
echo "I refactored auth.py to use JWT..." | dual-review --no-interactive

# Multi-turn (reviewer can ask for more context)
echo "..." | dual-review --multi-turn --context-cmd "cat /tmp/omp_session.log" -v

# Cron mode (periodic auto-review of new work)
dual-review --cron --context-file /tmp/omp_session.log --deliver telegram -v

# See both Fable and Sol reviews
echo "..." | dual-review --no-interactive --show-both -v

# Focus on a specific area
echo "..." | dual-review --no-interactive --focus "security"
```

## Installation

```bash
# Clone
git clone git@github.com:FujitsuPolycom/agent-review-tools.git
cd agent-review-tools

# Install all three
chmod +x codex-review/codex_review.py claude-review/claude_review.py dual-review/dual_review.py
ln -sf $(pwd)/codex-review/codex_review.py /usr/local/bin/codex-review
ln -sf $(pwd)/claude-review/claude_review.py /usr/local/bin/claude-review
ln -sf $(pwd)/dual-review/dual_review.py /usr/local/bin/dual-review

# Verify
codex-review --version   # v2.0.0
claude-review --version   # v1.0.0
dual-review --version     # v1.0.0
```

### Requirements
- Python 3.8+ (stdlib only)
- One or more auth methods (see below)

## Authentication

### codex-review (Sol)
| Method | Setup |
|--------|-------|
| OpenAI API | `export OPENAI_API_KEY=sk-...` |
| Codex CLI OAuth | `npm install -g @openai/codex && codex login` |
| Custom endpoint | `export CODEX_REVIEW_API_KEY=... CODEX_REVIEW_API_BASE=...` |
| LiteLLM proxy | Auto-detected from `~/.hermes/.env` |

### claude-review (Fable)
| Method | Setup |
|--------|-------|
| Anthropic API | `export ANTHROPIC_API_KEY=sk-ant-...` |
| Claude CLI OAuth | `npm install -g @anthropic-ai/claude-code && claude auth login` |
| Custom endpoint | `export CLAUDE_REVIEW_API_KEY=... CLAUDE_REVIEW_API_BASE=...` |
| LiteLLM proxy | Auto-detected from `~/.hermes/.env` (OpenAI-compatible fallback) |

### dual-review
Requires auth for **both** Sol and Fable. Uses codex-review's and claude-review's auth resolution independently.

## NEED_CONTEXT Protocol

When a reviewer spots something interesting and wants more detail, it appends:

```
NEED_CONTEXT: Show me the full auth.py file
```

The tool then:
1. Re-reads the context source (file or command output)
2. Filters for keywords from the request (±5 lines context)
3. Sends the relevant section back to the reviewer
4. Reviewer continues with the additional context
5. Repeats up to `--max-turns` (default: 3)

In `dual-review`, only Sol (pass 2) gets multi-turn capability. Fable (pass 1) is single-pass — its context requests are noted but not pulled, since Sol will have the full context anyway.

## Cron Mode

All three tools support `--cron` with identical state tracking:

```json
// /tmp/dual-review-state.json (or codex-review / claude-review)
{
  "last_context_hash": "a1b2c3d4...",
  "last_context_len": 5421,
  "last_review_at": "2026-08-16 14:30:00",
  "last_review_turns": 2,
  "last_review_requests": ["Show me the full auth.py file"]
}
```

- **Unchanged** → skips (no wasted API calls)
- **New content appended** → reviews only the new portion
- `--force` → re-reviews everything
- Delete state file to reset

### Hermes cron setup
```bash
hermes cron create \
  --schedule "every 30m" \
  --prompt "Run: dual-review --cron --context-file /tmp/omp_session.log --deliver telegram -v" \
  --deliver telegram
```

### System crontab
```bash
*/30 * * * * dual-review --cron --context-file /tmp/omp_session.log --deliver file --output /tmp/dual-review-last.md --no-interactive 2>> /var/log/dual-review.log
```

## OMP Integration

### Shell command
```
review = dual-review --no-interactive --context-file /tmp/omp_session.log
```

### Python subprocess
```python
import subprocess

result = subprocess.run(
    ['dual-review', '--no-interactive', '--multi-turn', '-v',
     '--context-file', '/tmp/omp_session.log'],
    capture_output=True, text=True, timeout=300
)

if result.returncode == 0:
    review = result.stdout  # Sol's final review
    print(review)
```

### Writing context for the tools to pick up
```python
# In OMP's main loop, after completing work:
with open('/tmp/omp_session.log', 'a') as f:
    f.write(f"\n## {timestamp} — {task_name}\n{work_summary}\n")
```

## Per-Pass Configuration (dual-review only)

```bash
# Use specific models for each pass
dual-review --fable-model opus --sol-model o4-mini

# Skip CLI tools, use APIs directly
dual-review --fable-api-only --sol-api-only

# Only Sol gets multi-turn (Fable is always single-pass in dual mode)
dual-review --multi-turn --max-turns 3
```

## File Layout

```
agent-review-tools/
├── README.md                    # This file
├── codex-review/
│   ├── codex_review.py          # Sol's review engine
│   ├── omp-codex-review.sh      # Shell wrapper
│   └── HANDOFF.md               # Detailed handoff for OMP
├── claude-review/
│   ├── claude_review.py          # Fable's review engine
│   ├── omp-claude-review.sh      # Shell wrapper
│   └── HANDOFF.md               # Detailed handoff for OMP
├── dual-review/
│   ├── dual_review.py            # Two-pass pipeline (imports both above)
│   └── HANDOFF.md               # Detailed handoff for OMP
└── LICENSE                       # MIT
```

`dual-review` imports from its sibling directories. All three must be in the same parent directory (or the tool searches within its own directory for siblings).

## License

MIT
