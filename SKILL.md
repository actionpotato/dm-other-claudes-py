# dm-other-claudes — Cross-Claude Communication

Send tasks to Claude Code instances on remote machines. Enables Claude-to-Claude collaboration over SSH with session continuity.

## How It Works

```
┌─────────────────┐         SSH + stdin          ┌─────────────────┐
│  Claude (TUI)   │ ──────────────────────────>  │  Claude (-p)    │
│  interactive     │                              │  non-interactive │
│  with human     │  <────────────────────────── │  on remote host  │
│                 │     JSON response + session   │                 │
└─────────────────┘                              └─────────────────┘
```

1. **Calling side** (you, in the TUI): Invokes `dm-claude.ts` via Bash tool
2. **Remote side**: Runs `claude -p --output-format json` on the target machine
3. **Session tracking**: Remote Claude's session ID is captured for follow-ups
4. **Preamble**: Auto-prepended context tells the remote Claude it's receiving a cross-Claude task

## Setup

### Prerequisites
- [Bun](https://bun.sh) installed locally
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed on **both** local and remote machines
- SSH access to the remote machine(s) (key-based auth recommended)

### Installation

> **If the user asks you to install/set up this skill**, ask them:
> 1. Where are your other Claude Code agents running? (e.g., `user@hostname` or `user@ip`)
> 2. Do you have SSH key-based auth to those machines, or do you need to set it up?
> 3. Does the remote machine have Claude Code CLI installed and authenticated?
>
> Then configure the skill with their specific hosts and update the examples below accordingly.

1. Copy `dm-claude.ts` and `SKILL.md` into your Claude Code skills directory:
   ```bash
   mkdir -p ~/.claude/skills/dm-other-claudes
   cp dm-claude.ts SKILL.md ~/.claude/skills/dm-other-claudes/
   ```

2. Add a reference to this skill in your `CLAUDE.md` or project instructions so Claude knows how to use it. Example entry:
   ```markdown
   ### Cross-Claude Communication
   - Tool: `bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts`
   - Remote hosts:
     - `user@your-server-ip` — description of what runs there
   - Use `--skip-permissions` for autonomous tasks (no human on the remote end to approve)
   - Use `-s SESSION_ID` for follow-ups
   ```

3. Verify SSH connectivity:
   ```bash
   ssh user@your-server "claude --version"
   ```

## Usage

### Tool Location
```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts [options] <user@host> "prompt"
```

### New Conversation
```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts user@server "check if the web server is running and healthy"
```

### Continue Conversation (with session ID from previous call)
```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts -s SESSION_ID user@server "now restart it"
```

### Fully Autonomous Remote Task
```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts --skip-permissions user@server "pull latest code and redeploy the service"
```

### Options

| Flag | Description |
|------|-------------|
| `-s, --session <id>` | Resume a previous session on the remote |
| `-t, --timeout <sec>` | Timeout in seconds (default: 600 / 10min) |
| `-f, --file <path>` | Read prompt from a file |
| `--skip-permissions` | Add `--dangerously-skip-permissions` on remote (needed for autonomous tool use) |
| `--max-turns <n>` | Limit how many tool-use turns the remote Claude can take |
| `--model <model>` | Override the remote Claude's model |
| `-v, --verbose` | Debug output |

## How to Use From the TUI (Instructions for Claude)

When you (Claude in TUI) need to delegate work to a remote machine:

### Step 1: Send the initial task
```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts --skip-permissions user@server "your task description here"
```

The tool outputs:
- **stdout**: The remote Claude's response text
- **stderr**: Metadata including `session_id` and cost

### Step 2: Process the response
Read the response. If you need to follow up:

```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts -s SESSION_ID user@server "follow-up question or next task"
```

### Step 3: Report back to the user
Once the collaboration is complete, summarize what happened.

### Multi-Turn Pattern
For complex tasks that need back-and-forth:

```python
# Pseudocode for the pattern
response1 = dm-claude(host, "analyze the logs on this machine")
# Parse response1, extract session_id
response2 = dm-claude(host, session=sid, "now restart the failing service")
# Parse response2
response3 = dm-claude(host, session=sid, "confirm it's healthy")
# Report combined results to user
```

## What the Remote Claude Sees

The tool auto-prepends a preamble to the first message explaining:
- This is a cross-Claude communication
- The calling Claude is working with a human in a TUI
- It should be thorough but concise
- It should complete the task autonomously
- Follow-ups may arrive via `--resume`

On follow-up messages (`--session`), no preamble is added — the remote Claude already has the context from the resumed session.

## Important Notes

1. **Always use `--skip-permissions` for autonomous tasks** — without it, the remote Claude may not be able to use tools that require permission prompts (since there's no human on the remote end to approve)

2. **Use IP addresses if hostname resolution is unreliable** — if you have flaky DNS, use the IP directly (e.g., `user@192.168.1.100` instead of `user@myserver`)

3. **Timeout** — default is 10 minutes. For long tasks, increase with `-t 1200` or more

4. **Session IDs live on the remote machine** — they reference Claude sessions stored in `~/.claude/` on the remote host. The session ID only works with the same `user@host`

5. **Cost awareness** — each dm-claude call is a separate Claude API call on the remote machine. The tool reports cost in stderr

6. **The remote Claude has its own CLAUDE.md and skills** — it will follow its own instructions on that machine

## Common Patterns

### Delegate a task to a remote server
```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts --skip-permissions user@server \
  "Check the disk usage on /var and clean up any log files older than 7 days"
```

### Check on a service
```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts user@server \
  "Is the API server running? Check with curl localhost:8080/health and show me the process"
```

### Deploy code
```bash
bun run ~/.claude/skills/dm-other-claudes/dm-claude.ts --skip-permissions user@server \
  "cd ~/myapp && git pull && npm install && pm2 restart myapp"
```

### Parallel work across multiple machines
Use the Bash tool's `run_in_background` to send tasks to multiple machines simultaneously:
```bash
# In parallel:
bun run dm-claude.ts --skip-permissions user@server1 "task for server 1"
bun run dm-claude.ts --skip-permissions user@server2 "task for server 2"
```
