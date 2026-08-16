#!/usr/bin/env python3
"""
claude-review v1.0 — Autonomous review bridge for CLI agents.

Same concept as codex-review but uses Claude Code CLI (or Anthropic API) as the
reviewer instead of Codex/OpenAI.

Sends recent agent work to Claude for a review. Claude can request more context
if it finds something interesting. Runs one-shot, multi-turn, or on a cron
schedule.

Usage:
  # One-shot: pipe context
  echo "I just refactored auth.py..." | claude-review

  # Multi-turn: Claude can ask for more context
  echo "..." | claude-review --multi-turn --context-cmd "cat /tmp/omp_session.log"

  # Cron mode: collect context from a file, review, deliver
  claude-review --cron --context-file /tmp/omp_session.log --deliver telegram

  # Focus the review
  echo "..." | claude-review --focus "security implications"

  # Use a specific model
  echo "..." | claude-review --model opus

  # API-only (skip Claude CLI, use Anthropic API directly)
  echo "..." | claude-review --api-only

Env vars:
  ANTHROPIC_API_KEY              Anthropic API key
  CLAUDE_REVIEW_API_KEY           Custom API key (overrides ANTHROPIC_API_KEY)
  CLAUDE_REVIEW_API_BASE           Custom API endpoint
  CLAUDE_REVIEW_MODEL              Model to use (default: claude-sonnet-4-20250514)
  CLAUDE_REVIEW_DELIVERY           telegram|file|omp|stdout (default: stdout)

Integrating with OMP:
  result = subprocess.run(['claude-review', '--no-interactive'],
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
import hashlib
from pathlib import Path
from textwrap import dedent

VERSION = "1.0.0"
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
        print(f"[claude-review] {msg}", file=sys.stderr)


def collect_context(args, query=None):
    """Collect context from file, command, or stdin."""
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

    if query and len(raw) > 5000:
        return _filter_context(raw, query)

    return raw


def _filter_context(raw, query):
    """Extract sections relevant to the query."""
    keywords = [w for w in query.lower().split() if len(w) > 3]
    lines = raw.split('\n')
    relevant = []

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

    log(f"No keyword matches for '{query}', returning tail")
    return truncate_context(raw)


def truncate_context(text, max_chars=MAX_CONTEXT_CHARS):
    if len(text) <= max_chars:
        return text
    head_size = int(max_chars * 0.2)
    tail_size = max_chars - head_size - 100
    return (
        text[:head_size]
        + f"\n\n[... {len(text) - head_size - tail_size} chars truncated ...]\n\n"
        + text[-tail_size:]
    )


def find_claude_cli():
    """Find the claude CLI binary."""
    return shutil.which("claude")


def call_claude_cli(prompt, model, timeout=120):
    """Use Claude Code CLI print mode for review.
    
    Claude -p is non-interactive, no PTY needed, no git repo required.
    Uses --append-system-prompt for the review system prompt.
    """
    claude_bin = find_claude_cli()
    if not claude_bin:
        return None, "claude CLI not found"

    cmd = [
        claude_bin, "-p",
        "--output-format", "text",
        "--max-turns", "1",
    ]
    if model:
        cmd.extend(["--model", model])

    # Use --append-system-prompt to inject our review persona
    cmd.extend(["--append-system-prompt", REVIEW_SYSTEM_PROMPT])
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )

        if result.returncode == 0:
            return result.stdout.strip(), None
        else:
            return None, f"claude exited {result.returncode}: {result.stderr.strip()[:300]}"
    except subprocess.TimeoutExpired:
        return None, f"claude timed out after {timeout}s"
    except Exception as e:
        return None, f"claude error: {e}"


def call_anthropic_api(messages, model, api_key, api_base, timeout=120):
    """Call Anthropic Messages API directly.
    
    Anthropic uses a different API format than OpenAI:
    - System prompt is a top-level parameter, not a message
    - Messages are simple {role, content} pairs
    - Endpoint: https://api.anthropic.com/v1/messages
    - Headers: x-api-key instead of Authorization Bearer
    - Requires anthropic-version header
    """
    # Extract system prompt from messages (first message if role=system)
    system_prompt = REVIEW_SYSTEM_PROMPT
    conversation_messages = []
    for msg in messages:
        if msg["role"] == "system":
            system_prompt = msg["content"]
        else:
            conversation_messages.append({"role": msg["role"], "content": msg["content"]})

    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": conversation_messages,
    }

    import urllib.request
    import urllib.error

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    try:
        req = urllib.request.Request(
            api_base,
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            # Anthropic returns content as a list of blocks
            content_blocks = result.get("content", [])
            text_parts = []
            for block in content_blocks:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            return "\n".join(text_parts), None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return None, f"API error {e.code}: {body[:500]}"
    except Exception as e:
        return None, f"API error: {e}"


def call_openai_api(messages, model, api_key, api_base, timeout=120):
    """Call an OpenAI-compatible chat completions endpoint (LiteLLM, vLLM, etc.)."""
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


def resolve_api_credentials(args):
    """Determine API key, base URL, model, and API format.
    
    Priority:
    1. Explicit args/env vars (CLAUDE_REVIEW_*)
    2. ANTHROPIC_API_KEY (Anthropic native API)
    3. Hermes .env for ANTHROPIC_API_KEY
    4. Hermes .env for LITELLM_API_KEY (OpenAI-compatible fallback)
    5. Claude CLI auth (~/.claude/ — OAuth, no API key needed for CLI)
    
    Returns: (api_key, api_base, model, api_format)
    api_format is "anthropic" or "openai" — determines which API call to use.
    """
    api_key = (
        os.environ.get("CLAUDE_REVIEW_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    api_base = os.environ.get("CLAUDE_REVIEW_API_BASE", "")
    model = args.model or os.environ.get("CLAUDE_REVIEW_MODEL", "")
    api_format = "anthropic"

    # Try Hermes .env for ANTHROPIC_API_KEY
    litellm_key = None
    litellm_base = None
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY=") and not api_key:
                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("LITELLM_API_KEY="):
                litellm_key = line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("LITELLM_BASE_URL="):
                litellm_base = line.split("=", 1)[1].strip().strip('"').strip("'")

    # If no Anthropic key, try LiteLLM proxy (OpenAI-compatible format)
    if not api_key and litellm_key:
        api_key = litellm_key
        api_base = litellm_base or "http://192.168.0.19:4000/v1/chat/completions"
        if not model:
            model = "ai01-glm5.2"
        api_format = "openai"
        log(f"Using LiteLLM proxy (OpenAI-compatible mode): {api_base} model={model}")

    if api_key and not api_base:
        api_base = "https://api.anthropic.com/v1/messages"
    if not model:
        model = "claude-sonnet-4-20250514"

    return api_key, api_base, model, api_format


def run_review(context, args, api_key, api_base, model, api_format="anthropic"):
    """Run the review, optionally multi-turn with context requests."""
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

        review = None
        error = None

        # Try Claude CLI first (if not api-only), then fall back to API
        if not args.api_only:
            # Build the full prompt from messages for CLI
            if turns == 1:
                full_prompt = REVIEW_USER_TEMPLATE.format(
                    context=truncate_context(context),
                    focus_line=focus_line,
                )
            else:
                # For multi-turn, include conversation history
                parts = []
                for m in messages:
                    if m["role"] != "system":
                        parts.append(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}")
                full_prompt = "\n\n".join(parts)

            review, error = call_claude_cli(full_prompt, model if not api_key else None, args.timeout)

        if review is None and api_key:
            if api_format == "anthropic":
                review, error = call_anthropic_api(messages, model, api_key, api_base, args.timeout)
            else:
                # OpenAI-compatible (LiteLLM, vLLM, etc.)
                review, error = call_openai_api(messages, model, api_key, api_base, args.timeout)

        if review is None:
            if error:
                log(f"Turn {turns} failed: {error}")
            return None, turns, context_requests, error

        # Check if Claude wants more context
        match = NEED_CONTEXT_PATTERN.search(review)
        if match and args.multi_turn and turns < max_turns:
            query = match.group(1).strip()
            log(f"Claude requested context: {query}")
            context_requests.append(query)

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
            return review, turns, context_requests, None

    return review, turns, context_requests, None


def deliver_review(review, args):
    """Deliver the review to the specified destination(s)."""
    destinations = args.deliver or os.environ.get("CLAUDE_REVIEW_DELIVERY", "stdout")

    for dest in destinations.split(","):
        dest = dest.strip().lower()

        if dest == "stdout":
            print(review)

        elif dest == "file":
            outpath = args.output or os.environ.get("CLAUDE_REVIEW_OUTPUT", "/tmp/claude-review-last.md")
            Path(outpath).write_text(review)
            log(f"Written to {outpath}")

        elif dest == "omp":
            omp_inbox = os.environ.get("OMP_INBOX", "/tmp/omp-inbox.md")
            with open(omp_inbox, "a") as f:
                f.write(f"\n\n---\n## Claude Review ({time.strftime('%Y-%m-%d %H:%M')})\n\n{review}\n")
            log(f"Delivered to OMP inbox: {omp_inbox}")

        elif dest == "telegram":
            outpath = "/tmp/claude-review-for-telegram.md"
            Path(outpath).write_text(f"📋 **Claude Review** ({time.strftime('%Y-%m-%d %H:%M')})\n\n{review}")
            log(f"Written to {outpath} (for Telegram delivery)")
            print(review)

        else:
            log(f"Unknown delivery target: {dest}")


def run_cron_mode(args, api_key, api_base, model, api_format="anthropic"):
    """Cron mode: collect, review, deliver, track state."""
    state_file = Path(args.state_file or "/tmp/claude-review-state.json")

    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, IOError):
            pass

    context = collect_context(args)
    if not context:
        log("No context available, nothing to review")
        return

    context_hash = hashlib.sha256(context.encode()).hexdigest()[:16]
    last_hash = state.get("last_context_hash")
    last_len = state.get("last_context_len", 0)

    if context_hash == last_hash and len(context) == last_len:
        if not args.force:
            log("Context unchanged since last review, skipping (use --force to override)")
            return

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

    review, turns, requests, error = run_review(review_context, args, api_key, api_base, model, api_format)

    if review is None:
        log(f"Review failed: {error}")
        return

    state["last_context_hash"] = context_hash
    state["last_context_len"] = len(context)
    state["last_review_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_review_turns"] = turns
    state["last_review_requests"] = requests
    state_file.write_text(json.dumps(state, indent=2))

    log(f"Review complete ({turns} turns, {len(requests)} context requests)")

    deliver_review(review, args)


def main():
    parser = argparse.ArgumentParser(
        description="Send recent work to Claude for review. Supports multi-turn context requests and cron scheduling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent("""
            Examples:
              # One-shot
              echo "refactored auth.py" | claude-review
              
              # Multi-turn (Claude can ask for more context)
              echo "..." | claude-review --multi-turn --context-cmd "cat /tmp/omp.log"
              
              # Cron mode (periodic auto-review)
              claude-review --cron --context-file /tmp/omp.log --deliver telegram,file
              
              # Force review even if context unchanged
              claude-review --cron --context-file /tmp/omp.log --force
              
              OMP integration:
                result = subprocess.run(['claude-review', '--no-interactive'],
                                        input=text, capture_output=True, text=True)
        """),
    )

    ctx_group = parser.add_argument_group("Context Sources")
    ctx_group.add_argument("--file", "-f", help="Read context from file")
    ctx_group.add_argument("--context-file", help="Read context from file (cron mode)")
    ctx_group.add_argument("--context-cmd", help="Shell command to collect context")

    rev_group = parser.add_argument_group("Review Options")
    rev_group.add_argument("--focus", help="Specific area to focus the review on")
    rev_group.add_argument("--model", "-m", help="Model to use (sonnet, opus, haiku, or full name)")
    rev_group.add_argument("--multi-turn", action="store_true", help="Allow Claude to request more context")
    rev_group.add_argument("--max-turns", type=int, default=3, help="Max review turns (default: 3)")
    rev_group.add_argument("--timeout", type=int, default=120, help="API timeout in seconds")
    rev_group.add_argument("--api-only", action="store_true", help="Skip Claude CLI, use Anthropic API directly")

    cron_group = parser.add_argument_group("Cron Mode")
    cron_group.add_argument("--cron", action="store_true", help="Run in cron mode")
    cron_group.add_argument("--state-file", help="State file (default: /tmp/claude-review-state.json)")
    cron_group.add_argument("--force", action="store_true", help="Force review even if context unchanged")

    del_group = parser.add_argument_group("Delivery")
    del_group.add_argument("--deliver", help="Where to send review: stdout,file,omp,telegram (comma-sep)")
    del_group.add_argument("--output", "-o", help="Output file path (for --deliver file)")

    parser.add_argument("--no-interactive", action="store_true", help="Don't offer interactive follow-up")
    parser.add_argument("--version", action="version", version=f"claude-review v{VERSION}")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print status to stderr")

    args = parser.parse_args()

    api_key, api_base, model, api_format = resolve_api_credentials(args)

    if args.file and not args.context_file:
        args.context_file = args.file

    if args.cron:
        run_cron_mode(args, api_key, api_base, model, api_format)
    else:
        context = collect_context(args)
        if not context:
            print("Error: no context provided. Pipe text via stdin, use --file, or --context-cmd.", file=sys.stderr)
            sys.exit(1)

        log(f"Context: {len(context)} chars", args.verbose)

        review, turns, requests, error = run_review(context, args, api_key, api_base, model, api_format)

        if review is None:
            print(f"Error: {error}", file=sys.stderr)
            print("\nTo fix:", file=sys.stderr)
            print("  1. Install & auth Claude CLI: npm install -g @anthropic-ai/claude-code && claude auth login", file=sys.stderr)
            print("  2. Or set ANTHROPIC_API_KEY env var", file=sys.stderr)
            print("  3. Or set CLAUDE_REVIEW_API_KEY + CLAUDE_REVIEW_API_BASE for custom endpoint", file=sys.stderr)
            sys.exit(1)

        if requests:
            log(f"Claude requested {len(requests)} context pull(s): {requests}", args.verbose)

        deliver_review(review, args)

        if not args.no_interactive and sys.stdin.isatty():
            print("\n" + "="*60, file=sys.stderr)
            print("Want to ask a follow-up? Type below (Ctrl+C to exit):", file=sys.stderr)
            print("="*60, file=sys.stderr)
            claude_bin = find_claude_cli()
            if claude_bin:
                try:
                    follow_cmd = [claude_bin, "-p", f"Previous review:\n{review}\n\nFollow-up question:"]
                    subprocess.run(follow_cmd, timeout=300)
                except (KeyboardInterrupt, subprocess.TimeoutExpired):
                    pass
            else:
                print("(Claude CLI needed for interactive mode)", file=sys.stderr)


if __name__ == "__main__":
    main()
