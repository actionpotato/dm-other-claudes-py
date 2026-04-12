#!/usr/bin/env bun
/**
 * dm-claude.ts - Cross-Claude Communication Tool
 *
 * Sends tasks to Claude Code instances on remote machines via SSH.
 * Supports session continuity for multi-turn conversations.
 *
 * Usage:
 *   bun run dm-claude.ts <user@host> "prompt"
 *   bun run dm-claude.ts -s <session-id> <user@host> "follow-up"
 */

import { randomUUID } from "crypto";

const PREAMBLE = `[CROSS-CLAUDE COMMUNICATION]
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
`;

// ── Argument Parsing ──────────────────────────────────────────────

const argv = Bun.argv.slice(2);
let sessionId: string | undefined;
let timeout = 600;
let skipPermissions = false;
let maxTurns: number | undefined;
let verbose = false;
let promptFile: string | undefined;
let modelOverride: string | undefined;
const positionals: string[] = [];

for (let i = 0; i < argv.length; i++) {
  switch (argv[i]) {
    case "-s":
    case "--session":
      sessionId = argv[++i];
      break;
    case "-t":
    case "--timeout":
      timeout = parseInt(argv[++i]);
      break;
    case "--skip-permissions":
      skipPermissions = true;
      break;
    case "--max-turns":
      maxTurns = parseInt(argv[++i]);
      break;
    case "--model":
      modelOverride = argv[++i];
      break;
    case "-f":
    case "--file":
      promptFile = argv[++i];
      break;
    case "-v":
    case "--verbose":
      verbose = true;
      break;
    case "-h":
    case "--help":
      printHelp();
      process.exit(0);
    default:
      positionals.push(argv[i]);
  }
}

function printHelp() {
  console.log(`dm-claude - Send tasks to Claude Code instances on remote machines

Usage:
  dm-claude.ts <user@host> "prompt"
  dm-claude.ts -s <session-id> <user@host> "follow-up"

Options:
  -s, --session <id>        Resume a previous session
  -t, --timeout <seconds>   SSH/command timeout (default: 600)
  -f, --file <path>         Read prompt from file
  --skip-permissions         Add --dangerously-skip-permissions on remote
  --max-turns <n>           Limit remote Claude's tool-use turns
  --model <model>           Override remote Claude's model
  -v, --verbose             Show debug info
  -h, --help                This help

Examples:
  # New conversation
  bun run dm-claude.ts user@myserver "list running services"

  # Continue conversation
  bun run dm-claude.ts -s abc123 user@myserver "restart the one on port 8080"

  # Fully autonomous remote task
  bun run dm-claude.ts --skip-permissions user@myserver "pull latest code and redeploy"

  # Long-running task with generous timeout
  bun run dm-claude.ts -t 1200 --skip-permissions user@myserver "run full analysis on target.com"
`);
}

if (positionals.length < 2 && !promptFile) {
  console.error("Error: need <user@host> and a prompt (or -f <file>)");
  console.error("Run with -h for help");
  process.exit(1);
}

const target = positionals[0]; // user@host
let prompt: string;
if (promptFile) {
  prompt = await Bun.file(promptFile).text();
  // If there are additional positionals after target, prepend them
  if (positionals.length > 1) {
    prompt = positionals.slice(1).join(" ") + "\n\n" + prompt;
  }
} else {
  prompt = positionals.slice(1).join(" ");
}

// Prepend preamble for new sessions, skip for follow-ups
const fullPrompt = sessionId ? prompt : PREAMBLE + prompt;

// ── Build Remote Claude Command ───────────────────────────────────

const remoteParts = ["claude"];
if (sessionId) remoteParts.push("--resume", sessionId);
remoteParts.push("-p", "--output-format", "json");
if (skipPermissions) remoteParts.push("--dangerously-skip-permissions");
if (maxTurns) remoteParts.push("--max-turns", String(maxTurns));
if (modelOverride) remoteParts.push("--model", modelOverride);
const remoteCmd = remoteParts.join(" ");

// ── Execute via SSH (Two-Phase: upload prompt, then run) ──────────
//
// Why two-phase? Avoids all shell escaping issues. The prompt goes
// through SSH stdin as raw bytes, lands in a temp file on the remote,
// then Claude reads from that file. Works with any prompt content.

const tmpId = randomUUID().slice(0, 8);
const tmpFile = `/tmp/dm-claude-${tmpId}.txt`;

console.error(
  `[dm-claude] -> ${target}${sessionId ? ` (session ${sessionId.slice(0, 12)}...)` : ""}`
);
if (verbose) {
  console.error(`[dm-claude] cmd: ${remoteCmd}`);
  console.error(`[dm-claude] prompt: ${fullPrompt.length} chars`);
  console.error(`[dm-claude] tmpFile: ${tmpFile}`);
}

// Phase 1: Upload prompt to temp file on remote
const uploadProc = Bun.spawn(
  [
    "ssh",
    "-T",
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    target,
    `cat > ${tmpFile}`,
  ],
  { stdin: "pipe", stdout: "pipe", stderr: "pipe" }
);

uploadProc.stdin.write(fullPrompt);
uploadProc.stdin.end();
const uploadExit = await uploadProc.exited;

if (uploadExit !== 0) {
  const uploadErr = await new Response(uploadProc.stderr).text();
  console.error(`[dm-claude] SSH connection failed: ${uploadErr.trim()}`);
  process.exit(1);
}

if (verbose) console.error(`[dm-claude] prompt uploaded to ${target}:${tmpFile}`);

// Phase 2: Run Claude reading from the temp file
const runProc = Bun.spawn(
  [
    "ssh",
    "-T",
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=20",
    target,
    `${remoteCmd} < ${tmpFile}; EXIT=$?; rm -f ${tmpFile}; exit $EXIT`,
  ],
  { stdin: "ignore", stdout: "pipe", stderr: "pipe" }
);

// Timeout safety net
const timeoutHandle = setTimeout(() => {
  console.error(`[dm-claude] TIMEOUT after ${timeout}s, killing...`);
  runProc.kill();
  // Clean up temp file
  Bun.spawn([
    "ssh", "-T", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
    target, `rm -f ${tmpFile}`,
  ]);
}, timeout * 1000);

const [stdout, stderr, exitCode] = await Promise.all([
  new Response(runProc.stdout).text(),
  new Response(runProc.stderr).text(),
  runProc.exited,
]);

clearTimeout(timeoutHandle);

if (verbose && stderr) console.error(`[dm-claude] stderr:\n${stderr}`);

if (exitCode !== 0 && !stdout.trim()) {
  console.error(`[dm-claude] FAILED (exit ${exitCode}): ${stderr.trim()}`);
  process.exit(exitCode || 1);
}

// ── Parse JSON Response ───────────────────────────────────────────

function tryParseResult(text: string): any {
  const lines = text.trim().split("\n");
  // Find the result JSON object (search from end)
  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      const obj = JSON.parse(lines[i]);
      if (obj.type === "result" || obj.session_id || obj.result !== undefined) {
        return obj;
      }
    } catch {}
  }
  // Try entire output as JSON
  try {
    return JSON.parse(text);
  } catch {}
  return null;
}

const parsed = tryParseResult(stdout);

if (parsed) {
  const sid = parsed.session_id;
  const result = parsed.result ?? parsed.text ?? "";
  const cost = parsed.cost_usd;
  const turns = parsed.num_turns;
  const duration = parsed.duration_ms;
  const isError = parsed.is_error;

  // Metadata to stderr (visible to calling Claude but separate from response)
  if (sid) {
    console.error(`[dm-claude] session_id: ${sid}`);
    console.error(
      `[dm-claude] continue: bun run dm-claude.ts -s ${sid} ${target} "follow-up"`
    );
  }
  if (cost !== undefined) {
    const d = duration ? `${(duration / 1000).toFixed(1)}s` : "?";
    console.error(
      `[dm-claude] cost=$${cost.toFixed(4)} turns=${turns ?? "?"} time=${d}${isError ? " ERROR" : ""}`
    );
  }

  // Response text to stdout
  console.log(result);
} else {
  // Couldn't parse JSON — output raw
  console.error("[dm-claude] warning: could not parse JSON, raw output follows");
  console.log(stdout);
}
