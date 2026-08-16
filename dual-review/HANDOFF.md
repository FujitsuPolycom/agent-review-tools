# dual-review — Handoff Guide for OMP

You now have access to **dual-review** — a two-pass review tool that chains Fable (Claude) and Sol (Codex) for deeper analysis than either reviewer alone.

## Pipeline

```
Context → Fable (Claude) → Fable's review
                                    ↓
         (Original context + Fable's review) → Sol (Codex) → Sol's final review → caller
```

1. **Pass 1 — Fable (Claude):** Reviews the work. Focuses on architectural analysis, design issues, subtle edge cases.
2. **Pass 2 — Sol (Codex):** Sees Fable's review + the original context. Agrees/disagrees with Fable, catches what Fable missed, adds its own perspective, gives the final verdict.
3. **Output:** Sol's final review goes to the caller. Use `--show-both` to also see Fable's pass 1.

## Installation

### Already installed on Hermes LXC
```
/root/workspace/dual-review/dual_review.py
/usr/local/bin/dual-review  (symlink)
```

### Depends on
```
/root/workspace/codex-review/codex_review.py   (Sol's engine)
/root/workspace/claude-review/claude_review.py   (Fable's engine)
```
All three directories must be siblings (or the tool searches within its own dir).

### Installing on a new host
```bash
# Copy all three tools
scp -r /root/workspace/codex-review/  user@host:~/codex-review/
scp -r /root/workspace/claude-review/ user@host:~/claude-review/
scp -r /root/workspace/dual-review/    user@host:~/dual-review/

# Make executable & symlink
chmod +x ~/dual-review/dual_review.py
ln -sf ~/dual-review/dual_review.py /usr/local/bin/dual-review

# Verify
dual-review --version
```

### Requirements
- Python 3.8+ (stdlib only)
- codex-review and claude-review installed as sibling directories
- Auth for both Claude (Anthropic/LiteLLM) and Codex (OpenAI/LiteLLM)

## Usage

### One-shot
```bash
echo "I refactored auth.py to use JWT..." | dual-review --no-interactive
```

### Multi-turn (Sol can request more context in pass 2)
```bash
dual-review --no-interactive --multi-turn --context-file /tmp/omp_session.log
```

### See both reviews (Fable + Sol)
```bash
echo "..." | dual-review --no-interactive --show-both -v
```

### Cron mode
```bash
dual-review --cron --context-file /tmp/omp_session.log --deliver telegram -v
```

### Per-pass model overrides
```bash
echo "..." | dual-review --no-interactive --fable-model opus --sol-model o4-mini
```

### All flags
```
--file, -f FILE              Read context from file
--context-file FILE          Same as --file (cron mode)
--context-cmd CMD            Shell command to collect context
--focus TEXT                 Focus area for both reviewers
--multi-turn                 Allow Sol to request more context in pass 2
--max-turns N                Max Sol turns (default: 3)
--timeout N                   API timeout per pass (default: 120)
--show-both                   Include Fable's pass 1 in output
--fable-model MODEL           Model for Fable/Claude pass
--sol-model MODEL             Model for Sol/Codex pass
--fable-api-only              Skip Claude CLI, use API for Fable
--sol-api-only                Skip Codex CLI, use API for Sol
--cron                        Cron mode (collect, review, deliver, track state)
--state-file FILE             State file (default: /tmp/dual-review-state.json)
--force                       Force review even if unchanged
--deliver TARGET              stdout,file,omp,telegram (comma-sep)
--output, -o FILE             Output file path
--no-interactive              No interactive follow-up
--verbose, -v                 Show both passes in stderr
--version                     Show version
```

## OMP Integration

### Programmatic
```python
import subprocess

result = subprocess.run(
    ['dual-review', '--no-interactive', '--multi-turn', '-v',
     '--context-file', '/tmp/omp_session.log'],
    capture_output=True, text=True, timeout=300  # 2x timeout for two passes
)

if result.returncode == 0:
    review = result.stdout  # Sol's final review
    print(review)
else:
    print(f"Dual review failed: {result.stderr}")
```

### Delivery targets
| Target | What happens |
|--------|-------------|
| `stdout` | Sol's final review to stdout (default) |
| `file` | Written to `--output` (default: `/tmp/dual-review-last.md`) |
| `omp` | Appended to `/tmp/omp-inbox.md` |
| `telegram` | Written to `/tmp/dual-review-for-telegram.md` + stdout |

## Cron Mode

Same state tracking as the individual tools:
- Content hash + length in `/tmp/dual-review-state.json`
- Skips if unchanged (unless `--force`)
- Reviews only new content if appended
- `rm /tmp/dual-review-state.json` to reset

### Hermes cron
```bash
hermes cron create \
  --schedule "every 30m" \
  --prompt "Run: dual-review --cron --context-file /tmp/omp_session.log --deliver telegram -v" \
  --deliver telegram
```

## How the Two Passes Work

### Pass 1: Fable (Claude)
- System prompt introduces "Fable" persona
- Told this is pass 1 of 2, Sol will review next
- Asked to focus on deep reasoning, architectural issues, subtle design problems
- Has a "Notes for Sol" section in output
- Can use `NEED_CONTEXT:` but requests are noted, not pulled (single-pass)

### Pass 2: Sol (Codex)
- System prompt introduces "Sol" persona
- Sees: original context + Fable's complete review
- Asked to: agree/disagree with Fable, catch what was missed, add own perspective
- Has explicit sections: "Agreement with Fable", "Additional Findings", "Final Assessment"
- Supports multi-turn `NEED_CONTEXT:` with context pulling (if `--multi-turn`)
- Sol's output is the final review delivered to caller

### Fallback behavior
If Sol (Codex) fails in pass 2, the tool returns Fable's review with a caveat note.

## File Layout
```
/root/workspace/dual-review/
├── dual_review.py          # Main tool (imports from both sibling tools)
├── HANDOFF.md               # This file
```

## Quick Reference
```bash
# Quick dual review
dual-review --no-interactive --context-file /tmp/omp_session.log

# Multi-turn with context pulling for Sol
dual-review --no-interactive --multi-turn --context-file /tmp/omp_session.log -v

# See both Fable and Sol reviews
dual-review --no-interactive --show-both -v

# Cron mode
dual-review --cron --context-file /tmp/omp_session.log --deliver telegram -v

# Force re-review
dual-review --cron --context-file /tmp/omp_session.log --force

# Focus on security
echo "..." | dual-review --no-interactive --focus "security"

# Reset cron state
rm /tmp/dual-review-state.json
```
