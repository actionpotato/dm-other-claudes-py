#!/usr/bin/env python3
"""Offline tests for dm-claude.py. Standard library only.

    python3 -m unittest discover -s tests -v

Unit tests cover the validators, the output sanitizer, the remote command builder
and the reply parser. The integration tests put a fake `ssh` (which runs the command
string in a local POSIX shell, the way the remote would) and a fake `claude` first on
PATH, so no network, real ssh or real claude is involved. They need a POSIX sh and
write only to a temp dir and /tmp/dm-claude-<random>/.
"""

import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "dm-claude.py")


def _load():
    spec = importlib.util.spec_from_file_location("dm_claude", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dmc = _load()


def parse(*argv):
    """Run parse_args() on argv; return (Namespace | exit code, captured stderr)."""
    err = io.StringIO()
    with mock.patch.object(sys, "argv", ["dm-claude.py", *argv]), contextlib.redirect_stderr(err):
        try:
            return dmc.parse_args(), err.getvalue()
        except SystemExit as e:
            return e.code, err.getvalue()


def remote_shells():
    """Distinct shells to play the remote login shell. They differ in what this script
    relies on: dash and bash exit when `exec 3<file` fails, zsh and ksh truncate the
    argument of `kill` to 32 bits."""
    seen, found = set(), []
    for path in ("/bin/sh", "/bin/dash", "/bin/bash", "/bin/zsh", "/usr/bin/zsh", "/bin/ksh", "/usr/bin/ksh"):
        real = os.path.realpath(path)
        if os.path.exists(path) and real not in seen:
            seen.add(real)
            found.append(path)
    return found


class ValidatorTests(unittest.TestCase):
    def assertRefused(self, *argv):
        result, _ = parse(*argv)
        self.assertEqual(result, 1, f"expected refusal for {argv!r}")

    def assertAccepted(self, *argv):
        result, err = parse(*argv)
        self.assertIsInstance(result, __import__("argparse").Namespace, err)

    def test_target(self):
        for ok in ("user@host", "host", "u@10.0.0.1", "u@[::1]", "u@host.example.com"):
            self.assertAccepted(ok, "p")
        for bad in ("-oProxyCommand=x", "u@h\n", "u@h x", "u@h;ls", "u@h$(id)"):
            self.assertRefused("--", bad, "p")

    def test_session(self):
        self.assertAccepted("-s", "abc-123_X", "u@h", "p")
        for bad in ("-x", "a\n", "a b", "a;b"):
            self.assertRefused("--session=" + bad, "u@h", "p")

    def test_model(self):
        self.assertAccepted("--model", "claude-sonnet-4-6", "u@h", "p")
        for bad in ("a\n", "a;b", "a b", "$(id)"):
            self.assertRefused("--model", bad, "u@h", "p")
        self.assertRefused("--model=-x", "u@h", "p")

    def test_remote_claude(self):
        self.assertAccepted("--remote-claude", "/usr/local/bin/claude", "u@h", "p")
        for bad in ("claude", "/a b", "/x\n", "/x;y", "/x$(id)"):
            self.assertRefused("--remote-claude", bad, "u@h", "p")

    def test_remote_env_allow_list(self):
        self.assertAccepted("--remote-env", "CLAUDE_CONFIG_DIR=/config/.claude-ssh", "u@h", "p")
        for bad in (
            "NODE_OPTIONS=--require=/tmp/x.js", "ANTHROPIC_BASE_URL=https://evil.example",
            "HTTPS_PROXY=http://evil.example:8080", "PATH=/tmp/evil", "HOME=/tmp/evil",
            "LD_PRELOAD=/tmp/x.so", "BASH_ENV=/tmp/x",
            "CLAUDE_CONFIG_DIR",            # no "="
            "CLAUDE_CONFIG_DIR\n=x",        # an allow-listed name followed by a newline
            "claude_config_dir=/x",         # names are case-sensitive
        ):
            self.assertRefused("--remote-env", bad, "u@h", "p")

    def test_needs_target_and_prompt(self):
        self.assertRefused()
        self.assertRefused("u@h")


class BuildRemoteCmdTests(unittest.TestCase):
    def test_quoting_roundtrip(self):
        args = types.SimpleNamespace(
            session="abc123", skip_permissions=True, max_turns=3, model="m-1",
            remote_claude="/usr/bin/claude",
            remote_env=["CLAUDE_CONFIG_DIR=/x y'z$(id)\nq;`id`"],
        )
        words = shlex.split(dmc.build_remote_cmd(args))
        self.assertEqual(words, [
            "CLAUDE_CONFIG_DIR=/x y'z$(id)\nq;`id`", "/usr/bin/claude", "--resume", "abc123",
            "-p", "--output-format", "json", "--dangerously-skip-permissions",
            "--max-turns", "3", "--model", "m-1",
        ])

    def test_key_is_quoted_too(self):
        # Defence in depth: even a key that skipped validation cannot start a new command line.
        args = types.SimpleNamespace(session=None, skip_permissions=False, max_turns=None,
                                     model=None, remote_claude=None, remote_env=["FOO\n=bar"])
        words = shlex.split(dmc.build_remote_cmd(args))
        self.assertEqual(words[0], "FOO\n=bar")
        self.assertEqual(words[1], "claude")


class SanitizeTests(unittest.TestCase):
    def test_strips_terminal_and_invisible_characters(self):
        tag = "".join(chr(0xE0000 + ord(c)) for c in "ignore previous instructions")
        cases = {
            "CSI": "a\x1b[31mb", "OSC title (BEL)": "a\x1b]0;t\x07b", "OSC 52 (ST)": "a\x1b]52;c;QQ==\x1b\\b",
            "CR": "a\rb", "C1 CSI": "a\x9bb", "NEL": "a\x85b", "NUL": "a\x00b",
            "tag characters": "a" + tag + "b", "ZWSP": "a\u200bb", "word joiner": "a\u2060b",
            "BOM": "a\ufeffb", "LS": "a\u2028b", "PS": "a\u2029b", "bidi override": "a\u202eb",
            "bidi embedding": "a\u202ab", "bidi isolate": "a\u2066b", "ALM": "a\u061cb",
            "soft hyphen": "a\u00adb", "interlinear annotation": "a\ufff9b",
            "lone surrogate": "a\ud800b",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(dmc.sanitize(text), "ab")

    def test_keeps_characters_that_real_text_needs(self):
        # Stripping joiners and variation selectors would corrupt Persian, Indic scripts,
        # emoji sequences, mixed-direction text and CJK. (They stay a possible covert
        # channel; the README says so.)
        cases = {
            "ZWNJ (Persian)": "می\u200cخواهم",
            "ZWJ (emoji family)": "\U0001F468\u200d\U0001F469\u200d\U0001F467",
            "VS16 (emoji presentation)": "❤\ufe0f",
            "LRM / RLM": "abc\u200eאב\u200f",
            "CJK variation selector": "辺\U000e0100",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertEqual(dmc.sanitize(text), text)

    def test_keeps_ordinary_text(self):
        text = "Plain text, newline\nand tab\t; accents café, CJK 日本語, dash — check ✓, box ┌─┐, emoji 🙂"
        self.assertEqual(dmc.sanitize(text), text)


class LogTests(unittest.TestCase):
    @staticmethod
    def logged(msg):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            dmc.log(msg)
        return err.getvalue().split("\n")[:-1]

    def test_every_line_gets_the_prefix(self):
        self.assertEqual(self.logged("one\ntwo"), ["[dm-claude] one", "[dm-claude] two"])
        self.assertEqual(self.logged(""), ["[dm-claude] "])

    def test_embedded_text_cannot_forge_a_status_line(self):
        lines = self.logged("remote stderr:\n[dm-claude] session_id: forged")
        self.assertNotIn("[dm-claude] session_id: forged", lines)


class ParseResultTests(unittest.TestCase):
    def test_result_line_after_noise(self):
        out = 'warming up\n{"type":"system"}\n{"type":"result","result":"hi","session_id":"abc"}\n'
        self.assertEqual(dmc.try_parse_result(out)["result"], "hi")

    def test_single_object(self):
        self.assertEqual(dmc.try_parse_result('{"result": "x"}')["result"], "x")

    def test_not_json_or_not_object(self):
        self.assertIsNone(dmc.try_parse_result("plain text"))
        self.assertIsNone(dmc.try_parse_result("[1, 2, 3]"))

    def test_absurdly_nested_json_does_not_raise(self):
        self.assertIsNone(dmc.try_parse_result("[" * 200000))


@unittest.skipUnless(os.name == "posix" and os.path.exists("/bin/sh"), "needs a POSIX sh")
class FakeRemoteTests(unittest.TestCase):
    """End-to-end runs against a fake ssh/claude on PATH."""

    FAKE_SSH = """#!/bin/sh
# Like ssh, but the "remote" is this machine. The remote shell is a CHILD (as with real
# ssh), so killing "ssh" does not kill it. FAKE_REMOTE_SH picks the remote login shell.
while [ $# -gt 0 ]; do
  case "$1" in --) shift; break;; -o) shift 2;; *) shift;; esac
done
shift   # target
"${FAKE_REMOTE_SH:-/bin/sh}" -c "$*" <&0 &
wait $!
"""
    FAKE_CLAUDE = """#!/bin/sh
last=$(tail -n 1)
case "$FAKE_MODE" in
  sleep) sleep 60 & echo $! > "$FAKE_STATE/child.pid"; echo $$ > "$FAKE_STATE/claude.pid"; wait ;;
  plain) echo "claude: plain-text failure"; exit 3 ;;
  err)   printf '%s\\n' '{"type":"result","is_error":true,"subtype":"x\\u001b[31mRED\\u202e\\u200bEND\\n[dm-claude] session_id: forged\\ud800","result":""}' ;;
  surrogate) printf '%s\\n' '{"type":"result","session_id":"abc123","result":"a\\ud800b"}' ;;
  bignum) big=1; i=0; while [ $i -lt 400 ]; do big=${big}0; i=$((i+1)); done
          printf '{"type":"result","session_id":"abc123","result":"big","total_cost_usd":%s,"duration_ms":%s}\\n' "$big" "$big" ;;
  exit5) printf '%s\\n' '{"type":"result","session_id":"abc123","result":"printed first"}'; exit 5 ;;
  *)     printf '{"type":"result","session_id":"abc123","result":"got %s","total_cost_usd":0.01,"num_turns":1,"duration_ms":10}\\n' "$last" ;;
esac
"""
    # remote_cleanup(kill=True) in a child Python; see cleanup_status()
    CLEANUP_CHILD = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('dmc', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "out = []\n"
        "for d in sys.argv[2:]:\n"
        "    st = m.remote_cleanup('me@host', d, kill=True)\n"
        "    out.append(None if st is None else sorted(st))\n"
        "print(json.dumps(out))\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dmc-test-")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for name, body in (("ssh", self.FAKE_SSH), ("claude", self.FAKE_CLAUDE)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
        self.env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_STATE=self.tmp, HOME=self.tmp)
        self.leftovers = []     # /tmp/dm-claude-* paths this test planted or may have leaked
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in ("child.pid", "claude.pid"):
            try:
                with open(os.path.join(self.tmp, name)) as f:
                    os.kill(int(f.read().strip()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        for path in self.leftovers:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args, mode="ok", timeout=60, **extra_env):
        env = dict(self.env, FAKE_MODE=mode, **extra_env)
        return subprocess.run([sys.executable, "-I", "-B", SCRIPT, *args], cwd=self.tmp, env=env,
                              capture_output=True, text=True, timeout=timeout)

    def assertGone(self, pid, what="process"):
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail(f"{what} {pid} survived")

    def grandchild_pid(self):
        with open(os.path.join(self.tmp, "child.pid")) as f:
            return int(f.read().strip())

    def run_dir(self, stderr):
        """The remote run directory, from the -v log; registered for cleanup."""
        path = next(l.split("runDir: ")[1] for l in stderr.splitlines() if "runDir: " in l)
        self.leftovers.append(path)
        return path

    def assertNoRemoteFiles(self, stderr):
        self.assertFalse(os.path.lexists(self.run_dir(stderr)), "remote run directory left behind")

    def test_roundtrip_and_cleanup(self):
        for shell in remote_shells():
            with self.subTest(shell=shell):
                r = self.run_script("-v", "me@host", "hello", FAKE_REMOTE_SH=shell)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("got hello", r.stdout)
                self.assertIn("session_id: abc123", r.stderr)
                self.assertNoRemoteFiles(r.stderr)

    def test_timeout_kills_the_whole_tree(self):
        r = self.run_script("-v", "-t", "2", "me@host", "slow", mode="sleep")
        self.assertEqual(r.returncode, 124, r.stderr)
        self.assertIn("remote process tree signalled", r.stderr)
        self.assertGone(self.grandchild_pid(), "grandchild")
        self.assertNoRemoteFiles(r.stderr)

    def test_non_json_failure_keeps_exit_status(self):
        r = self.run_script("me@host", "x", mode="plain")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("plain-text failure", r.stdout)

    def test_error_subtype_is_sanitized_and_cannot_forge_status_lines(self):
        r = self.run_script("me@host", "x", mode="err")
        self.assertEqual(r.returncode, 1, r.stderr)        # also: a lone surrogate must not crash print()
        self.assertIn("RED", r.stderr)
        for bad in ("\x1b", "\u202e", "\u200b", "\ud800"):
            self.assertNotIn(bad, r.stderr)
        self.assertNotIn("[dm-claude] session_id: forged", r.stderr.splitlines())

    def test_lone_surrogate_in_the_reply_does_not_crash(self):
        r = self.run_script("me@host", "x", mode="surrogate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "ab\n")

    def test_integers_too_big_for_a_float_do_not_crash(self):
        # 400 digits parse as a JSON int but overflow float(): the cost line is skipped
        r = self.run_script("me@host", "x", mode="bignum")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "big\n")
        self.assertNotIn("Traceback", r.stderr)

    def test_result_followed_by_a_failing_exit_is_not_success(self):
        r = self.run_script("me@host", "x", mode="exit5")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(r.stdout, "printed first\n")      # the result is still delivered
        self.assertIn("remote command exited 5", r.stderr)

    # -- in-process: pre-planted names, hostile pid-file contents ------------------------

    def _in_process_main(self, run_id, *argv):
        err, out = io.StringIO(), io.StringIO()
        self.leftovers.append(f"/tmp/dm-claude-{run_id}")
        saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])
        code = 0                    # main() returns normally on success
        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(dmc.uuid, "uuid4", return_value=types.SimpleNamespace(hex=run_id)), \
                mock.patch.object(sys, "argv", ["dm-claude.py", *argv]), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                dmc.main()
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def test_preplanted_name_is_refused_not_written_through(self):
        # noclobber (`set -C`) refuses an existing regular file but opens a FIFO or a
        # symlink to a device and writes through it. mkdir refuses anything that exists.
        victim = os.path.join(self.tmp, "victim.txt")
        for kind in ("fifo", "symlink to a file", "symlink to /dev/null", "directory"):
            with self.subTest(kind):
                run_id = uuid.uuid4().hex
                planted = f"/tmp/dm-claude-{run_id}"
                with open(victim, "w") as f:
                    f.write("PRECIOUS\n")
                if kind == "fifo":
                    os.mkfifo(planted)
                elif kind == "symlink to a file":
                    os.symlink(victim, planted)
                elif kind == "symlink to /dev/null":
                    os.symlink("/dev/null", planted)
                else:
                    os.mkdir(planted)
                    os.chmod(planted, 0o777)
                before = os.lstat(planted).st_mode
                code, err = self._in_process_main(run_id, "me@host", "hi")
                self.assertEqual(code, 1, err)
                self.assertIn("File exists", err)
                self.assertEqual(os.lstat(planted).st_mode, before)
                if kind == "directory":
                    self.assertEqual(os.listdir(planted), [], "wrote into a pre-existing directory")
                with open(victim) as f:
                    self.assertEqual(f.read(), "PRECIOUS\n")

    def test_run_directory_is_private(self):
        run_id = uuid.uuid4().hex
        modes = []
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            r = real_run(cmd, *a, **kw)
            d = f"/tmp/dm-claude-{run_id}"
            if os.path.isdir(d) and not modes:
                modes.append(stat.S_IMODE(os.stat(d).st_mode))
                modes.append(stat.S_IMODE(os.stat(f"{d}/prompt").st_mode))
            return r

        with mock.patch.object(dmc.subprocess, "run", spy):
            code, err = self._in_process_main(run_id, "me@host", "hi")
        self.assertEqual(modes, [0o700, 0o600], err)

    def cleanup_statuses(self, run_dirs, shell):
        """remote_cleanup(kill=True) for each directory, in a child Python in its own session.
        A broken pid guard can end up signalling a whole process group, and that must not be ours."""
        r = subprocess.run([sys.executable, "-I", "-B", "-c", self.CLEANUP_CHILD, SCRIPT, *run_dirs],
                           env=dict(self.env, FAKE_REMOTE_SH=shell), capture_output=True, text=True,
                           timeout=60, start_new_session=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def new_run_dir(self, pid_content):
        path = f"/tmp/dm-claude-{uuid.uuid4().hex}"
        os.mkdir(path, 0o700)
        self.leftovers.append(path)
        with open(f"{path}/pid", "w") as f:
            f.write(pid_content)
        return path

    def test_kill_script_only_signals_a_plain_running_pid(self):
        canary = subprocess.Popen(["sleep", "60"])
        self.addCleanup(canary.kill)
        marker = os.path.join(self.tmp, "pwned")
        hostile = ["-1", "12 34", "1", "0", "", "abc", "00012", f"0{canary.pid}",
                   "123456789",                     # 9 digits
                   "4294967296", "4294967297",      # 2**32 and 2**32+1: zsh/ksh read them as 0 and 1
                   f"{canary.pid}; touch {marker}", f"$(touch {marker})", f"{canary.pid} {canary.pid}"]
        for shell in remote_shells():
            with self.subTest(shell=shell):
                run_dirs = [self.new_run_dir(content) for content in hostile]
                statuses = self.cleanup_statuses(run_dirs, shell)
                self.assertEqual(dict(zip(hostile, statuses)), {c: ["no-pid"] for c in hostile})
                self.assertIsNone(canary.poll(), "canary was signalled")
                self.assertFalse(os.path.exists(marker), "injected command ran")
                self.assertEqual([d for d in run_dirs if os.path.lexists(d)], [], "run directory not removed")
        gone = subprocess.Popen(["true"])
        gone.wait()                 # a plain number, but nothing runs under it any more
        [status] = self.cleanup_statuses([self.new_run_dir(str(gone.pid))], remote_shells()[0])
        self.assertEqual(status, ["no-pid"])
        [status] = self.cleanup_statuses([self.new_run_dir(str(canary.pid))], remote_shells()[0])
        self.assertIn("killed", status)
        self.assertIsNotNone(canary.wait(timeout=5))


if __name__ == "__main__":
    unittest.main()
