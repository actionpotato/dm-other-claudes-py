#!/usr/bin/env python3
"""
dm-claude.py - Cross-Claude Communication Tool

Sends tasks to Claude Code instances on remote machines via SSH.
Supports session continuity for multi-turn conversations.

Python port of dm-claude.ts. Standard library only (Python 3.8+), no Bun.

Usage:
  dm-claude.py <user@host> "prompt"
  dm-claude.py -s <session-id> <user@host> "follow-up"
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import uuid

PREAMBLE = """[CROSS-CLAUDE COMMUNICATION]
You are receiving this message from another Claude Code instance via the "dm-claude" tool.
The calling Claude is running interactively in a TUI with a human user.

How this works:
- You are running in non-interactive print mode (-p) on this machine
- The calling Claude is waiting for your response
- Your entire response will be relayed back to the calling Claude
- Follow-up messages may arrive via --resume to continue this conversation

Guidelines:
- Treat this as a collaborative task from another Claude working with the same human
- Be thorough but concise - your response goes to another Claude, not directly to a human
- Include all relevant details: full file paths, URLs, command outputs, error messages
- If something fails or you need clarification, say so clearly
- You have full access to this machine's tools and filesystem
- Complete the task as autonomously as possible
- If the task references files, configs, or services on this machine, use your tools to inspect them

---
TASK FROM CALLING CLAUDE:
"""

EPILOG = """\
examples:
  # New conversation
  dm-claude.py user@myserver "list running services"

  # Continue conversation
  dm-claude.py -s abc123 user@myserver "restart the one on port 8080"

  # Fully autonomous remote task
  dm-claude.py --skip-permissions user@myserver "pull latest code and redeploy"

  # Long-running task with generous timeout
  dm-claude.py -t 1200 --skip-permissions user@myserver "run full analysis on target.com"
"""

# accept-new trusts a host on first contact but refuses if its key later changes
# (StrictHostKeyChecking=no would let a MITM silently read prompts and replies).
SSH_BASE = [
    "ssh", "-T",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
    "-o", "ClearAllForwardings=yes",
    "-o", "ForwardAgent=no",
    "-o", "ConnectTimeout=10",
]

# A target starting with "-" would be parsed by ssh as an option
# (e.g. -oProxyCommand=...), which is local command execution.
TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:%\[\]-]*$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Remote output is untrusted: drop ANSI escapes and control characters
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text):
    return CTRL_RE.sub("", ANSI_RE.sub("", text))


def log(msg):
    print(f"[dm-claude] {msg}", file=sys.stderr)


def parse_args():
    p = argparse.ArgumentParser(
        prog="dm-claude.py",
        description="Send tasks to Claude Code instances on remote machines",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-s", "--session", help="Resume a previous session")
    p.add_argument("-t", "--timeout", type=int, default=600,
                   help="SSH/command timeout in seconds (default: 600)")
    p.add_argument("-f", "--file", dest="prompt_file", help="Read prompt from file")
    p.add_argument("--skip-permissions", action="store_true",
                   help="Add --dangerously-skip-permissions on remote")
    p.add_argument("--max-turns", type=int, help="Limit remote Claude's tool-use turns")
    p.add_argument("--model", help="Override remote Claude's model")
    p.add_argument("-v", "--verbose", action="store_true", help="Show debug info")
    p.add_argument("target", nargs="?", help="user@host")
    p.add_argument("prompt", nargs="*", help="prompt text")
    args = p.parse_args()

    if not args.target or (not args.prompt and not args.prompt_file):
        log("Error: need <user@host> and a prompt (or -f <file>)")
        log("Run with -h for help")
        sys.exit(1)
    if not TARGET_RE.match(args.target):
        log(f"Error: invalid target {args.target!r} (expected user@host)")
        sys.exit(1)
    if args.session and not SESSION_RE.match(args.session):
        log("Error: invalid session id")
        sys.exit(1)
    return args


def build_prompt(args):
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            prompt = f.read()
        if args.prompt:
            prompt = " ".join(args.prompt) + "\n\n" + prompt
    else:
        prompt = " ".join(args.prompt)
    # Preamble only for new sessions; resumed ones already have context
    return prompt if args.session else PREAMBLE + prompt


def build_remote_cmd(args):
    parts = ["claude"]
    if args.session:
        parts += ["--resume", args.session]
    parts += ["-p", "--output-format", "json"]
    if args.skip_permissions:
        parts.append("--dangerously-skip-permissions")
    if args.max_turns:
        parts += ["--max-turns", str(args.max_turns)]
    if args.model:
        parts += ["--model", args.model]
    return " ".join(shlex.quote(x) for x in parts)


def try_parse_result(text):
    lines = text.strip().split("\n")
    # Find the result JSON object (search from end)
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and (
            obj.get("type") == "result" or obj.get("session_id") or "result" in obj
        ):
            return obj
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def main():
    args = parse_args()
    target = args.target
    full_prompt = build_prompt(args)
    remote_cmd = build_remote_cmd(args)

    # Two-phase: upload prompt over stdin to a remote temp file, then run
    # claude reading from it. Avoids all shell escaping issues.
    # Full 128-bit name; the remote writes it with umask 077 and noclobber
    # (O_EXCL) so a pre-planted file or symlink can't be followed or read.
    tmp_file = f"/tmp/dm-claude-{uuid.uuid4().hex}.txt"

    sid_note = f" (session {args.session[:12]}...)" if args.session else ""
    log(f"-> {target}{sid_note}")
    if args.verbose:
        log(f"cmd: {remote_cmd}")
        log(f"prompt: {len(full_prompt)} chars")
        log(f"tmpFile: {tmp_file}")

    # Phase 1: upload prompt
    try:
        up = subprocess.run(
            SSH_BASE + ["--", target, f"umask 077; set -C; cat > {tmp_file}"],
            input=full_prompt.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        log("SSH connection failed: upload timed out")
        sys.exit(1)
    if up.returncode != 0:
        log(f"SSH connection failed: {up.stderr.decode(errors='replace').strip()}")
        sys.exit(1)
    if args.verbose:
        log(f"prompt uploaded to {target}:{tmp_file}")

    # Phase 2: run claude
    run_cmd = SSH_BASE + [
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=20",
        "--", target,
        f"{remote_cmd} < {tmp_file}; EXIT=$?; rm -f {tmp_file}; exit $EXIT",
    ]
    try:
        run = subprocess.run(run_cmd, stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT after {args.timeout}s, killing...")
        subprocess.run(
            SSH_BASE + ["-o", "ConnectTimeout=5", "--", target, f"rm -f {tmp_file}"],
            stdin=subprocess.DEVNULL, capture_output=True,
        )
        sys.exit(124)

    stdout = run.stdout.decode("utf-8", errors="replace")
    stderr = sanitize(run.stderr.decode("utf-8", errors="replace"))

    if args.verbose and stderr:
        log(f"stderr:\n{stderr}")

    if run.returncode != 0 and not stdout.strip():
        log(f"FAILED (exit {run.returncode}): {stderr.strip()}")
        sys.exit(run.returncode or 1)

    parsed = try_parse_result(stdout)
    if not parsed:
        log("warning: could not parse JSON, raw output follows")
        print(sanitize(stdout))
        return

    sid = parsed.get("session_id")
    if not isinstance(sid, str) or not SESSION_RE.match(sid):
        sid = None
    result = parsed.get("result", parsed.get("text", ""))
    cost = parsed.get("cost_usd", parsed.get("total_cost_usd"))
    turns = parsed.get("num_turns")
    duration = parsed.get("duration_ms")

    # Metadata to stderr, response text to stdout
    if sid:
        log(f"session_id: {sid}")
        log(f'continue: python3 dm-claude.py -s {sid} {target} "follow-up"')
    if isinstance(cost, (int, float)):
        d = f"{duration / 1000:.1f}s" if isinstance(duration, (int, float)) and duration else "?"
        err = " ERROR" if parsed.get("is_error") else ""
        log(f"cost=${cost:.4f} turns={turns if isinstance(turns, int) else '?'} time={d}{err}")

    print(sanitize(str(result)))


if __name__ == "__main__":
    main()
