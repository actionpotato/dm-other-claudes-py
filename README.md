# dm-other-claudes-py

> **This is a fork of [jthack/dm-other-claudes](https://github.com/jthack/dm-other-claudes) that uses Python instead of Bun.** The original `dm-claude.ts` script is replaced by `dm-claude.py`, which needs only Python 3.8+ (standard library) and the `ssh` client, so it runs on Home Assistant and any Linux machine without installing Bun. Behaviour and flags are otherwise the same, plus some security hardening (see [Security notes](#security-notes)). Credit for the original idea and design goes to the upstream author.

Let your main Claude Code instance communicate with remote Claude Code instances over SSH. Instead of copying messages back and forth between terminal windows, your Claudes can just talk to each other directly.

Born from the annoyance of manually relaying messages between Claude Code sessions on different servers. Each Claude knows its own machine best — now they can collaborate without you playing telephone.

> This is just how my system is set up. Feel free to fork, improve, and personalize it for yourself.

```
┌─────────────────┐         SSH + stdin          ┌─────────────────┐
│  Claude Code    │ ──────────────────────────>  │  Claude Code    │
│  (interactive)  │                              │  (headless)     │
│  your laptop    │  <────────────────────────── │  remote server  │
│                 │     JSON response + session  │                 │
└─────────────────┘                              └─────────────────┘
```

Your local Claude sends a prompt to a remote machine via SSH, where another Claude Code instance runs it headlessly (`claude -p`), and the response comes back. Sessions are tracked so you can have multi-turn conversations across machines.

## Python port / Home Assistant

This fork replaces the Bun/TypeScript script with `dm-claude.py`: standard library only, Python 3.8+, plus the `ssh` client. Nothing to `pip install`, so it runs on Linux boxes, Raspberry Pis and Home Assistant (e.g. the Advanced SSH & Terminal add-on, or a `shell_command` / container with `openssh-client`). Flags and output are identical to the original. Fork of [jthack/dm-other-claudes](https://github.com/jthack/dm-other-claudes).

Only the *calling* side needs Python; the remote side still needs the Claude Code CLI.

## Prerequisites

- Python 3.8+ (standard library only, no pip install) on the calling machine
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated on **both** local and remote machines
- SSH key-based auth to the remote machine(s)

## Installation

### 1. Copy the skill files

```bash
mkdir -p ~/.claude/skills/dm-other-claudes
cp dm-claude.py SKILL.md ~/.claude/skills/dm-other-claudes/
```

### 2. Tell Claude how to use it

Add something like this to a project-level `CLAUDE.md`. Keep it per project rather than global, so the hosts a session may reach are listed where the work is and every call stays visible to you:

```markdown
### Cross-Claude Communication (dm-claude)

I can send tasks to Claude Code instances on remote machines using the dm-claude tool.

**Tool location**: `python3 ~/.claude/skills/dm-other-claudes/dm-claude.py`

**My remote agents**:
- `alice@10.0.1.50` — build server (has access to CI/CD, Docker, test suites)
- `bob@devbox.example.com` — dev environment (has the full monorepo checked out)

**Usage**:
- New task: `python3 ~/.claude/skills/dm-other-claudes/dm-claude.py user@host "task"`
- Follow-up: `python3 ~/.claude/skills/dm-other-claudes/dm-claude.py -s SESSION_ID user@host "follow-up"`
- Add `--skip-permissions` only when I have approved that specific task on that host (it gives the remote Claude unrestricted shell access)
- Use `-t 1200` for long-running tasks (default timeout is 600s)

**When to use**: Delegate tasks that need to run on specific machines — deployments, log analysis,
service health checks, builds, or any work that requires access to that machine's filesystem/tools.
```

**Important**: Replace the example hosts above with your actual remote machines. The `CLAUDE.md` entry is what teaches your Claude *where* to send tasks and *when* to use this tool. Without it, Claude won't know your remote agents exist.

### 3. Verify it works

```bash
# Test SSH + Claude on the remote
ssh user@your-server "claude --version"

# Test the full round-trip
python3 ~/.claude/skills/dm-other-claudes/dm-claude.py user@your-server "echo hello from the remote side"
```

## Usage

```bash
# New conversation
python3 dm-claude.py user@server "check disk usage and report back"

# Continue a conversation (session ID is printed after each call)
python3 dm-claude.py -s SESSION_ID user@server "now clean up the /tmp directory"

# Autonomous mode (remote Claude can use tools without asking)
python3 dm-claude.py --skip-permissions user@server "deploy the latest main branch"

# Read prompt from a file
python3 dm-claude.py -f task.md user@server

# Override model, increase timeout
python3 dm-claude.py --model claude-sonnet-4-6 -t 1200 user@server "long analysis task"
```

### Options

| Flag | Description |
|------|-------------|
| `-s, --session <id>` | Resume a previous session |
| `-t, --timeout <sec>` | Timeout in seconds (default: 600) |
| `-f, --file <path>` | Read prompt from file |
| `--skip-permissions` | Autonomous mode on remote (opt-in: unrestricted shell access as the remote user) |
| `--max-turns <n>` | Limit tool-use turns |
| `--model <model>` | Override remote model |
| `--remote-claude <path>` | Absolute path to `claude` on the remote, for when it is not on a non-interactive SSH `PATH` (e.g. Home Assistant) |
| `--remote-env KEY=VALUE` | Set an environment variable for remote claude (repeatable); only allow-listed names are accepted, currently `CLAUDE_CONFIG_DIR` (e.g. `CLAUDE_CONFIG_DIR=/config/.claude-ssh`); anything else (`PATH`, `NODE_OPTIONS`, `ANTHROPIC_BASE_URL`, `LD_PRELOAD`, ...) is refused. Extend `REMOTE_ENV_ALLOW` in the script if you need more |
| `-v, --verbose` | Debug output |

## How It Works

1. **Phase 1 — Upload**: A private directory `/tmp/dm-claude-<random>/` is created on the remote and the prompt is uploaded into it via `ssh ... cat > /tmp/dm-claude-<random>/prompt`. This avoids all shell escaping issues.
2. **Phase 2 — Execute**: Claude runs headlessly reading from that file: `claude -p --output-format json < /tmp/dm-claude-<random>/prompt`
3. **Parse**: The JSON response is parsed for the result text, session ID, cost, and duration.
4. **Cleanup**: The prompt file is deleted as soon as claude has opened it, and the pid file and directory when the run ends. If the run times out or `dm-claude.py` receives Ctrl-C, `SIGTERM` or `SIGHUP`, it also stops the remote claude and its child processes (exit codes 124, 130 and 128+signal) instead of leaving it running unattended.

For new conversations, a preamble is prepended explaining to the remote Claude that it's receiving a cross-Claude task. On follow-ups (`--session`), the preamble is skipped since the remote already has context.

## Security notes

- **Host keys:** SSH uses `StrictHostKeyChecking=accept-new` (trust on first use, refuse if the key later changes), `BatchMode=yes`, and no agent/port forwarding. The original used `StrictHostKeyChecking=no`, which allows silent MITM.
- **Input validation:** `<user@host>` must not start with `-` (ssh would treat it as an option such as `-oProxyCommand=...`); `--` precedes the target; session IDs are restricted to `[A-Za-z0-9_-]`; all patterns are applied with `fullmatch` (a `$` anchor alone also accepts a trailing newline); all remote command parts are shell-quoted. `--remote-env` is an allow-list (currently `CLAUDE_CONFIG_DIR`), because variables such as `NODE_OPTIONS`, `ANTHROPIC_BASE_URL` or `PATH` can load code or redirect credentials.
- **Remote files:** each run gets its own directory, `/tmp/dm-claude-<128-bit random>/`, created with `umask 077; mkdir`. `mkdir` is atomic and fails if the name already exists as anything (a file, FIFO, symlink or directory), so a local user on the remote cannot pre-plant the name or redirect the write; the directory is mode 700, so nothing in it can be created, replaced or read by anyone else. (Noclobber, `set -C`, would not do: it only protects regular files and writes through a FIFO or a symlink to a device.) This relies on `/tmp` having the sticky bit, as it does by default. The prompt is deleted as soon as it is opened.
- **Timeout kill:** the remote pid is read back from the pid file, and only a plain decimal number of at most 8 digits without a leading zero, greater than 1 and naming a live process, is signalled. The length cap matters: zsh and ksh truncate `kill`'s argument to 32 bits, so `4294967296` would otherwise mean "our own process group". The log says what actually happened. Children that detach themselves (`nohup`, `setsid`) escape the process-tree walk.
- **Output:** every remote-derived string (reply, stderr, error subtype) is stripped of ANSI escapes, C0/C1 control characters, lone surrogates, and characters a human cannot see but a model can read: soft hyphen, Arabic letter mark, zero-width space, line/paragraph separators, bidi embeddings, overrides and isolates, word joiner and invisible operators, BOM, interlinear annotation, and the Unicode tag block (the "ASCII smuggling" channel). Every logged line is prefixed with `[dm-claude]`, so multi-line remote text cannot forge a status line such as `session_id:`. **Not stripped, on purpose:** ZWNJ/ZWJ (Persian, Indic scripts, emoji sequences), LRM/RLM (mixed-direction text) and variation selectors (emoji presentation, CJK) are needed by legitimate text, so they remain a possible covert channel. Plain-text prompt injection cannot be filtered at all: treat replies as untrusted data.
- **`--skip-permissions`** is full shell access as the remote user. Use it only on hosts and tasks you trust, and don't pass untrusted content into such prompts. Remote replies are untrusted data.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Offline, standard library only, under half a minute. The unit tests cover the input validators, the output sanitizer, `log()`, the remote command builder and the reply parser. The integration tests put a fake `ssh` (which runs the command string in a local shell, as the remote would; every one of `sh`, `dash`, `bash`, `zsh` and `ksh` that is installed gets a turn as the remote login shell, because they differ in what this script relies on) and a fake `claude` first on `PATH`, then check the normal round trip, the timeout kill of the whole process tree (and the same for Ctrl-C, `SIGTERM` and `SIGHUP`, with no files left behind), that a pre-planted FIFO, symlink or directory at the run-directory name is refused, and that hostile pid-file contents are handled safely. Nothing touches the network; the tests write only to a temp directory and to `/tmp/dm-claude-<random>/`.

## Tips

- **Use IP addresses** if hostname resolution is flaky on your network
- **Session IDs are per-machine** — they reference `~/.claude/` on the remote host
- **Each call is a separate API call** on the remote — cost is reported in stderr
- **The remote Claude follows its own `CLAUDE.md`** — give it relevant instructions on that machine too
- **For parallel work**, fire off multiple `dm-claude` calls simultaneously (e.g., using `&` in bash or your editor's background task support)

## License

This is a derivative work with two layers of licensing:

- **Upstream-derived portions** (the original design, preamble text and docs from [jthack/dm-other-claudes](https://github.com/jthack/dm-other-claudes)) remain under the **MIT license**. The notice is kept in [`LICENSE-MIT`](LICENSE-MIT).
- **Modifications and new code** (Copyright (c) 2026 George W Walford; the Python port, security hardening, and related docs) are licensed under the **GNU General Public License, version 2 only** (`GPL-2.0-only`). See [`COPYING`](COPYING). Not "or later".

MIT is GPL-compatible, so the combined work can be distributed under GPL v2 provided the MIT notice is retained. The original, unmodified upstream remains available under MIT from its own repository.
