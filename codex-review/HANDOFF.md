# codex-review — Handoff Guide for OMP

You now have access to a tool called **codex-review**. It sends your recent work to an LLM reviewer (Codex, OpenAI, or any OpenAI-compatible endpoint) and gets back a "here's what I see, take what's useful" review. It supports multi-turn (reviewer can ask for more context) and cron mode (periodic auto-review of new work).

---

## 1. Installation

### Already installed on this host
The tool is installed at:
```
/root/workspace/codex-review/codex_review.py
/usr/local/bin/codex-review  (symlink → above)
```
Verify:
```bash
codex-review --version
# Should print: codex-review v2.0.0
```

### Installing on a new host

```bash
# 1. Copy the files
scp -r /root/workspace/codex-review/ user@newhost:~/codex-review/

# 2. Make executable
chmod +x ~/codex-review/codex_review.py ~/codex-review/omp-codex-review.sh

# 3. Symlink to PATH
ln -sf ~/codex-review/codex_review.py /usr/local/bin/codex-review

# 4. Verify
codex-review --version
```

### Requirements
- Python 3.8+ (stdlib only — no pip installs needed)
- One auth method (see §2 below)
- `git` installed (only needed if using Codex CLI backend; API mode doesn't need it)

---

## 2. Authentication

The tool auto-detects auth in this priority order:

| Priority | Method | How to set up |
|----------|--------|---------------|
| 1 | `OPENAI_API_KEY` env var | `export OPENAI_API_KEY=sk-...` |
| 2 | `CODEX_REVIEW_API_KEY` + `CODEX_REVIEW_API_BASE` | Custom endpoint (LiteLLM, vLLM, etc.) |
| 3 | Codex CLI OAuth | `codex login --device-auth` (opens browser flow) |
| 4 | Hermes LiteLLM proxy | Auto-detected from `~/.hermes/.env` (LITELLM_API_KEY + LITELLM_BASE_URL) |
| 5 | Codex auth file | `~/.codex/auth.json` with `api_key` field |

### This host is already configured
The tool detects Hermes' LiteLLM proxy from `~/.hermes/.env`:
- Endpoint: `http://192.168.0.19:4000/v1/chat/completions`
- Key: `LITELLM_API_KEY` from `.env`
- Model: `ai01-glm5.2` (GLM 5.2 on ai01)

To switch to OpenAI directly:
```bash
export OPENAI_API_KEY=sk-...
codex-review --model gpt-4o "echo test context" 
```

To use a custom endpoint:
```bash
export CODEX_REVIEW_API_KEY="your-key"
export CODEX_REVIEW_API_BASE="http://your-endpoint:port/v1/chat/completions"
export CODEX_REVIEW_MODEL="your-model-name"
```

### Codex CLI (optional, gives access to GPT-5.6-sol)
```bash
npm install -g @openai/codex
codex login --device-auth
# Opens a browser flow — enter the code at https://auth.openai.com/codex/device
```
If Codex CLI is authed, the tool uses it for the first turn (richer context handling), then falls back to API for multi-turn.

---

## 3. Usage

### One-shot (simplest)
```bash
echo "I just refactored auth.py to use JWT tokens instead of session cookies" | codex-review --no-interactive
```
Output goes to stdout. Errors go to stderr.

### From a file
```bash
codex-review --no-interactive --file /tmp/my_session.md
```

### With a focus area
```bash
echo "..." | codex-review --no-interactive --focus "security implications of the JWT change"
```

### Multi-turn (reviewer can request more context)
```bash
codex-review --no-interactive --multi-turn --max-turns 3 --context-file /tmp/omp_session.log
```
The reviewer may append `NEED_CONTEXT: <query>` to its response. The tool then:
1. Re-reads the context source (file or command output)
2. Filters for keywords from the query
3. Sends the relevant section back
4. Reviewer continues with the additional context
This repeats up to `--max-turns` times.

### Verbose mode (status to stderr)
```bash
echo "..." | codex-review --no-interactive -v
```

### All flags
```
--file, -f FILE          Read context from file
--context-file FILE      Same as --file (for cron mode)
--context-cmd CMD        Shell command to collect context (output = context)
--focus TEXT             Focus the review on a specific area
--model, -m MODEL        Model to use (default: auto-detected)
--multi-turn             Allow reviewer to request more context
--max-turns N            Max review turns (default: 3)
--timeout N               API timeout in seconds (default: 120)
--api-only                Skip Codex CLI, use API directly
--cron                    Cron mode (collect, review, deliver, track state)
--state-file FILE         State file for cron tracking (default: /tmp/codex-review-state.json)
--force                   Force review even if context unchanged (cron mode)
--deliver TARGET          Where to send review: stdout,file,omp,telegram (comma-sep)
--output, -o FILE         Output file path (for --deliver file)
--no-interactive          Don't offer interactive follow-up (use for automation)
--verbose, -v             Print status to stderr
--version                 Show version
```

---

## 4. Integration with OMP

### As a shell command
Add to OMP's command config:
```
review = codex-review --no-interactive --context-file /tmp/omp_session.log
```

### Programmatic (Python)
```python
import subprocess

# Send your recent work for review
result = subprocess.run(
    ['codex-review', '--no-interactive', '--multi-turn', '-v',
     '--context-file', '/tmp/omp_session.log'],
    capture_output=True, text=True, timeout=120
)

if result.returncode == 0:
    review = result.stdout
    print(review)  # "## What I See\n## Thoughts\n## Take What's Useful"
else:
    print(f"Review failed: {result.stderr}", file=sys.stderr)
```

### Writing context for the tool to pick up
OMP should write its session/work log to a file that codex-review can read:
```python
# In OMP's main loop, after completing work:
with open('/tmp/omp_session.log', 'a') as f:
    f.write(f"\n## {timestamp} — {task_name}\n{work_summary}\n")
```

### Delivery targets
| Target | What happens |
|--------|-------------|
| `stdout` | Review printed to stdout (default) |
| `file` | Written to `--output` path (default: `/tmp/codex-review-last.md`) |
| `omp` | Appended to `/tmp/omp-inbox.md` (OMP reads this on next turn) |
| `telegram` | Written to `/tmp/codex-review-for-telegram.md` + stdout (Hermes cron picks up) |

---

## 5. Cron Mode (Periodic Auto-Review)

### How it works
1. Collects context from `--context-file` or `--context-cmd`
2. Checks state file — if content unchanged since last run, skips (unless `--force`)
3. If new content was appended, reviews **only the new portion**
4. Delivers review to `--deliver` targets
5. Updates state file with new hash/length/timestamp

### Running cron manually
```bash
codex-review --cron --context-file /tmp/omp_session.log --deliver telegram,file --output /tmp/codex-review-last.md -v
```

### Setting up a Hermes cron job
```bash
# In Hermes, create a cron job:
hermes cron create \
  --schedule "every 30m" \
  --prompt "Run: codex-review --cron --context-file /tmp/omp_session.log --deliver telegram -v" \
  --deliver telegram
```

### Setting up a system cron (if not using Hermes)
```bash
# crontab -e
*/30 * * * *  codex-review --cron --context-file /tmp/omp_session.log --deliver file --output /tmp/codex-review-last.md --no-interactive 2>> /var/log/codex-review.log
```

### State file
Located at `/tmp/codex-review-state.json` (configurable via `--state-file`):
```json
{
  "last_context_hash": "a1b2c3d4...",
  "last_context_len": 5421,
  "last_review_at": "2026-08-16 14:30:00",
  "last_review_turns": 2,
  "last_review_requests": ["Show me the full auth.py file"]
}
```
Delete this file to reset state (forces full re-review on next cron run).

---

## 6. Maintenance

### Updating the tool
The source is at `/root/workspace/codex-review/codex_review.py`. To update:
```bash
# Edit the file, then test
codex-review --version
echo "test" | codex-review --no-interactive --api-only -v
```

### Common issues

| Symptom | Fix |
|---------|-----|
| `No API key found` | Set `OPENAI_API_KEY` or `CODEX_REVIEW_API_KEY` env var, or `codex login` |
| `API error 401` | Bad/expired API key — re-authenticate |
| `API error 429` | Rate limited — increase `--timeout` or reduce cron frequency |
| `codex CLI not found` | `npm install -g @openai/codex` (optional — API mode works without it) |
| Review is empty | Check if context source exists and has content; use `-v` for diagnostics |
| Cron keeps skipping | Delete state file: `rm /tmp/codex-review-state.json` |
| Wrong model | Set `--model` or `CODEX_REVIEW_MODEL` env var |

### Changing the reviewer model
```bash
# Temporarily
echo "..." | codex-review --model gpt-4o

# Permanently (env var)
export CODEX_REVIEW_MODEL=gpt-4o

# Or edit ~/.hermes/.env to change LITELLM model
```

---

## 7. The NEED_CONTEXT Protocol

When the reviewer finds something interesting and wants more detail, it appends to its response:
```
NEED_CONTEXT: Show me the full auth.py file
```

### How the tool handles it
1. Parses the `NEED_CONTEXT:` line
2. Extracts keywords from the request
3. Re-reads the context source (file or command output)
4. Greps for lines matching the keywords (±5 lines context)
5. Sends matching sections back to the reviewer
6. Reviewer continues its review with the new info

### If no matches found
The tool tells the reviewer "No additional context available" and asks for a final review based on what it has.

### Limiting context requests
Use `--max-turns` to cap how many times the reviewer can ask for more:
```bash
codex-review --multi-turn --max-turns 2  # Max 2 context requests
```

---

## 8. File Layout

```
/root/workspace/codex-review/
├── codex_review.py        # Main tool (Python 3, stdlib only)
├── omp-codex-review.sh    # Shell wrapper for OMP integration
└── README.md               # Project README
```

No external Python dependencies. No database. No daemon. Just a script.

---

## 9. Quick Reference Card

```bash
# Quick review of current work
codex-review --no-interactive --context-file /tmp/omp_session.log

# Multi-turn with context pulling
codex-review --no-interactive --multi-turn --context-file /tmp/omp_session.log -v

# Cron mode (auto-review new work)
codex-review --cron --context-file /tmp/omp_session.log --deliver telegram --no-interactive -v

# Force full re-review
codex-review --cron --context-file /tmp/omp_session.log --force --no-interactive

# Focus on security
echo "..." | codex-review --no-interactive --focus "security"

# Use specific model
echo "..." | codex-review --no-interactive --model gpt-4o

# Reset cron state
rm /tmp/codex-review-state.json
```
