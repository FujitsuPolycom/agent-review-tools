# claude-review — Handoff Guide for OMP

You now have access to a tool called **claude-review**. It sends your recent work to Claude (via Claude Code CLI or Anthropic API) for a "here's what I see, take what's useful" review. Supports multi-turn (Claude can ask for more context) and cron mode (periodic auto-review of new work).

This is the companion to **codex-review** — same architecture, different reviewer. Use either or both for dual-perspective reviews.

---

## 1. Installation

### Already installed on this host (Hermes LXC)
```
/root/workspace/claude-review/claude_review.py
/usr/local/bin/claude-review  (symlink → above)
```
Verify:
```bash
claude-review --version
# Should print: claude-review v1.0.0
```

### Installing on a new host

```bash
# 1. Copy the files
scp -r /root/workspace/claude-review/ user@newhost:~/claude-review/

# 2. Make executable
chmod +x ~/claude-review/claude_review.py ~/claude-review/omp-claude-review.sh

# 3. Symlink to PATH
ln -sf ~/claude-review/claude_review.py /usr/local/bin/claude-review

# 4. Verify
claude-review --version
```

### Requirements
- Python 3.8+ (stdlib only — no pip installs needed)
- One auth method (see §2 below)
- Claude Code CLI is optional (print mode `-p` is used if available; API fallback otherwise)

---

## 2. Authentication

The tool auto-detects auth in this priority order:

| Priority | Method | How to set up |
|----------|--------|---------------|
| 1 | `ANTHROPIC_API_KEY` env var | `export ANTHROPIC_API_KEY=sk-ant-...` |
| 2 | `CLAUDE_REVIEW_API_KEY` + `CLAUDE_REVIEW_API_BASE` | Custom endpoint |
| 3 | Claude CLI OAuth | `claude auth login` (browser flow for Pro/Max) |
| 4 | Hermes LiteLLM proxy | Auto-detected from `~/.hermes/.env` (LITELLM_API_KEY) |
| 5 | Hermes .env `ANTHROPIC_API_KEY` | Auto-detected from `~/.hermes/.env` |

### This host (Hermes LXC)
The tool detects Hermes' LiteLLM proxy from `~/.hermes/.env`:
- Endpoint: `http://192.168.0.19:4000/v1/chat/completions` (OpenAI-compatible format)
- Key: `LITELLM_API_KEY` from `.env`
- Model: `ai01-glm5.2` (GLM 5.2 on ai01)
- API format: OpenAI-compatible (not native Anthropic)

### Using actual Claude
```bash
# Option A: Claude CLI (Pro/Max subscription)
npm install -g @anthropic-ai/claude-code
claude auth login  # browser OAuth flow

# Option B: Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...
claude-review --model sonnet  # uses native Anthropic Messages API
```

### Using a custom endpoint
```bash
export CLAUDE_REVIEW_API_KEY="your-key"
export CLAUDE_REVIEW_API_BASE="http://your-endpoint:port/v1/chat/completions"
export CLAUDE_REVIEW_MODEL="your-model-name"
```

---

## 3. Usage

### One-shot (simplest)
```bash
echo "I just refactored auth.py to use JWT tokens" | claude-review --no-interactive
```

### From a file
```bash
claude-review --no-interactive --file /tmp/my_session.md
```

### With a focus area
```bash
echo "..." | claude-review --no-interactive --focus "security implications"
```

### Multi-turn (Claude can request more context)
```bash
claude-review --no-interactive --multi-turn --max-turns 3 --context-file /tmp/omp_session.log
```
The reviewer may append `NEED_CONTEXT: <query>` to its response. The tool then:
1. Re-reads the context source
2. Filters for keywords from the query
3. Sends the relevant section back
4. Reviewer continues with the additional context
Up to `--max-turns` times.

### Verbose mode
```bash
echo "..." | claude-review --no-interactive -v
```

### All flags
```
--file, -f FILE          Read context from file
--context-file FILE      Same as --file (for cron mode)
--context-cmd CMD        Shell command to collect context
--focus TEXT             Focus the review on a specific area
--model, -m MODEL        Model: sonnet, opus, haiku, or full name
--multi-turn             Allow reviewer to request more context
--max-turns N            Max review turns (default: 3)
--timeout N               API timeout in seconds (default: 120)
--api-only                Skip Claude CLI, use API directly
--cron                    Cron mode (collect, review, deliver, track state)
--state-file FILE         State file (default: /tmp/claude-review-state.json)
--force                   Force review even if context unchanged
--deliver TARGET          stdout,file,omp,telegram (comma-sep)
--output, -o FILE         Output file path (for --deliver file)
--no-interactive          Don't offer interactive follow-up
--verbose, -v             Print status to stderr
--version                 Show version
```

---

## 4. Integration with OMP

### As a shell command
```
review = claude-review --no-interactive --context-file /tmp/omp_session.log
```

### Programmatic (Python)
```python
import subprocess

result = subprocess.run(
    ['claude-review', '--no-interactive', '--multi-turn', '-v',
     '--context-file', '/tmp/omp_session.log'],
    capture_output=True, text=True, timeout=120
)

if result.returncode == 0:
    review = result.stdout
    print(review)
else:
    print(f"Review failed: {result.stderr}", file=sys.stderr)
```

### Writing context for the tool
```python
# In OMP's main loop:
with open('/tmp/omp_session.log', 'a') as f:
    f.write(f"\n## {timestamp} — {task_name}\n{work_summary}\n")
```

### Delivery targets
| Target | What happens |
|--------|-------------|
| `stdout` | Review printed to stdout (default) |
| `file` | Written to `--output` (default: `/tmp/claude-review-last.md`) |
| `omp` | Appended to `/tmp/omp-inbox.md` |
| `telegram` | Written to `/tmp/claude-review-for-telegram.md` + stdout |

### Dual-review (Codex + Claude)
Run both tools for two independent perspectives:
```python
import subprocess

context = open('/tmp/omp_session.log').read()

# Codex review
codex = subprocess.run(['codex-review', '--no-interactive'],
                       input=context, capture_output=True, text=True, timeout=120)

# Claude review
claude = subprocess.run(['claude-review', '--no-interactive'],
                        input=context, capture_output=True, text=True, timeout=120)

# Compare perspectives
print("=== CODEX ===")
print(codex.stdout)
print("\n=== CLAUDE ===")
print(claude.stdout)
```

---

## 5. Cron Mode (Periodic Auto-Review)

### How it works
1. Collects context from `--context-file` or `--context-cmd`
2. Checks state file — if content unchanged, skips (unless `--force`)
3. If new content appended, reviews **only the new portion**
4. Delivers review to `--deliver` targets
5. Updates state file

### Running cron manually
```bash
claude-review --cron --context-file /tmp/omp_session.log --deliver telegram,file --output /tmp/claude-review-last.md -v
```

### Hermes cron job
```bash
hermes cron create \
  --schedule "every 30m" \
  --prompt "Run: claude-review --cron --context-file /tmp/omp_session.log --deliver telegram -v" \
  --deliver telegram
```

### System crontab
```bash
# crontab -e
*/30 * * * *  claude-review --cron --context-file /tmp/omp_session.log --deliver file --output /tmp/claude-review-last.md --no-interactive 2>> /var/log/claude-review.log
```

### State file
`/tmp/claude-review-state.json`:
```json
{
  "last_context_hash": "a1b2c3d4...",
  "last_context_len": 5421,
  "last_review_at": "2026-08-16 14:30:00",
  "last_review_turns": 2,
  "last_review_requests": ["Show me the full backup script"]
}
```
Delete to reset (forces full re-review).

---

## 6. Maintenance

### Common issues

| Symptom | Fix |
|---------|-----|
| `No API key found` | Set `ANTHROPIC_API_KEY` or use `claude auth login` |
| `API error 401` | Bad/expired key — re-authenticate |
| `API error 429` | Rate limited — increase `--timeout` or reduce cron frequency |
| `claude CLI not found` | `npm install -g @anthropic-ai/claude-code` (optional — API mode works without it) |
| Review is empty | Check context source exists; use `-v` for diagnostics |
| Cron keeps skipping | `rm /tmp/claude-review-state.json` |
| Wrong model | `--model sonnet` or `export CLAUDE_REVIEW_MODEL=sonnet` |

### Changing the reviewer model
```bash
# Claude models (native Anthropic API)
claude-review --model sonnet   # fast, balanced
claude-review --model opus     # deepest reasoning
claude-review --model haiku    # fastest, cheapest

# Via env var
export CLAUDE_REVIEW_MODEL=opus
```

---

## 7. Key Differences from codex-review

| | codex-review | claude-review |
|---|---|---|
| Reviewer | Codex / OpenAI | Claude / Anthropic |
| CLI tool | `codex exec` (needs git repo) | `claude -p` (works anywhere) |
| API format | OpenAI Chat Completions | Anthropic Messages API (or OpenAI-compat via LiteLLM) |
| Auth header | `Authorization: Bearer` | `x-api-key` (Anthropic) or `Authorization: Bearer` (OpenAI-compat) |
| Default model | `o4-mini` / `ai01-glm5.2` | `claude-sonnet-4-20250514` / `ai01-glm5.2` |
| State file | `/tmp/codex-review-state.json` | `/tmp/claude-review-state.json` |
| Env prefix | `CODEX_REVIEW_*` | `CLAUDE_REVIEW_*` |

Both tools share the same:
- NEED_CONTEXT protocol
- Cron state tracking (hash + length)
- Delivery targets (stdout/file/omp/telegram)
- Context collection (stdin/file/cmd)
- Multi-turn flow
- Review system prompt format

---

## 8. File Layout

```
/root/workspace/claude-review/
├── claude_review.py        # Main tool (Python 3, stdlib only)
├── omp-claude-review.sh    # Shell wrapper for OMP integration
└── README.md               # Project README
```

No external Python dependencies. No database. No daemon.

---

## 9. Quick Reference Card

```bash
# Quick review
claude-review --no-interactive --context-file /tmp/omp_session.log

# Multi-turn with context pulling
claude-review --no-interactive --multi-turn --context-file /tmp/omp_session.log -v

# Cron mode (auto-review new work)
claude-review --cron --context-file /tmp/omp_session.log --deliver telegram --no-interactive -v

# Force full re-review
claude-review --cron --context-file /tmp/omp_session.log --force --no-interactive

# Focus on security
echo "..." | claude-review --no-interactive --focus "security"

# Use Claude opus model
echo "..." | claude-review --no-interactive --model opus

# Reset cron state
rm /tmp/claude-review-state.json

# Dual review (both perspectives)
echo "..." | codex-review --no-interactive
echo "..." | claude-review --no-interactive
```
