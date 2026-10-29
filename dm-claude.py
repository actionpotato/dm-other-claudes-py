#!/usr/bin/env python3
"""
dm-claude.py - Cross-Claude Communication Tool

Sends tasks to Claude Code instances on remote machines via SSH.
Supports session continuity for multi-turn conversations.

Python port of dm-claude.ts. Standard library only (Python 3.8+), no Bun.

Copyright (c) 2026 George W Walford (modifications and new code)

Derived from dm-claude.ts in https://github.com/jthack/dm-other-claudes, which is
MIT licensed (see LICENSE-MIT; that notice is retained for the upstream-derived
portions). Modifications and new code in this file are licensed under the GNU
General Public License, version 2 only (see COPYING).

SPDX-License-Identifier: GPL-2.0-only AND MIT

Usage:
  dm-claude.py <user@host> "prompt"
  dm-claude.py -s <session-id> <user@host> "follow-up"
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import uuid

DEFAULT_TIMEOUT = 600

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

  # Autonomous remote task (opt-in: unrestricted shell on the remote, only for
  # tasks you have approved on that host)
  dm-claude.py --skip-permissions user@myserver "pull latest code and redeploy"

  # Long-running task with generous timeout
  dm-claude.py -t 1200 user@myserver "run the full test suite and summarise failures"
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

# --remote-env is an allow-list, not a deny-list: any variable that can change what
# the remote claude loads or where it talks (NODE_OPTIONS, PATH, HOME, ANTHROPIC_BASE_URL,
# HTTPS_PROXY, NODE_EXTRA_CA_CERTS, LD_*, ...) is a code-execution or credential-exfiltration
# knob, and a deny-list cannot be complete. Add a name here if you really need it.
REMOTE_ENV_ALLOW = frozenset({"CLAUDE_CONFIG_DIR"})
# All patterns below are applied with fullmatch(): re.match() with a trailing "$"
# also accepts a value followed by one "\n", which would slip a newline through.
# A target starting with "-" would be parsed by ssh as an option
# (e.g. -oProxyCommand=...), which is local command execution.
TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:%\[\]-]*$")
# Must start alphanumeric so the value can never be parsed as a flag by remote claude
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
REMOTE_BIN_RE = re.compile(r"^/[A-Za-z0-9_./+-]+$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$")
# Remote output is untrusted: drop ANSI escapes and control characters
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")
# Control and invisible characters. Beyond C0/C1 controls (incl. \r and 8-bit CSI)
# this strips characters a human cannot see but a model can read, so a reply
# cannot smuggle instructions past the person watching the terminal: soft hyphen,
# ALM, zero-width space, line and paragraph separators, bidi embeddings, overrides
# and isolates, word joiner & invisible operators, BOM, interlinear annotation, the
# Unicode tag block (U+E0000-E007F, the "ASCII smuggling" channel), and lone
# surrogates (which would make print() raise UnicodeEncodeError).
# Deliberately NOT stripped, because legitimate text needs them: ZWNJ / ZWJ
# (Persian, Indic scripts, emoji sequences), LRM / RLM (mixed-direction text) and
# variation selectors (emoji presentation, CJK). They remain a possible covert
# channel; see README "Security notes".
CTRL_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b\u2028-\u202e"
    "\u2060-\u206f\ufeff\ufff9-\ufffb\ud800-\udfff\U000e0000-\U000e007f]"
)


def sanitize(text):
    return CTRL_RE.sub("", ANSI_RE.sub("", text))


def log(msg):
    # Prefix every line, so a multi-line message (e.g. remote stderr) cannot
    # contain a line that looks like one of our own status lines.
    for line in str(msg).splitlines() or [""]:
        print(f"[dm-claude] {line}", file=sys.stderr)


def positive_int(v):
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def parse_args():
    p = argparse.ArgumentParser(
        prog="dm-claude.py",
        description="Send tasks to Claude Code instances on remote machines",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-s", "--session", help="Resume a previous session")
    p.add_argument("-t", "--timeout", type=positive_int, default=DEFAULT_TIMEOUT,
                   help=f"SSH/command timeout in seconds (default: {DEFAULT_TIMEOUT})")
    p.add_argument("-f", "--file", dest="prompt_file", help="Read prompt from file")
    p.add_argument("--skip-permissions", action="store_true",
                   help="Add --dangerously-skip-permissions on remote")
    p.add_argument("--max-turns", type=positive_int, help="Limit remote Claude's tool-use turns")
    p.add_argument("--model", help="Override remote Claude's model")
    p.add_argument("--remote-claude", metavar="PATH",
                   help="Absolute path to the claude binary on the remote "
                        "(when it isn't on a non-interactive ssh PATH)")
    p.add_argument("--remote-env", action="append", default=[], metavar="KEY=VALUE",
                   help="Set an environment variable for remote claude (repeatable); only "
                        "allow-listed names are accepted, currently CLAUDE_CONFIG_DIR "
                        "(e.g. CLAUDE_CONFIG_DIR=/config/.claude-ssh)")
    p.add_argument("-v", "--verbose", action="store_true", help="Show debug info")
    p.add_argument("target", nargs="?", help="user@host")
    p.add_argument("prompt", nargs="*", help="prompt text")
    args = p.parse_args()

    if not args.target or (not args.prompt and not args.prompt_file):
        log("Error: need <user@host> and a prompt (or -f <file>)")
        log("Run with -h for help")
        sys.exit(1)
    if not TARGET_RE.fullmatch(args.target):
        log(f"Error: invalid target {args.target!r} (expected user@host)")
        sys.exit(1)
    if args.remote_claude and not REMOTE_BIN_RE.fullmatch(args.remote_claude):
        log("Error: --remote-claude must be an absolute path (letters, digits, _ . / + -)")
        sys.exit(1)
    for kv in args.remote_env:
        key, sep, _ = kv.partition("=")
        if not sep or key not in REMOTE_ENV_ALLOW:
            log(f"Error: --remote-env {key!r} is not allowed "
                f"(allowed: {', '.join(sorted(REMOTE_ENV_ALLOW))})")
            sys.exit(1)
    if args.model and not MODEL_RE.fullmatch(args.model):
        log("Error: invalid model name")
        sys.exit(1)
    if args.session and not SESSION_RE.fullmatch(args.session):
        log("Error: invalid session id")
        sys.exit(1)
    return args


def build_prompt(args):
    if args.prompt_file:
        try:
            with open(args.prompt_file, encoding="utf-8") as f:
                prompt = f.read()
        except (OSError, UnicodeDecodeError) as e:
            log(f"Error: cannot read prompt file: {e}")
            sys.exit(1)
        if args.prompt:
            prompt = " ".join(args.prompt) + "\n\n" + prompt
    else:
        prompt = " ".join(args.prompt)
    # Preamble only for new sessions; resumed ones already have context
    return prompt if args.session else PREAMBLE + prompt


def build_remote_cmd(args):
    parts = [args.remote_claude or "claude"]
    if args.session:
        parts += ["--resume", args.session]
    parts += ["-p", "--output-format", "json"]
    if args.skip_permissions:
        parts.append("--dangerously-skip-permissions")
    if args.max_turns is not None:
        parts += ["--max-turns", str(args.max_turns)]
    if args.model:
        parts += ["--model", args.model]
    env = [f"{shlex.quote(k)}={shlex.quote(v)}"
           for k, _, v in (kv.partition("=") for kv in args.remote_env)]
    return " ".join(env + [shlex.quote(x) for x in parts])


def try_parse_result(text):
    lines = text.strip().split("\n")
    # Find the result JSON object (search from end)
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):  # RecursionError: absurdly nested JSON
            continue
        if isinstance(obj, dict) and (
            obj.get("type") == "result" or obj.get("session_id") or "result" in obj
        ):
            return obj
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, RecursionError):
        return None


def remote_cleanup(target, run_dir, kill):
    """Best-effort remote cleanup over a fresh ssh; returns the set of status words
    the remote script printed, or None if it could not be reached / timed out.

    With kill=True it first signals the process tree rooted at the recorded pid
    (children bottom-up, so tool subprocesses claude spawned die too). Only a
    plain decimal pid greater than 1 that is still running is ever signalled,
    whatever the pid file holds; "killed" is only reported in that case.
    """
    script = f"rm -f {run_dir}/prompt {run_dir}/pid; rmdir {run_dir} 2>/dev/null || :"
    if kill:
        script = (
            'kt() { for c in $(pgrep -P "$1" 2>/dev/null); do kt "$c"; done; kill "$1" 2>/dev/null; }; '
            f"p=$(cat {run_dir}/pid 2>/dev/null); "
            # Reject empty, leading-zero, non-digit and 9+ digit values. Linux pids have
            # at most 7 digits; zsh and ksh truncate kill's argument to 32 bits, so
            # "4294967296" would become 0, i.e. our own process group.
            'case $p in ""|0*|*[!0-9]*|?????????*) p=;; esac; '
            'if [ -n "$p" ] && [ "$p" -gt 1 ] && kill -0 "$p" 2>/dev/null; then '
            'command -v pgrep >/dev/null 2>&1 || echo no-pgrep; kt "$p"; echo killed; '
            'else echo no-pid; fi; ' + script
        )
    try:
        r = subprocess.run(
            SSH_BASE + ["-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                        "--", target, script],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    return set(r.stdout.decode("utf-8", errors="replace").split()) if r.returncode == 0 else None


def kill_remote(target, run_dir):
    """Stop the remote run and say truthfully what happened."""
    status = remote_cleanup(target, run_dir, kill=True)
    if status is None:
        log(f"could not confirm remote cleanup; check {target} for a running claude")
    elif "killed" not in status:
        log("no live pid recorded on the remote (claude may already have exited); temp files removed")
    elif "no-pgrep" in status:
        log("signalled only the top-level remote shell (pgrep missing on the remote); "
            f"check {target} for a stray claude")
    else:
        log("remote process tree signalled")


class Interrupted(Exception):
    """Raised from a signal handler so the remote can be cleaned up before we exit."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _raise_interrupted(signum, _frame):
    raise Interrupted(signum)


def set_signal_handlers(handler):
    # SIGINT already raises KeyboardInterrupt. SIGTERM (kill, a caller's tool timeout) and
    # SIGHUP (closed terminal) would otherwise end us at once and leave the remote claude
    # running with nobody to read its answer. SIGHUP does not exist on Windows.
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)


def interrupt_exit_code(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return 124
    if isinstance(exc, KeyboardInterrupt):
        return 130
    return 128 + exc.signum


def explain_exit(code):
    if code == 127:
        log("hint: claude may not be on the remote's non-interactive PATH; "
            "try --remote-claude /absolute/path/to/claude")


def continue_command(args, sid):
    """The command that continues this conversation, with every flag it needs to reach the
    same remote claude (--remote-claude / --remote-env decide which binary and config dir)."""
    cmd = ["python3", os.path.abspath(__file__), "-s", sid]
    if args.timeout != DEFAULT_TIMEOUT:
        cmd += ["-t", str(args.timeout)]
    if args.skip_permissions:
        cmd.append("--skip-permissions")
    if args.max_turns is not None:
        cmd += ["--max-turns", str(args.max_turns)]
    if args.model:
        cmd += ["--model", args.model]
    if args.remote_claude:
        cmd += ["--remote-claude", args.remote_claude]
    for kv in args.remote_env:
        cmd += ["--remote-env", kv]
    cmd.append(args.target)
    return " ".join(shlex.quote(x) for x in cmd) + ' "follow-up"'


def main():
    args = parse_args()
    target = args.target
    full_prompt = build_prompt(args)
    remote_cmd = build_remote_cmd(args)

    # Two-phase: upload prompt over stdin to a remote temp file, then run
    # claude reading from it. Avoids all shell escaping issues.
    # Both files live in a per-run directory made by mkdir (atomic: it fails if the
    # name already exists, as a file, FIFO, symlink or directory) with umask 077. No
    # other user can then create, replace or read anything inside it. (Noclobber
    # would not do: it only protects regular files, and writes through a FIFO or a
    # symlink to a device.)
    run_id = uuid.uuid4().hex
    run_dir = f"/tmp/dm-claude-{run_id}"
    tmp_file = f"{run_dir}/prompt"
    pid_file = f"{run_dir}/pid"

    sid_note = f" (session {args.session[:12]}...)" if args.session else ""
    log(f"-> {target}{sid_note}")
    if args.verbose:
        log(f"cmd: {remote_cmd}")
        log(f"prompt: {len(full_prompt)} chars")
        log(f"runDir: {run_dir}")

    set_signal_handlers(_raise_interrupted)

    # Phase 1: upload prompt
    try:
        up = subprocess.run(
            SSH_BASE + ["--", target,
                        f"umask 077; mkdir {run_dir} || exit 1; "
                        f"cat > {tmp_file} || {{ rm -f {tmp_file}; rmdir {run_dir}; exit 1; }}"],
            input=full_prompt.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        log("SSH connection failed: upload timed out")
        sys.exit(1)
    except (KeyboardInterrupt, Interrupted) as e:
        set_signal_handlers(signal.SIG_DFL)
        log("interrupted during upload, removing the prompt from the remote...")
        remote_cleanup(target, run_dir, kill=False)
        sys.exit(interrupt_exit_code(e))
    if up.returncode != 0:
        log(f"SSH connection failed: {sanitize(up.stderr.decode(errors='replace')).strip()}")
        sys.exit(1)
    if args.verbose:
        log(f"prompt uploaded to {target}:{tmp_file}")

    # Phase 2: run claude
    run_cmd = SSH_BASE + [
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=20",
        "--", target,
        # Arm the cleanup first: POSIX shells exit if `exec 3<` fails, and the
        # trap must already be set when they do. Then record the shell's pid, open
        # the prompt and unlink it immediately so it never lingers. The shell stays
        # as claude's parent so the EXIT trap fires; on timeout remote_cleanup()
        # walks its children.
        f"umask 077; trap 'rm -f {tmp_file} {pid_file}; rmdir {run_dir} 2>/dev/null' EXIT; "
        f"echo $$ > {pid_file} || exit 1; "
        f"exec 3< {tmp_file} || exit 1; rm -f {tmp_file}; "
        f"{remote_cmd} <&3 3<&-",
    ]
    try:
        run = subprocess.run(run_cmd, stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=args.timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt, Interrupted) as e:
        set_signal_handlers(signal.SIG_DFL)  # a second signal ends us at once
        if isinstance(e, subprocess.TimeoutExpired):
            log(f"TIMEOUT after {args.timeout}s, terminating remote...")
        else:
            log("interrupted, terminating remote...")
        # Killing local ssh doesn't stop the remote process (no pty), so do it explicitly.
        kill_remote(target, run_dir)
        sys.exit(interrupt_exit_code(e))
    set_signal_handlers(signal.SIG_DFL)

    stdout = run.stdout.decode("utf-8", errors="replace")
    stderr = sanitize(run.stderr.decode("utf-8", errors="replace"))

    if args.verbose and stderr:
        log(f"stderr:\n{stderr}")

    if run.returncode != 0 and not stdout.strip():
        log(f"FAILED (exit {run.returncode}): {stderr.strip()}")
        explain_exit(run.returncode)
        if run.returncode == 255:
            # ssh itself failed (e.g. the link dropped after the upload): don't leave
            # the prompt behind in the remote /tmp.
            remote_cleanup(target, run_dir, kill=False)
        sys.exit(run.returncode or 1)

    parsed = try_parse_result(stdout)
    if not parsed:
        log("warning: could not parse JSON, raw output follows")
        print(sanitize(stdout))
        if run.returncode != 0:
            log(f"remote command exited {run.returncode}")
            if stderr.strip() and not args.verbose:  # -v already showed it
                log(f"remote stderr:\n{stderr.strip()}")
            explain_exit(run.returncode)
            sys.exit(run.returncode)
        return

    sid = parsed.get("session_id")
    if not isinstance(sid, str) or not SESSION_RE.fullmatch(sid):
        sid = None
    result = parsed.get("result", parsed.get("text", ""))
    if result is None:
        result = ""
    # Claude Code's failure results (e.g. subtype "error_max_turns") have no "result" text
    # and may not set is_error, so the subtype counts too.
    subtype = parsed.get("subtype")
    subtype = subtype if isinstance(subtype, str) else ""
    is_error = bool(parsed.get("is_error")) or subtype.startswith("error")
    cost = parsed.get("cost_usd", parsed.get("total_cost_usd"))
    turns = parsed.get("num_turns")
    duration = parsed.get("duration_ms")

    # Metadata to stderr, response text to stdout
    if sid:
        log(f"session_id: {sid}")
        log(f"continue: {continue_command(args, sid)}")
    if isinstance(cost, (int, float)):
        try:
            d = f"{duration / 1000:.1f}s" if isinstance(duration, (int, float)) and duration else "?"
            err = " ERROR" if is_error else ""
            log(f"cost=${cost:.4f} turns={turns if isinstance(turns, int) else '?'} time={d}{err}")
        except OverflowError:   # an integer too big for a float: the reply is not ours to trust
            log("cost/time not shown: numbers in the reply are out of range")

    print(sanitize(str(result)))
    if is_error:
        log(f"remote claude reported an error ({sanitize(subtype) or 'unknown'})")
        sys.exit(1)
    if run.returncode != 0:
        # A valid result line does not make a failed command a success (e.g. the remote
        # shell was killed after claude printed it).
        log(f"remote command exited {run.returncode}")
        sys.exit(run.returncode)


if __name__ == "__main__":
    main()
