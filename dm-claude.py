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
import contextlib
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

  # A long prompt, without shell quoting
  dm-claude.py -f - user@myserver <<'EOF'
  ...as many lines as you like...
  EOF

  # Long-running task with generous timeout
  dm-claude.py -t 1200 user@myserver "run the full test suite and summarise failures"

exit status:
  0      the remote claude answered
  1      an argument refused by our own checks, the upload failed, or the remote claude
         reported an error or printed nothing
  2      the command line was not understood (unknown flag, bad number)
  120    python could not flush stdout (for example a closed pipe): replaces the status the
         run would have had
  124    timed out (-t); SIGTERM is sent to the remote claude (best effort; the log says what was found)
  130    interrupted with Ctrl-C; SIGTERM is sent to the remote claude, or the uploaded prompt is removed if the upload was still running (best effort; the log says which)
  129, 143   killed by SIGHUP / SIGTERM (128 + signal); SIGTERM is sent to the remote claude, or the uploaded prompt is removed if the upload was still running (best effort; the log says which)
  255    ssh itself failed (connection lost, host key changed, killed by a signal, ...);
         SIGTERM is sent to the remote claude (best effort; the log says what was found)
  other  the remote command's own status (127: claude not found on the remote's PATH);
         one that equals a code above cannot be told apart from it
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
# A --remote-env value (a config directory) is a plain path. Quoting makes any value safe for
# /bin/sh, but the remote *login* shell sees the command first: csh and tcsh expand `!` and
# reject a newline even inside single quotes, fish reads a backslash, and a command they
# reject never starts, so the cleanup trap never arms.
REMOTE_ENV_VALUE_RE = re.compile(r"[A-Za-z0-9_./+@:%= -]+")
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


# A reply line that starts with our log prefix could pose as one of our status lines
# (session_id:, continue:) once stdout and stderr are shown interleaved, as the Bash tool
# and `2>&1` do. Log lines are all prefixed, so only reply lines need this. Any whitespace may
# precede the prefix (NBSP, em space, ideographic space ... : str.lstrip() removes them all);
# after sanitize() the only line break left is "\n".
REPLY_PREFIX_RE = re.compile(r"^([^\S\n]*)\[dm-claude\]", re.MULTILINE)


def reply_text(text):
    """Remote text for stdout: sanitized, and no line of it starts with `[dm-claude]`."""
    return REPLY_PREFIX_RE.sub(r"\1(dm-claude)", sanitize(text))


def log(msg):
    # Prefix every line, so a multi-line message (e.g. remote stderr) cannot
    # contain a line that looks like one of our own status lines.
    for line in str(msg).splitlines() or [""]:
        if sys.stderr is None:      # print(file=None) would write to stdout, i.e. into the reply
            return
        try:
            print(f"[dm-claude] {line}", file=sys.stderr)
        except (OSError, ValueError):
            # stderr is gone (hung-up terminal, dead pipe, closed file). Logging must never
            # stop the remote cleanup or the exit status that follow, and there is nobody
            # left to tell. Swap in a sink so the interpreter's exit flush cannot fail too.
            sys.stderr = open(os.devnull, "w")
            return


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
                   help=f"Timeout for the remote run, in seconds (default: {DEFAULT_TIMEOUT}); "
                        "the upload (60 s) and each cleanup (30 s) have fixed limits")
    p.add_argument("-f", "--file", dest="prompt_file", metavar="FILE",
                   help="Read prompt from file ('-' for standard input)")
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
                        "(e.g. CLAUDE_CONFIG_DIR=/config/.claude-ssh); the value must be a plain path")
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
        key, sep, value = kv.partition("=")
        if not sep or key not in REMOTE_ENV_ALLOW:
            log(f"Error: --remote-env {key!r} is not allowed "
                f"(allowed: {', '.join(sorted(REMOTE_ENV_ALLOW))})")
            sys.exit(1)
        if not REMOTE_ENV_VALUE_RE.fullmatch(value):
            log(f"Error: --remote-env {key} must be a plain path "
                "(letters, digits, space and _ . / + @ : % = -)")
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
            if args.prompt_file == "-":     # e.g. a heredoc: no shell quoting, no argv size limit
                prompt = sys.stdin.buffer.read().decode("utf-8")
            else:
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


def remote_sh(script):
    """The string ssh hands to the remote *login* shell for a POSIX-sh script.

    The login shell may be csh or tcsh (FreeBSD, TrueNAS, OPNsense), fish, or anything else,
    and none of those parse `$(...)`, `name() { ...; }`, `exec 3< file` or `trap ... EXIT`.
    Every one of them can run `/bin/sh -c '<one single-quoted word>'`, and sh then runs the
    script. csh and tcsh expand `!` even inside single quotes, so the scripts avoid it."""
    return f"/bin/sh -c {shlex.quote(script)}"


def remote_cleanup(target, run_dir, kill):
    """Best-effort remote cleanup over a fresh ssh; returns the set of status words
    ("killed", "no-pid", "no-pgrep") the remote script printed, or None if it could not
    be reached / timed out.

    With kill=True it first signals the process tree rooted at the recorded pid
    (children bottom-up, so tool subprocesses claude spawned die too). Only a
    plain decimal pid greater than 1 that is still running is ever signalled,
    whatever the pid file holds; "killed" is only reported in that case.
    """
    script = f"rm -f {run_dir}/prompt {run_dir}/pid; rmdir {run_dir} 2>/dev/null || :"
    # Status words carry this run's name: the remote login shell may print anything first (an
    # rc file's banner), and a banner saying "killed" must not be taken for our report.
    tag = os.path.basename(run_dir)
    if kill:
        script = (
            'kt() { for c in $(pgrep -P "$1" 2>/dev/null); do kt "$c"; done; kill "$1" 2>/dev/null; }; '
            f"p=$(cat {run_dir}/pid 2>/dev/null); "
            # Reject empty, leading-zero, non-digit and 9+ digit values. Linux pids have
            # at most 7 digits; zsh and ksh truncate kill's argument to 32 bits, so
            # "4294967296" would become 0, i.e. our own process group. "Non-digit" is tested
            # by deleting everything but digits and comparing (a `[!0-9]` pattern would be
            # simpler, but csh and tcsh expand `!` even inside single quotes).
            'case $p in ""|0*|?????????*) p=;; esac; '
            '[ "$(printf %s "$p" | LC_ALL=C tr -cd 0-9 2>/dev/null)" = "$p" ] || p=; '
            'if [ -n "$p" ] && [ "$p" -gt 1 ] && kill -0 "$p" 2>/dev/null; then '
            f'command -v pgrep >/dev/null 2>&1 || echo no-pgrep:{tag}; kt "$p"; echo killed:{tag}; '
            f'else echo no-pid:{tag}; fi; ' + script
        )
    try:
        r = subprocess.run(
            SSH_BASE + ["-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                        "--", target, remote_sh(script)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    words = r.stdout.decode("utf-8", errors="replace").split()
    return {w[:-len(tag) - 1] for w in words if w.endswith(":" + tag)}


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


def remove_remote_leftovers(target, run_dir):
    """The cleanup for paths where no claude is running over there, only a prompt may be. It is a
    second chance, so when it fails too (the link is still down) say where the prompt is."""
    if remote_cleanup(target, run_dir, kill=False) is None:
        log(f"could not confirm the remote cleanup; the prompt may still be in {target}:{run_dir}")


class Interrupted(Exception):
    """Raised from a signal handler so the remote can be cleaned up before we exit."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


def _raise_interrupted(signum, _frame):
    raise Interrupted(signum)


def _set_unless_ignored(names, handler):
    for name in names:
        sig = getattr(signal, name, None)
        # A signal that was ignored when we started (nohup, `trap '' HUP`, a supervisor)
        # stays ignored: whoever set that wants this run to survive it.
        if sig is not None and signal.getsignal(sig) != signal.SIG_IGN:
            signal.signal(sig, handler)


def set_signal_handlers(handler):
    # SIGINT already raises KeyboardInterrupt. SIGTERM (kill, a caller's tool timeout) and
    # SIGHUP (closed terminal) would otherwise end us at once and leave the remote claude
    # running with nobody to read its answer. SIGHUP does not exist on Windows.
    _set_unless_ignored(("SIGTERM", "SIGHUP"), handler)


def end_at_next_signal():
    """Called once the first interrupt is being handled: a second signal (an impatient
    Ctrl-C) must end us at once instead of interrupting the cleanup half way."""
    _set_unless_ignored(("SIGTERM", "SIGHUP", "SIGINT"), signal.SIG_DFL)


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
            SSH_BASE + ["--", target, remote_sh(
                f"umask 077; mkdir {run_dir} || exit 1; "
                f"cat > {tmp_file} || {{ rm -f {tmp_file}; rmdir {run_dir}; exit 1; }}")],
            input=full_prompt.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        log("SSH connection failed: upload timed out")
        end_at_next_signal()
        remove_remote_leftovers(target, run_dir)        # part of the prompt may be there
        sys.exit(1)
    except (KeyboardInterrupt, Interrupted) as e:
        end_at_next_signal()
        log("interrupted during upload, removing the prompt from the remote...")
        remove_remote_leftovers(target, run_dir)
        sys.exit(interrupt_exit_code(e))
    if up.returncode != 0:
        detail = sanitize(up.stderr.decode(errors="replace")).strip()
        if up.returncode < 0:
            detail = f"ssh was killed by signal {-up.returncode}" + (f": {detail}" if detail else "")
        elif not detail:
            detail = f"ssh exited with status {up.returncode}"
        log(f"SSH connection failed: {detail}")
        if up.returncode != 1:
            # ssh itself failed (255), was killed, or ended with some other status, possibly
            # after the remote `cat` finished: don't leave the prompt behind. (Exit 1 is the
            # remote script's own: it cleaned up, or mkdir refused a name that is not ours
            # to touch.)
            end_at_next_signal()
            remove_remote_leftovers(target, run_dir)
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
        remote_sh(
            f"umask 077; trap 'rm -f {tmp_file} {pid_file}; rmdir {run_dir} 2>/dev/null' EXIT; "
            f"echo $$ > {pid_file} || exit 1; "
            f"exec 3< {tmp_file} || exit 1; rm -f {tmp_file}; "
            f"{remote_cmd} <&3 3<&-"),
    ]
    try:
        run = subprocess.run(run_cmd, stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=args.timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt, Interrupted) as e:
        end_at_next_signal()
        if isinstance(e, subprocess.TimeoutExpired):
            log(f"TIMEOUT after {args.timeout}s, terminating remote...")
        else:
            log("interrupted, terminating remote (a second Ctrl-C abandons the cleanup)...")
        # Killing local ssh doesn't stop the remote process (no pty), so do it explicitly.
        kill_remote(target, run_dir)
        sys.exit(interrupt_exit_code(e))
    end_at_next_signal()        # the rest is failure cleanup and output: no half-done Interrupted

    rc = run.returncode
    if rc < 0:
        # ssh itself was killed by a signal (OOM killer, pkill): sys.exit(-9) would say 247,
        # and the remote claude is as stranded as after a dropped connection.
        log(f"ssh was killed by signal {-rc}")
        rc = 255
    stdout = run.stdout.decode("utf-8", errors="replace")
    stderr = sanitize(run.stderr.decode("utf-8", errors="replace"))

    if args.verbose and stderr:
        log(f"stderr:\n{stderr}")

    def clean_up_after_failure():
        if rc == 255:
            # ssh itself failed (e.g. the link dropped after the upload) and claude may well
            # still be running over there with nobody to read its answer: stop it, and
            # don't leave the prompt behind in the remote /tmp.
            kill_remote(target, run_dir)
        else:
            # The remote script's own EXIT trap cleans up when it ran at all. If the login
            # shell rejected the command it never did, and the prompt is still over there.
            remove_remote_leftovers(target, run_dir)

    if rc != 0 and not stdout.strip():
        log(f"FAILED (exit {rc}): {stderr.strip()}")
        explain_exit(rc)
        clean_up_after_failure()
        sys.exit(rc)

    # From here on a reply, or part of one, has arrived. If the command still failed: show what
    # there is first and clean up last, because the cleanup is another connection that can be
    # slow, or never finish if the link is down; and clean up whatever happens to the printing
    # (a closed or full stdout, an encoding it cannot represent).
    try:
        if not stdout.strip():
            # Only exit 0 gets here: the remote command succeeded and said nothing.
            log("the remote command exited 0 but printed nothing"
                + (f": {stderr.strip()}" if stderr.strip() else ""))
            sys.exit(1)

        parsed = try_parse_result(stdout)
        if not parsed:
            log("warning: could not parse JSON, raw output follows")
            print(reply_text(stdout))
            if rc != 0:
                log(f"remote command exited {rc}")
                if stderr.strip() and not args.verbose:  # -v already showed it
                    log(f"remote stderr:\n{stderr.strip()}")
                explain_exit(rc)
                sys.exit(rc)
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

        print(reply_text(str(result)))
        if is_error:
            log(f"remote claude reported an error ({sanitize(subtype) or 'unknown'})")
            sys.exit(1)
        if rc != 0:
            # A valid result line does not make a failed command a success (e.g. the remote
            # shell was killed after claude printed it).
            log(f"remote command exited {rc}")
            sys.exit(rc)
    finally:
        if rc != 0:
            if sys.stdout is not None:      # None when it was closed at startup
                with contextlib.suppress(OSError, ValueError):
                    sys.stdout.flush()
            clean_up_after_failure()


if __name__ == "__main__":
    main()
