# agent-dashboard (working name)

A glanceable, real-time dashboard for Claude Code sessions. Designed to be opened
on any device on your LAN — an old smartphone on a charging stand makes a great
ambient display.

```
Claude Code event
       │
       ▼
   hook.sh ──► daemon.py --hook ──► /tmp/agent-dashboard-state.json
                                          │
                                          ▼
                                    daemon.py (server)
                                          │
                                          ▼  WebSocket (cookie-auth'd)
                                    browser tile UI
```

## Quick start (~10 min)

### 1. Install dependencies (project-local venv)

Homebrew Python on macOS blocks system-wide installs (PEP 668), so we use a
project-local venv. Run this once from the project root:

```bash
cd /Users/martinyao/Workspace/agent-dashboard
python3 -m venv .venv
.venv/bin/pip install aiohttp qrcode
```

`hook.sh` automatically uses `.venv/bin/python` when present.

### 2. Make the hook executable

```bash
chmod +x hook.sh
```

### 3. Wire up Claude Code hooks

Merge the `hooks` block from `settings.local.json` into `~/.claude/settings.json`.
If your settings.json has no `hooks` block, just copy:

```bash
cp settings.local.json ~/.claude/settings.json
```

Otherwise, merge by hand — each event in `settings.local.json` is independent
and can be added one at a time.

### 4. Use Claude Code

The first hook event auto-spawns the daemon. Nothing else to start.

### 5. Pair a device

The daemon now requires auth — every device gets its own token. Add one with:

```bash
.venv/bin/python daemon.py --pair "iPhone"
```

This prints a QR code and a URL like
`http://<your-lan-ip>:8765/auth?token=<32-byte-secret>`.
Scan the QR with your phone (or open the URL in any browser). The token is
shown **once**; only its SHA-256 hash is persisted on disk.

After scanning, the browser gets a 1-year httpOnly cookie and the dashboard
is reachable at `http://<your-lan-ip>:8765/` for that device.

### 6. (Optional) Add to Home Screen

On iOS Safari, tap **Share → Add to Home Screen** for a fullscreen kiosk-style
launcher. Pair with Guided Access (Settings → Accessibility) to lock the phone
to this page and turn it into a dedicated ambient display.

## Device management

```bash
# list all paired devices (id, name, created, last_seen, user-agent)
.venv/bin/python daemon.py --list

# add another device (auto-named if no name given)
.venv/bin/python daemon.py --pair "MacBook"

# revoke one device by id prefix (use --list to see ids)
.venv/bin/python daemon.py --revoke f56f
```

Revoking removes the device's hash from `devices.json`; its token immediately
fails auth on every endpoint, without affecting any other paired device.

## File layout

```
agent-dashboard/
├── daemon.py            # hook handler + long-running server + pair/list/revoke CLI
├── hook.sh              # thin shim that calls `daemon.py --hook`
├── index.html           # single-file frontend (no build step)
├── settings.local.json  # snippet to merge into ~/.claude/settings.json
├── devices.json         # paired-device list (token hashes only; chmod 600)  [gitignored]
├── .venv/               # local Python env  [gitignored]
└── README.md
```

## State machine: Claude Code events → tile state

| Event                       | Tile state           | Color       | Pulses |
|-----------------------------|----------------------|-------------|--------|
| SessionStart                | idle                 | grey        |        |
| UserPromptSubmit / PostToolUse | thinking          | cyan        | yes    |
| PreToolUse                  | tool_use             | yellow      | yes    |
| Notification + "permission/approve" | awaiting_approval | purple | yes    |
| Notification + "waiting/idle/input" | idle_waiting    | amber       | yes    |
| Notification (other)        | notification         | orange      |        |
| Stop                        | done                 | green       |        |
| SessionEnd                  | (tile removed)       | —           |        |

`error` (red) is defined in CSS but no hook event currently maps to it.

## Runtime files

| Path                                       | Purpose |
|--------------------------------------------|---------|
| `/tmp/agent-dashboard-state.json`          | current state, indexed by Claude session id |
| `/tmp/agent-dashboard.lock`                | flock around state-file read-modify-write |
| `/tmp/agent-dashboard-spawn.lock`          | flock around daemon spawn (prevents double-start) |
| `/tmp/agent-dashboard-devices.lock`        | flock around devices.json mutations |
| `/tmp/agent-dashboard-daemon.pid`          | running daemon's PID |
| `/tmp/agent-dashboard-daemon.log`          | daemon stdout/stderr (plus hook errors) |
| `./devices.json`                           | paired devices (persistent, in project dir) |

## MVP caveats

- **No HTTPS yet.** Auth tokens travel over plaintext HTTP on your LAN. Fine on
  a trusted home WiFi; not safe on coffee-shop networks. Tailscale or mkcert
  would fix this.
- **All devices have full read access.** Scopes (`read:state` vs `write:approve`)
  aren't enforced yet — the field exists in devices.json but every authenticated
  device sees everything.
- **Daemon auto-exits after 5h idle.** Next hook event respawns it (~1.5s); the
  page reconnects with exponential backoff.
- **State persists in `/tmp`**, wiped on reboot. Sessions repopulate as you use
  Claude Code.
- **Single laptop only.** No multi-machine aggregation yet.
- **Claude Code only.** Codex / opencode / multi-CLI adapters are future work.

## Manual sanity test

```bash
# simulate a hook event without Claude Code
echo '{"hook_event_name":"UserPromptSubmit","session_id":"test-1","cwd":"/tmp"}' \
  | ./hook.sh

# inspect the state file
cat /tmp/agent-dashboard-state.json

# inspect daemon activity
tail -f /tmp/agent-dashboard-daemon.log
```

If `cat` prints a `test-1` session entry, the hook → state path works.

## Reset / panic buttons

```bash
# kick all paired devices, start fresh
rm devices.json

# wipe all live session state (page goes empty until next hook)
rm /tmp/agent-dashboard-state.json

# stop the daemon
lsof -ti:8765 | xargs kill

# completely start over: drop venv, devices, state, then redo step 1
rm -rf .venv devices.json /tmp/agent-dashboard-*
```
