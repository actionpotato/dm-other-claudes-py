# dm-with-claude

Send tasks between Claude Code instances on different machines over SSH. Claude-to-Claude collaboration with session continuity.

```
┌─────────────────┐         SSH + stdin          ┌─────────────────┐
│  Claude Code    │ ──────────────────────────>  │  Claude Code    │
│  (interactive)  │                              │  (headless)     │
│  your laptop    │  <────────────────────────── │  remote server   │
│                 │     JSON response + session   │                 │
└─────────────────┘                              └─────────────────┘
```

Your local Claude sends a prompt to a remote machine via SSH, where another Claude Code instance runs it headlessly (`claude -p`), and the response comes back. Sessions are tracked so you can have multi-turn conversations across machines.

## Prerequisites

- [Bun](https://bun.sh) installed on your local machine
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated on **both** local and remote machines
- SSH key-based auth to the remote machine(s)

## Installation

### 1. Copy the skill files

```bash
mkdir -p ~/.claude/skills/dm-other-claudes
cp dm-claude.ts SKILL.md ~/.claude/skills/dm-other-claudes/
```

### 2. Tell Claude how to use it

Add something like this to your `CLAUDE.md` (either `~/.claude/CLAUDE.md` for global, or a project-level one):

```markdown
### Cross-Claude Communication (dm-claude)

I can send tasks to Claude Code instances on remote machines using the dm-claude tool.

**Tool location**: `bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts`

**My remote agents**:
- `alice@10.0.1.50` — build server (has access to CI/CD, Docker, test suites)
- `bob@devbox.example.com` — dev environment (has the full monorepo checked out)

**Usage**:
- New task: `bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts --skip-permissions user@host "task"`
- Follow-up: `bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts -s SESSION_ID user@host "follow-up"`
- Use `--skip-permissions` for autonomous tasks (no human on remote to approve prompts)
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
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts user@your-server "echo hello from the remote side"
```

## Usage

```bash
# New conversation
bun run dm-claude.ts user@server "check disk usage and report back"

# Continue a conversation (session ID is printed after each call)
bun run dm-claude.ts -s SESSION_ID user@server "now clean up the /tmp directory"

# Autonomous mode (remote Claude can use tools without asking)
bun run dm-claude.ts --skip-permissions user@server "deploy the latest main branch"

# Read prompt from a file
bun run dm-claude.ts -f task.md user@server

# Override model, increase timeout
bun run dm-claude.ts --model claude-sonnet-4-6 -t 1200 user@server "long analysis task"
```

### Options

| Flag | Description |
|------|-------------|
| `-s, --session <id>` | Resume a previous session |
| `-t, --timeout <sec>` | Timeout in seconds (default: 600) |
| `-f, --file <path>` | Read prompt from file |
| `--skip-permissions` | Autonomous mode on remote |
| `--max-turns <n>` | Limit tool-use turns |
| `--model <model>` | Override remote model |
| `-v, --verbose` | Debug output |

## How It Works

1. **Phase 1 — Upload**: The prompt is uploaded to a temp file on the remote via `ssh ... cat > /tmp/dm-claude-xxx.txt`. This avoids all shell escaping issues.
2. **Phase 2 — Execute**: Claude runs headlessly reading from that temp file: `claude -p --output-format json < /tmp/dm-claude-xxx.txt`
3. **Parse**: The JSON response is parsed for the result text, session ID, cost, and duration.
4. **Cleanup**: The temp file is deleted after execution.

For new conversations, a preamble is prepended explaining to the remote Claude that it's receiving a cross-Claude task. On follow-ups (`--session`), the preamble is skipped since the remote already has context.

## Tips

- **Use IP addresses** if hostname resolution is flaky on your network
- **Session IDs are per-machine** — they reference `~/.claude/` on the remote host
- **Each call is a separate API call** on the remote — cost is reported in stderr
- **The remote Claude follows its own `CLAUDE.md`** — give it relevant instructions on that machine too
- **For parallel work**, fire off multiple `dm-claude` calls simultaneously (e.g., using `&` in bash or your editor's background task support)

## License

MIT
