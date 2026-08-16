#!/usr/bin/env python3
"""
codex-review v2 — Autonomous review bridge for CLI agents.

Sends recent agent work to Codex (or any LLM) for review. Codex can request
more context if it finds something interesting. Runs one-shot, multi-turn,
or on a cron schedule.

Usage:
  # One-shot: pipe context
  echo "I just refactored auth.py..." | codex-review

  # Multi-turn: Codex can ask for more context
  echo "..." | codex-review --multi-turn

  # Cron mode: collect context from a command, review, deliver
  codex-review --cron --context-cmd "cat /tmp/omp_session.log" --deliver telegram

  # With a context provider (pulls more context on demand)
  echo "..." | codex-review --multi-turn --context-cmd "cat /tmp/omp_session.log"

  # Focus the review
  echo "..." | codex-review --focus "security implications"

How multi-turn works:
  1. Send initial context to reviewer
  2. Reviewer responds with thoughts
  3. If response contains NEED_CONTEXT: <query>, tool pulls more context
     (runs --context-cmd, greps for the query, or reads more of the file)
  4. Sends additional context back to reviewer
  5. Repeat up to --max-turns times
  6. Final review is returned

Context collection:
  --context-cmd "shell command"   Output of this command = context
  --context-file /path/to/file     Read context from file
  --context-stdin                  Read from stdin (default)
  For multi-turn context requests:
  --context-cmd is used to pull MORE context when Codex asks for it.
  The command should output all available context; the tool greps/filter
  for what Codex specifically requested.

Env vars:
  OPENAI_API_KEY                    OpenAI API key
  CODEX_REVIEW_API_KEY              Custom API key
  CODEX_REVIEW_API_BASE              Custom API endpoint
  CODEX_REVIEW_MODEL                 Model to use (default: o4-mini)
  CODEX_REVIEW_DELIVERY              telegram|file|omp (default: stdout)

Integrating with OMP:
  1. Set up OMP to write its session to a file (e.g. /tmp/omp_session.log)
  2. Create a cron job:
     codex-review --cron --context-file /tmp/omp_session.log --deliver telegram
  3. Or have OMP call it directly:
     result = subprocess.run(['codex-review', '--no-interactive'],
                              input=session_text, capture_output=True, text=True)
"""

import argparse
import os
import subprocess
import sys
import json
import tempfile
import shutil
import re
import time
from pathlib import Path
from textwrap import dedent

VERSION = "2.0.0"
MAX_CONTEXT_CHARS = 50000
NEED_CONTEXT_PATTERN = re.compile(r'NEED_CONTEXT:\s*(.+?)(?:\n|$)', re.IGNORECASE)

REVIEW_SYSTEM_PROMPT = dedent("""\
    You are a code review partner for an autonomous CLI agent called OMP (Oh My Pi).
    OMP has sent you a summary of its recent work for your perspective.

    Review what OMP has done and provide your honest thoughts.

    Format your response as:

    ## What I See
    (Brief summary of what was done)

    ## Thoughts
    - Things that look good
    - Potential issues or risks
    - Suggestions or alternatives

    ## Take What's Useful
    (Final notes — the receiver is told to take what they find useful and ignore the rest)

    IMPORTANT: If you spot something that looks particularly interesting, risky, or
    concerning and you need more detail to give a proper review, append this line
    at the very end of your response:

    NEED_CONTEXT: <specific question about what you want to see>

    Examples:
    NEED_CONTEXT: Show me the full auth.py file
    NEED_CONTEXT: What does the error handling look like in the main loop?
    NEED_CONTEXT: Show me the git diff of the last 3 commits

    Only use NEED_CONTEXT if you genuinely need more detail. If you have enough
    to give a solid review, don't use it.

    Be concise but thorough. Focus on correctness, edge cases, security, and design.
    If the work looks solid, say so — don't manufacture criticism.
""")

REVIEW_USER_TEMPLATE = dedent("""\
    Here is a summary of recent work from OMP (Oh My Pi), a CLI agent.

    {focus_line}

    --- BEGIN AGENT CONTEXT ---
    {context}
    --- END AGENT CONTEXT ---
""")

FOLLOWUP_TEMPLATE = dedent("""\
    You asked for more context. Here's what I found:

    --- ADDITIONAL CONTEXT ---
    {additional}
    --- END ADDITIONAL CONTEXT ---

    Please continue your review with this new information.
""")


def log(msg, verbose=True):
    if verbose:
        print(f"[codex-review] {msg}", file=sys.stderr)


def collect_context(args, query=None):
    """Collect context from file, command, or stdin.
    
    If query is provided (for multi-turn context requests), try to pull
    relevant sections rather than the full context.
    """
    raw = None
    
    if args.context_cmd:
        log(f"Running context command: {args.context_cmd}")
        try:
            result = subprocess.run(
                args.context_cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            raw = result.stdout
        except Exception as e:
            log(f"Context command failed: {e}")
            return None
    elif args.context_file:
        path = Path(args.context_file)
        if not path.exists():
            log(f"Context file not found: {path}")
            return None
        raw = path.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        # Fallback to env-specified file
        for p in [
            os.environ.get("OMP_SESSION_FILE"),
            os.environ.get("AGENT_CONTEXT_FILE"),
            "/tmp/omp_session.log",
            "/tmp/omp_last_context.md",
        ]:
            if p and Path(p).exists():
                log(f"Using context from: {p}")
                raw = Path(p).read_text(encoding="utf-8")
                break
    
    if not raw or not raw.strip():
        return None
    
    # For multi-turn context requests, try to filter relevant sections
    if query and len(raw) > 5000:
        return _filter_context(raw, query)
    
    return raw


def _filter_context(raw, query):
    """Try to extract sections relevant to the query.
    
    Strategy:
    1. If context-cmd is set, re-run it with the query as an argument
    2. Otherwise, grep for keywords in the existing context
    3. Fall back to returning the full context (truncated)
    """
    # Simple keyword extraction from query
    keywords = [w for w in query.lower().split() if len(w) > 3]
    
    lines = raw.split('\n')
    relevant = []
    
    # Find lines matching keywords + surrounding context (±5 lines)
    for i, line in enumerate(lines):
        if any(kw in line.lower() for kw in keywords):
            start = max(0, i - 5)
            end = min(len(lines), i + 6)
            for j in range(start, end):
                if lines[j] not in relevant:
                    relevant.append(lines[j])
    
    if relevant:
        result = '\n'.join(relevant)
        log(f"Filtered context: {len(result)} chars from {len(raw)} (query: {query})")
        return result
    
    # No matches — return tail (most recent work)
    log(f"No keyword matches for '{query}', returning tail")
    return truncate_context(raw)


def truncate_context(text, max_chars=MAX_CONTEXT_CHARS):
    """Truncate to stay within token limits. Keep head and tail."""
    if len(text) <= max_chars:
        return text
    head_size = int(max_chars * 0.2)
    tail_size = max_chars - head_size - 100
    return (
        text[:head_size]
        + f"\n\n[... {len(text) - head_size - tail_size} chars truncated ...]\n\n"
        + text[-tail_size:]
    )


def find_codex_cli():
    """Find the codex CLI binary."""
    return shutil.which("codex")


def call_llm(messages, model, api_key, api_base, timeout=120):
    """Call an OpenAI-compatible chat completions endpoint."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
    }
    
    import urllib.request
    import urllib.error
    
    try:
        req = urllib.request.Request(
            api_base,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"], None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return None, f"API error {e.code}: {body[:500]}"
    except Exception as e:
        return None, f"API error: {e}"


def call_codex_cli(prompt, model, timeout=120):
    """Use codex CLI for review."""
    codex_bin = find_codex_cli()
    if not codex_bin:
        return None, "codex CLI not found"
    
    cmd = [codex_bin, "exec"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    
    try:
        tmpdir = tempfile.mkdtemp(prefix="codex-review-")
        subprocess.run(["git", "init", "-q"], cwd=tmpdir, capture_output=True, timeout=5)
        
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=tmpdir,
        )
        shutil.rmtree(tmpdir, ignore_errors=True)
        
        if result.returncode == 0:
            return result.stdout.strip(), None
        else:
            return None, f"codex exited {result.returncode}: {result.stderr.strip()[:300]}"
    except subprocess.TimeoutExpired:
        return None, f"codex timed out after {timeout}s"
    except Exception as e:
        return None, f"codex error: {e}"


def resolve_api_credentials(args):
    """Determine API key, base URL, and model.
    
    Priority:
    1. Explicit args/env vars
    2. OPENAI_API_KEY
    3. Hermes LiteLLM proxy (auto-detected from ~/.hermes/.env)
    4. Codex auth file
    """
    api_key = os.environ.get("CODEX_REVIEW_API_KEY") or os.environ.get("OPENAI_API_KEY")
    api_base = os.environ.get("CODEX_REVIEW_API_BASE", "")
    model = args.model or os.environ.get("CODEX_REVIEW_MODEL", "")
    
    # Try codex auth file
    if not api_key:
        for p in [Path.home() / ".codex" / "auth.json", Path.home() / ".config" / "codex" / "auth.json"]:
            if p.exists():
                try:
                    auth = json.loads(p.read_text())
                    api_key = auth.get("api_key") or auth.get("OPENAI_API_KEY")
                    if api_key:
                        break
                except (json.JSONDecodeError, IOError):
                    pass
    
    # Try Hermes .env for LiteLLM proxy
    if not api_key:
        env_path = Path.home() / ".hermes" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("LITELLM_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if line.startswith("LITELLM_BASE_URL="):
                    api_base = line.split("=", 1)[1].strip().strip('"').strip("'")
            if api_key and not api_base:
                api_base = "http://192.168.0.19:4000/v1/chat/completions"
            if api_key and not model:
                model = "ai01-glm5.2"
    
    if api_key and not api_base:
        api_base = "https://api.openai.com/v1/chat/completions"
    if not model:
        model = "o4-mini"
    
    return api_key, api_base, model


def run_review(context, args, api_key, api_base, model):
    """Run the review, optionally multi-turn with context requests.
    
    Returns (review_text, turns_used, context_requested)
    """
    messages = [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
    ]
    
    focus_line = f"Focus area: {args.focus}" if args.focus else ""
    user_msg = REVIEW_USER_TEMPLATE.format(
        context=truncate_context(context),
        focus_line=focus_line,
    )
    messages.append({"role": "user", "content": user_msg})
    
    context_requests = []
    turns = 0
    max_turns = args.max_turns if args.multi_turn else 1
    
    while turns < max_turns:
        turns += 1
        log(f"Turn {turns}/{max_turns}: sending {len(messages)} messages...")
        
        # Try codex CLI first (if not api-only), then fall back to API
        review = None
        error = None
        
        if not args.api_only and turns == 1:
            # Only use codex CLI for first turn (it's stateless per-call)
            full_prompt = "\n\n".join([m["content"] for m in messages])
            review, error = call_codex_cli(full_prompt, model if not api_key else None, args.timeout)
        
        if review is None and api_key:
            review, error = call_llm(messages, model, api_key, api_base, args.timeout)
        
        if review is None:
            if error:
                log(f"Turn {turns} failed: {error}")
            return None, turns, context_requests, error
        
        # Check if Codex wants more context
        match = NEED_CONTEXT_PATTERN.search(review)
        if match and args.multi_turn and turns < max_turns:
            query = match.group(1).strip()
            log(f"Codex requested context: {query}")
            context_requests.append(query)
            
            # Pull more context
            additional = collect_context(args, query=query)
            if additional:
                followup = FOLLOWUP_TEMPLATE.format(additional=truncate_context(additional, 20000))
                messages.append({"role": "assistant", "content": review})
                messages.append({"role": "user", "content": followup})
                log(f"Sent {len(followup)} chars of additional context")
            else:
                log(f"No additional context available for: {query}")
                messages.append({"role": "assistant", "content": review})
                messages.append({"role": "user", "content": f"No additional context available for your request: {query}\n\nPlease provide your final review based on what you have."})
        else:
            # No context request or max turns reached — we're done
            return review, turns, context_requests, None
    
    # Max turns reached
    return review, turns, context_requests, None


def deliver_review(review, args):
    """Deliver the review to the specified destination(s)."""
    destinations = args.deliver or os.environ.get("CODEX_REVIEW_DELIVERY", "stdout")
    
    for dest in destinations.split(","):
        dest = dest.strip().lower()
        
        if dest == "stdout":
            print(review)
        
        elif dest == "file":
            outpath = args.output or os.environ.get("CODEX_REVIEW_OUTPUT", "/tmp/codex-review-last.md")
            Path(outpath).write_text(review)
            log(f"Written to {outpath}")
        
        elif dest == "omp":
            # Write to OMP's inbox for OMP to read on next turn
            omp_inbox = os.environ.get("OMP_INBOX", "/tmp/omp-inbox.md")
            with open(omp_inbox, "a") as f:
                f.write(f"\n\n---\n## Codex Review ({time.strftime('%Y-%m-%d %H:%M')})\n\n{review}\n")
            log(f"Delivered to OMP inbox: {omp_inbox}")
        
        elif dest == "telegram":
            # Write to a file that a Hermes cron job can pick up and send
            outpath = "/tmp/codex-review-for-telegram.md"
            Path(outpath).write_text(f"📋 **Codex Review** ({time.strftime('%Y-%m-%d %H:%M')})\n\n{review}")
            log(f"Written to {outpath} (for Telegram delivery)")
            # Also print so Hermes cron can capture stdout
            print(review)
        
        else:
            log(f"Unknown delivery target: {dest}")


def run_cron_mode(args, api_key, api_base, model):
    """Cron mode: collect, review, deliver, track state to avoid re-reviewing."""
    state_file = Path(args.state_file or "/tmp/codex-review-state.json")
    
    # Load state
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    
    # Collect context
    context = collect_context(args)
    if not context:
        log("No context available, nothing to review")
        return
    
    # Check if context changed since last run
    import hashlib
    context_hash = hashlib.sha256(context.encode()).hexdigest()[:16]
    last_hash = state.get("last_context_hash")
    last_len = state.get("last_context_len", 0)
    
    # Skip if nothing new (same hash AND same length)
    if context_hash == last_hash and len(context) == last_len:
        if not args.force:
            log("Context unchanged since last review, skipping (use --force to override)")
            return
    
    # Only review the NEW portion if we have a previous state
    if last_hash and len(context) > last_len and not args.force:
        new_portion = context[last_len:]
        if new_portion.strip():
            log(f"Reviewing new content: {len(new_portion)} chars (skipping {last_len} already reviewed)")
            review_context = new_portion
        else:
            log("No new content since last review")
            return
    else:
        review_context = context
    
    log(f"Reviewing {len(review_context)} chars of context...")
    
    # Run the review
    review, turns, requests, error = run_review(review_context, args, api_key, api_base, model)
    
    if review is None:
        log(f"Review failed: {error}")
        return
    
    # Update state
    state["last_context_hash"] = context_hash
    state["last_context_len"] = len(context)
    state["last_review_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_review_turns"] = turns
    state["last_review_requests"] = requests
    state_file.write_text(json.dumps(state, indent=2))
    
    log(f"Review complete ({turns} turns, {len(requests)} context requests)")
    
    # Deliver
    deliver_review(review, args)


def main():
    parser = argparse.ArgumentParser(
        description="Send recent work to Codex for review. Supports multi-turn context requests and cron scheduling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent("""
            Examples:
              # One-shot
              echo "refactored auth.py" | codex-review
              
              # Multi-turn (Codex can ask for more context)
              echo "..." | codex-review --multi-turn --context-cmd "cat /tmp/omp.log"
              
              # Cron mode (periodic auto-review)
              codex-review --cron --context-file /tmp/omp.log --deliver telegram,file
              
              # Force review even if context unchanged
              codex-review --cron --context-file /tmp/omp.log --force
              
              OMP integration:
                result = subprocess.run(['codex-review', '--no-interactive'],
                                        input=text, capture_output=True, text=True)
        """),
    )
    
    # Context sources
    ctx_group = parser.add_argument_group("Context Sources")
    ctx_group.add_argument("--file", "-f", help="Read context from file")
    ctx_group.add_argument("--context-file", help="Read context from file (cron mode)")
    ctx_group.add_argument("--context-cmd", help="Shell command to collect context")
    
    # Review options
    rev_group = parser.add_argument_group("Review Options")
    rev_group.add_argument("--focus", help="Specific area to focus the review on")
    rev_group.add_argument("--model", "-m", help="Model to use")
    rev_group.add_argument("--multi-turn", action="store_true", help="Allow Codex to request more context")
    rev_group.add_argument("--max-turns", type=int, default=3, help="Max review turns (default: 3)")
    rev_group.add_argument("--timeout", type=int, default=120, help="API timeout in seconds")
    rev_group.add_argument("--api-only", action="store_true", help="Skip codex CLI, use API directly")
    
    # Cron mode
    cron_group = parser.add_argument_group("Cron Mode")
    cron_group.add_argument("--cron", action="store_true", help="Run in cron mode (collect, review, deliver, track state)")
    cron_group.add_argument("--state-file", help="State file for tracking reviewed content (default: /tmp/codex-review-state.json)")
    cron_group.add_argument("--force", action="store_true", help="Force review even if context unchanged")
    
    # Delivery
    del_group = parser.add_argument_group("Delivery")
    del_group.add_argument("--deliver", help="Where to send review: stdout,file,omp,telegram (comma-sep)")
    del_group.add_argument("--output", "-o", help="Output file path (for --deliver file)")
    
    # Misc
    parser.add_argument("--no-interactive", action="store_true", help="Don't offer interactive follow-up")
    parser.add_argument("--version", action="version", version=f"codex-review v{VERSION}")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print status to stderr")
    
    args = parser.parse_args()
    
    # Resolve credentials
    api_key, api_base, model = resolve_api_credentials(args)
    
    # For backwards compat, map --file to --context-file
    if args.file and not args.context_file:
        args.context_file = args.file
    
    if args.cron:
        run_cron_mode(args, api_key, api_base, model)
    else:
        # One-shot mode
        context = collect_context(args)
        if not context:
            print("Error: no context provided. Pipe text via stdin, use --file, or --context-cmd.", file=sys.stderr)
            sys.exit(1)
        
        log(f"Context: {len(context)} chars", args.verbose)
        
        review, turns, requests, error = run_review(context, args, api_key, api_base, model)
        
        if review is None:
            print(f"Error: {error}", file=sys.stderr)
            print("\nTo fix:", file=sys.stderr)
            print("  1. Install & auth codex CLI: npm install -g @openai/codex && codex login", file=sys.stderr)
            print("  2. Or set OPENAI_API_KEY env var", file=sys.stderr)
            print("  3. Or set CODEX_REVIEW_API_KEY + CODEX_REVIEW_API_BASE for custom endpoint", file=sys.stderr)
            sys.exit(1)
        
        if requests:
            log(f"Codex requested {len(requests)} context pull(s): {requests}", args.verbose)
        
        deliver_review(review, args)
        
        # Interactive follow-up
        if not args.no_interactive and sys.stdin.isatty():
            print("\n" + "="*60, file=sys.stderr)
            print("Want to ask a follow-up? Type below (Ctrl+C to exit):", file=sys.stderr)
            print("="*60, file=sys.stderr)
            codex_bin = find_codex_cli()
            if codex_bin:
                try:
                    follow_cmd = [codex_bin, "exec", f"Previous review:\n{review}\n\nFollow-up question:"]
                    subprocess.run(follow_cmd, timeout=300)
                except (KeyboardInterrupt, subprocess.TimeoutExpired):
                    pass


if __name__ == "__main__":
    main()
