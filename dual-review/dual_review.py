#!/usr/bin/env python3
"""
dual-review v1.0 — Two-pass review: Fable (Claude) reviews first, then Sol (Codex)
reviews Fable's review + original context. Sol's final review goes to the caller.

Pipeline:
  1. Context → Fable (Claude) → Fable's review
  2. (Original context + Fable's review) → Sol (Codex) → Sol's final review
  3. Sol's review → caller

Usage:
  # One-shot
  echo "I refactored auth.py..." | dual-review --no-interactive

  # Multi-turn (Sol can request more context in pass 2)
  echo "..." | dual-review --multi-turn --context-cmd "cat /tmp/omp.log"

  # Cron mode
  dual-review --cron --context-file /tmp/omp.log --deliver telegram

  # Verbose (see both passes)
  echo "..." | dual-review --no-interactive -v

  # Show both reviews (Fable + Sol) instead of just Sol's final
  echo "..." | dual-review --no-interactive --show-both

Env vars:
  All CLAUDE_REVIEW_* and CODEX_REVIEW_* vars from the individual tools apply.
  Additionally:
  DUAL_REVIEW_DELIVERY    telegram|file|omp|stdout (default: stdout)
  DUAL_REVIEW_OUTPUT       Output file path for --deliver file

Integrating with OMP:
  result = subprocess.run(['dual-review', '--no-interactive'],
                          input=session_text, capture_output=True, text=True)
"""

import argparse
import os
import sys
import json
import time
import hashlib
from pathlib import Path
from textwrap import dedent

VERSION = "1.0.0"

# Import functions from the individual review tools
SCRIPT_DIR = Path(__file__).resolve().parent
CODEX_REVIEW_DIR = SCRIPT_DIR.parent / "codex-review"
CLAUDE_REVIEW_DIR = SCRIPT_DIR.parent / "claude-review"

# Fallback: try sibling directories
if not CODEX_REVIEW_DIR.exists():
    CODEX_REVIEW_DIR = SCRIPT_DIR / "codex-review"
if not CLAUDE_REVIEW_DIR.exists():
    CLAUDE_REVIEW_DIR = SCRIPT_DIR / "claude-review"

# Add both to path for imports
import sys as _sys
for d in [CODEX_REVIEW_DIR, CLAUDE_REVIEW_DIR]:
    if d.exists():
        _sys.path.insert(0, str(d))

try:
    from codex_review import (
        resolve_api_credentials as resolve_codex_creds,
        run_review as run_codex_review,
        collect_context as collect_codex_context,
        truncate_context as truncate_codex,
        REVIEW_SYSTEM_PROMPT as CODEX_SYSTEM_PROMPT,
        REVIEW_USER_TEMPLATE as CODEX_USER_TEMPLATE,
        NEED_CONTEXT_PATTERN,
    )
except ImportError:
    CODEX_AVAILABLE = False
else:
    CODEX_AVAILABLE = True

try:
    from claude_review import (
        resolve_api_credentials as resolve_claude_creds,
        run_review as run_claude_review,
        collect_context as collect_claude_context,
        truncate_context as truncate_claude,
        REVIEW_SYSTEM_PROMPT as CLAUDE_SYSTEM_PROMPT,
        REVIEW_USER_TEMPLATE as CLAUDE_USER_TEMPLATE,
    )
except ImportError:
    CLAUDE_AVAILABLE = False
else:
    CLAUDE_AVAILABLE = True


# --- Pass 1: Fable (Claude) system prompt ---
FABLE_SYSTEM_PROMPT = dedent("""\
    You are "Fable", a code review partner for an autonomous CLI agent called OMP (Oh My Pi).
    OMP has sent you a summary of its recent work for your perspective.

    This is the FIRST PASS of a two-pass review process. Another reviewer named "Sol" 
    (powered by Codex) will review your response along with the original context after you.
    So focus on what YOU do best: deep reasoning, architectural analysis, and catching 
    subtle design issues that might be missed.

    Format your response as:

    ## Fable's Review

    ### What I See
    (Brief summary of what was done)

    ### Thoughts
    - Things that look good
    - Potential issues or risks (especially subtle/design-level)
    - Suggestions or alternatives

    ### Notes for Sol
    (Anything you want Sol to specifically look at or verify — Sol will see your full review)

    IMPORTANT: If you spot something that looks particularly interesting, risky, or
    concerning and you need more detail, append this line at the very end:

    NEED_CONTEXT: <specific question about what you want to see>

    Only use NEED_CONTEXT if you genuinely need more detail.

    Be concise but thorough. Focus on correctness, edge cases, security, and design.
    If the work looks solid, say so — don't manufacture criticism.
""")

FABLE_USER_TEMPLATE = dedent("""\
    Here is a summary of recent work from OMP (Oh My Pi), a CLI agent.

    {focus_line}

    --- BEGIN AGENT CONTEXT ---
    {context}
    --- END AGENT CONTEXT ---
""")


# --- Pass 2: Sol (Codex) system prompt ---
SOL_SYSTEM_PROMPT = dedent("""\
    You are "Sol", a code review partner for an autonomous CLI agent called OMP (Oh My Pi).
    
    This is the SECOND PASS of a two-pass review process. Another reviewer named "Fable" 
    (powered by Claude) has already reviewed this work. You will see:
    1. The original context that Fable reviewed
    2. Fable's complete review

    Your job is to provide the FINAL review that goes back to OMP. You should:
    - Build on Fable's observations (agree, disagree, or add nuance)
    - Catch anything Fable missed (especially correctness bugs, security, operational issues)
    - Verify any claims Fable made that seem uncertain
    - Add your own perspective — you have different strengths
    - Give the final verdict

    Format your response as:

    ## Sol's Final Review

    ### Agreement with Fable
    (Where you agree or disagree with Fable's assessment, and why)

    ### Additional Findings
    (Things Fable missed or didn't emphasize enough)

    ### Final Assessment
    (Your overall take — this is what OMP will act on)

    ### Take What's Useful
    (Final notes for OMP — take what's useful, ignore the rest)

    IMPORTANT: If you need more context to give a proper final review, append:

    NEED_CONTEXT: <specific question about what you want to see>

    Be concise but thorough. If Fable's review is solid, say so and add what you can.
    Don't repeat what Fable already said unless you're confirming or contradicting it.
""")

SOL_USER_TEMPLATE = dedent("""\
    You are reviewing work from OMP (Oh My Pi), a CLI agent.
    
    {focus_line}

    Here is the original context that Fable reviewed:

    --- BEGIN AGENT CONTEXT ---
    {context}
    --- END AGENT CONTEXT ---

    And here is Fable's review:

    --- BEGIN FABLE'S REVIEW ---
    {fable_review}
    --- END FABLE'S REVIEW ---

    Please provide your final review, building on Fable's analysis and adding your own perspective.
""")


SOL_FOLLOWUP_TEMPLATE = dedent("""\
    You asked for more context. Here's what I found:

    --- ADDITIONAL CONTEXT ---
    {additional}
    --- END ADDITIONAL CONTEXT ---

    Please continue your final review with this new information.
""")


def log(msg, verbose=True):
    if verbose:
        print(f"[dual-review] {msg}", file=sys.stderr)


def collect_context(args):
    """Collect context using whichever tool's collector is available."""
    # Prefer codex_review's collector (they're identical anyway)
    if CODEX_AVAILABLE:
        return collect_codex_context(args)
    elif CLAUDE_AVAILABLE:
        return collect_claude_context(args)
    else:
        # Inline fallback
        if args.context_file:
            return Path(args.context_file).read_text(encoding="utf-8")
        elif args.context_cmd:
            import subprocess
            r = subprocess.run(args.context_cmd, shell=True, capture_output=True, text=True, timeout=30)
            return r.stdout
        elif not sys.stdin.isatty():
            return sys.stdin.read()
        return None


def truncate_context(text, max_chars=50000):
    """Truncate using whichever tool's truncator is available."""
    if CODEX_AVAILABLE:
        return truncate_codex(text, max_chars)
    elif CLAUDE_AVAILABLE:
        return truncate_claude(text, max_chars)
    # Inline
    if len(text) <= max_chars:
        return text
    head_size = int(max_chars * 0.2)
    tail_size = max_chars - head_size - 100
    return text[:head_size] + f"\n\n[... {len(text) - head_size - tail_size} chars truncated ...]\n\n" + text[-tail_size:]


def _filter_context(raw, query):
    """Filter context for a NEED_CONTEXT request."""
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
        return '\n'.join(relevant)
    return truncate_context(raw)


def run_pass1_fable(context, args):
    """Pass 1: Send context to Fable (Claude) for initial review.
    
    Uses claude_review's infrastructure (which handles Claude CLI / Anthropic API / LiteLLM).
    """
    log("=== Pass 1: Fable (Claude) ===", args.verbose)

    if not CLAUDE_AVAILABLE:
        return None, "claude_review module not found — ensure claude-review is installed"

    # Create a mock args object for claude_review
    fable_args = argparse.Namespace(
        focus=args.focus,
        model=args.fable_model,
        multi_turn=False,  # Fable is single-pass; Sol gets the multi-turn capability
        max_turns=1,
        timeout=args.timeout,
        api_only=args.fable_api_only,
        context_file=args.context_file,
        context_cmd=args.context_cmd,
        file=None,
    )

    # Build messages manually using Fable's prompts
    messages = [
        {"role": "system", "content": FABLE_SYSTEM_PROMPT},
    ]
    focus_line = f"Focus area: {args.focus}" if args.focus else ""
    user_msg = FABLE_USER_TEMPLATE.format(
        context=truncate_context(context),
        focus_line=focus_line,
    )
    messages.append({"role": "user", "content": user_msg})

    # Resolve Claude credentials
    api_key, api_base, model, api_format = resolve_claude_creds(fable_args)

    review, turns, requests, error = run_claude_review(
        context, fable_args, api_key, api_base, model, api_format
    )

    if review is None:
        return None, f"Fable (Claude) failed: {error}"
    
    log(f"Fable's review: {len(review)} chars, {turns} turns", args.verbose)
    if requests:
        log(f"Fable requested context (noted but not pulled in pass 1): {requests}", args.verbose)
    
    return review, None


def run_pass2_sol(context, fable_review, args):
    """Pass 2: Send (context + Fable's review) to Sol (Codex) for final review.
    
    Uses codex_review's infrastructure (which handles Codex CLI / OpenAI API / LiteLLM).
    Supports multi-turn: Sol can request more context.
    """
    log("=== Pass 2: Sol (Codex) ===", args.verbose)

    if not CODEX_AVAILABLE:
        return None, "codex_review module not found — ensure codex-review is installed"

    # Build the combined input for Sol
    focus_line = f"Focus area: {args.focus}" if args.focus else ""
    sol_user_msg = SOL_USER_TEMPLATE.format(
        context=truncate_context(context),
        fable_review=fable_review,
        focus_line=focus_line,
    )

    # Build messages with Sol's system prompt
    messages = [
        {"role": "system", "content": SOL_SYSTEM_PROMPT},
        {"role": "user", "content": sol_user_msg},
    ]

    # Resolve Codex credentials
    sol_args = argparse.Namespace(
        focus=args.focus,
        model=args.sol_model,
        multi_turn=args.multi_turn,
        max_turns=args.max_turns,
        timeout=args.timeout,
        api_only=args.sol_api_only,
        context_file=args.context_file,
        context_cmd=args.context_cmd,
        file=None,
    )
    api_key, api_base, model = resolve_codex_creds(sol_args)

    # We need to run the review loop manually to use Sol's system prompt
    # instead of codex_review's default. Let's build a custom context string
    # that includes Fable's review, and use codex_review's API calling functions.
    
    context_requests = []
    turns = 0
    max_turns = args.max_turns if args.multi_turn else 1

    while turns < max_turns:
        turns += 1
        log(f"Sol turn {turns}/{max_turns}: sending {len(messages)} messages...", args.verbose)

        review = None
        error = None

        # Try Codex CLI first (if not api-only and first turn)
        if not args.sol_api_only and turns == 1:
            from codex_review import find_codex_cli, call_codex_cli
            full_prompt = SOL_SYSTEM_PROMPT + "\n\n" + sol_user_msg
            review, error = call_codex_cli(full_prompt, model if not api_key else None, args.timeout)

        # Fall back to API
        if review is None and api_key:
            from codex_review import call_llm
            # codex_review's call_llm uses messages format
            review, error = call_llm(messages, model, api_key, api_base, args.timeout)

        if review is None:
            if error:
                log(f"Sol turn {turns} failed: {error}", args.verbose)
            return None, turns, context_requests, error

        # Check if Sol wants more context
        match = NEED_CONTEXT_PATTERN.search(review)
        if match and args.multi_turn and turns < max_turns:
            query = match.group(1).strip()
            log(f"Sol requested context: {query}", args.verbose)
            context_requests.append(query)

            # Pull more context
            additional = collect_context(args)
            if additional:
                filtered = _filter_context(additional, query) if len(additional) > 5000 else additional
                followup = SOL_FOLLOWUP_TEMPLATE.format(additional=truncate_context(filtered, 20000))
                messages.append({"role": "assistant", "content": review})
                messages.append({"role": "user", "content": followup})
                log(f"Sent {len(followup)} chars of additional context to Sol", args.verbose)
            else:
                log(f"No additional context available for: {query}", args.verbose)
                messages.append({"role": "assistant", "content": review})
                messages.append({"role": "user", "content": f"No additional context available for your request: {query}\n\nPlease provide your final review based on what you have."})
        else:
            log(f"Sol's final review: {len(review)} chars, {turns} turns", args.verbose)
            return review, turns, context_requests, None

    return review, turns, context_requests, None


def run_dual_review(context, args):
    """Run the full two-pass review pipeline."""
    
    # Pass 1: Fable (Claude)
    fable_review, error = run_pass1_fable(context, args)
    if fable_review is None:
        return None, error, None

    # Pass 2: Sol (Codex)
    sol_review, turns, requests, error = run_pass2_sol(context, fable_review, args)
    if sol_review is None:
        # If Sol fails, return Fable's review with a note
        log(f"Sol failed, returning Fable's review with caveat: {error}", args.verbose)
        fallback = f"{fable_review}\n\n---\n*Note: Sol (Codex) review failed ({error}). Only Fable's review is available.*"
        return fallback, None, fable_review

    return sol_review, None, fable_review


def deliver_review(review, fable_review, args):
    """Deliver the review(s) to the specified destination(s)."""
    destinations = args.deliver or os.environ.get("DUAL_REVIEW_DELIVERY", "stdout")

    for dest in destinations.split(","):
        dest = dest.strip().lower()

        if dest == "stdout":
            print(review)

        elif dest == "file":
            outpath = args.output or os.environ.get("DUAL_REVIEW_OUTPUT", "/tmp/dual-review-last.md")
            content = review
            if args.show_both and fable_review:
                content = f"# Dual Review ({time.strftime('%Y-%m-%d %H:%M')})\n\n---\n\n{review}\n\n---\n\n## Fable's Original Review (Pass 1)\n\n{fable_review}\n"
            Path(outpath).write_text(content)
            log(f"Written to {outpath}")

        elif dest == "omp":
            omp_inbox = os.environ.get("OMP_INBOX", "/tmp/omp-inbox.md")
            with open(omp_inbox, "a") as f:
                f.write(f"\n\n---\n## Dual Review ({time.strftime('%Y-%m-%d %H:%M')})\n\n{review}\n")
            log(f"Delivered to OMP inbox: {omp_inbox}")

        elif dest == "telegram":
            outpath = "/tmp/dual-review-for-telegram.md"
            content = f"📋 **Dual Review** — Fable→Sol ({time.strftime('%Y-%m-%d %H:%M')})\n\n{review}"
            if args.show_both and fable_review:
                content += f"\n\n---\n\n**Fable's Pass 1:**\n\n{fable_review}"
            Path(outpath).write_text(content)
            log(f"Written to {outpath} (for Telegram delivery)")
            print(review)

        else:
            log(f"Unknown delivery target: {dest}")


def run_cron_mode(args):
    """Cron mode: collect, dual-review, deliver, track state."""
    state_file = Path(args.state_file or "/tmp/dual-review-state.json")

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

    log(f"Reviewing {len(review_context)} chars of context...", )

    review, error, fable_review = run_dual_review(review_context, args)

    if review is None:
        log(f"Dual review failed: {error}")
        return

    state["last_context_hash"] = context_hash
    state["last_context_len"] = len(context)
    state["last_review_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state_file.write_text(json.dumps(state, indent=2))

    log(f"Dual review complete", args.verbose)
    deliver_review(review, fable_review, args)


def main():
    parser = argparse.ArgumentParser(
        description="Two-pass review: Fable (Claude) reviews first, then Sol (Codex) reviews Fable's review + original context. Sol's final review goes to caller.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=dedent("""
            Pipeline: Context → Fable (Claude) → Fable's review → Sol (Codex) → Sol's final review → caller

            Examples:
              echo "refactored auth.py" | dual-review --no-interactive
              echo "..." | dual-review --no-interactive --multi-turn --context-cmd "cat /tmp/omp.log"
              dual-review --cron --context-file /tmp/omp.log --deliver telegram
              echo "..." | dual-review --no-interactive --show-both -v
        """),
    )

    ctx_group = parser.add_argument_group("Context Sources")
    ctx_group.add_argument("--file", "-f", help="Read context from file")
    ctx_group.add_argument("--context-file", help="Read context from file (cron mode)")
    ctx_group.add_argument("--context-cmd", help="Shell command to collect context")

    rev_group = parser.add_argument_group("Review Options")
    rev_group.add_argument("--focus", help="Specific area to focus the review on")
    rev_group.add_argument("--multi-turn", action="store_true", help="Allow Sol to request more context in pass 2")
    rev_group.add_argument("--max-turns", type=int, default=3, help="Max Sol review turns (default: 3)")
    rev_group.add_argument("--timeout", type=int, default=120, help="API timeout per pass in seconds (default: 120)")
    rev_group.add_argument("--show-both", action="store_true", help="Include Fable's pass 1 review in output alongside Sol's final")

    # Per-pass model overrides
    pass_group = parser.add_argument_group("Per-Pass Configuration")
    pass_group.add_argument("--fable-model", help="Model for Fable/Claude pass (default: auto-detected)")
    pass_group.add_argument("--sol-model", help="Model for Sol/Codex pass (default: auto-detected)")
    pass_group.add_argument("--fable-api-only", action="store_true", help="Skip Claude CLI, use API for Fable")
    pass_group.add_argument("--sol-api-only", action="store_true", help="Skip Codex CLI, use API for Sol")

    cron_group = parser.add_argument_group("Cron Mode")
    cron_group.add_argument("--cron", action="store_true", help="Run in cron mode")
    cron_group.add_argument("--state-file", help="State file (default: /tmp/dual-review-state.json)")
    cron_group.add_argument("--force", action="store_true", help="Force review even if context unchanged")

    del_group = parser.add_argument_group("Delivery")
    del_group.add_argument("--deliver", help="Where to send review: stdout,file,omp,telegram (comma-sep)")
    del_group.add_argument("--output", "-o", help="Output file path (for --deliver file)")

    parser.add_argument("--no-interactive", action="store_true", help="Don't offer interactive follow-up")
    parser.add_argument("--version", action="version", version=f"dual-review v{VERSION}")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print status to stderr (shows both passes)")

    args = parser.parse_args()

    # Check dependencies
    if not CODEX_AVAILABLE:
        print("Error: codex-review not found. Install it at /root/workspace/codex-review/ or as a sibling directory.", file=sys.stderr)
        sys.exit(1)
    if not CLAUDE_AVAILABLE:
        print("Error: claude-review not found. Install it at /root/workspace/claude-review/ or as a sibling directory.", file=sys.stderr)
        sys.exit(1)

    if args.file and not args.context_file:
        args.context_file = args.file

    if args.cron:
        run_cron_mode(args)
    else:
        context = collect_context(args)
        if not context:
            print("Error: no context provided. Pipe text via stdin, use --file, or --context-cmd.", file=sys.stderr)
            sys.exit(1)

        log(f"Context: {len(context)} chars", args.verbose)

        review, error, fable_review = run_dual_review(context, args)

        if review is None:
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)

        deliver_review(review, fable_review, args)


if __name__ == "__main__":
    main()
