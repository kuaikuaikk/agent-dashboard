# agent-dashboard

A glanceable, real-time dashboard for Claude Code sessions. Designed to be
opened on any device on your LAN — an old smartphone on a charging stand
makes a great ambient display.

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

## Install

### As a Claude Code plugin (recommended)

```
/plugin marketplace add kuaikuaikk/agent-dashboard
/plugin install agent-dashboard
```

That's it. Hooks auto-wire, the daemon spawns on the first hook event, and
the first run bootstraps a Python venv with `aiohttp` + `qrcode` (~10s,
one-time). Run `/pair` inside any Claude Code session to add a device.

Requires: Python 3.11+ on PATH.

### Manually (for development / no-plugin setup)

```bash
git clone https://github.com/kuaikuaikk/agent-dashboard
cd agent-dashboard/.claude-plugin
python3 -m venv .venv
.venv/bin/pip install aiohttp qrcode
chmod +x hook.sh
```

Then merge `.claude-plugin/hooks/hooks.json` into your
`~/.claude/settings.json` by hand (replace `${CLAUDE_PLUGIN_ROOT}` with
the absolute path to the `.claude-plugin/` directory).

For *local plugin development* (live-edit), symlink the repo into the
marketplaces dir instead of installing:

```bash
ln -s "$(pwd)" ~/.claude/plugins/marketplaces/agent-dashboard
```

Now edits in this repo are picked up as if the plugin were installed
normally — no `/plugin update` needed.

## Pair a device

Once installed, run:

```
/pair
```

inside any Claude Code session. Safari (macOS) auto-opens with a QR code.
Scan it with your phone — the page on the Mac switches to a green "✓ paired"
state once consumed, and the link is then permanently spent.

Each pairing creates an independent device entry. Tokens are SHA-256 hashed
on disk; the plaintext token is shown once and is **single-use** (re-using
the same URL returns 403).

```bash
# list / revoke without /pair:
.venv/bin/python daemon.py --list
.venv/bin/python daemon.py --revoke <id-prefix>
```

## HTTPS (recommended for iOS ambient use)

iOS Safari's Wake Lock API only works in a secure context. Plain HTTP on a
LAN IP won't keep the screen on. Two options:

**Tailscale (easiest):**

```bash
brew install --cask tailscale-app
# log in on Mac and iPhone with the same account
# enable HTTPS in https://login.tailscale.com/admin/dns
tailscale serve --bg http://127.0.0.1:8765
```

`daemon.py --pair` then auto-detects the Tailscale hostname and embeds an
HTTPS URL in the QR. Real Let's Encrypt cert, no profile install.

**Other options:** set `DASHBOARD_PUBLIC_URL=https://my.example.com` to
override (e.g., behind Caddy or a self-signed cert with an iOS trust
profile).

Without HTTPS, the dashboard still works; only the keep-awake feature
silently degrades.

## (Optional) Add to Home Screen

On iOS Safari, tap **Share → Add to Home Screen** for a fullscreen,
kiosk-style launcher. Pair with Guided Access (Settings → Accessibility) to
lock the phone to this page.

## Plugin layout

All plugin assets live under `.claude-plugin/` (Claude Code's `source`
field points there). README/LICENSE stay at the repo root.

```
agent-dashboard/
├── .claude-plugin/
│   ├── plugin.json         # manifest
│   ├── marketplace.json    # so `/plugin marketplace add` finds the plugin
│   ├── hooks/hooks.json    # auto-wires 7 hook events to hook.sh
│   ├── commands/pair.md    # /pair slash command (invoked as `/agent-dashboard:pair`)
│   ├── daemon.py           # hook handler + server + pair/list/revoke CLI
│   ├── hook.sh             # auto-bootstraps venv, then execs daemon.py --hook
│   ├── index.html          # single-file frontend
│   ├── devices.json        # paired-device list (token hashes; gitignored)
│   └── .venv/              # local Python env (gitignored)
├── LICENSE
└── README.md
```

## State machine

| Hook event                          | Tile state           | Color  | Pulses |
|-------------------------------------|----------------------|--------|--------|
| SessionStart                        | idle                 | grey   |        |
| UserPromptSubmit / PostToolUse      | thinking             | cyan   | yes    |
| PreToolUse                          | tool_use             | yellow | yes    |
| Notification + "permission/approve" | awaiting_approval    | purple | yes    |
| Notification + "waiting/idle/input" | idle_waiting         | amber  | yes    |
| Notification (other)                | notification         | orange |        |
| Stop                                | done                 | green  |        |
| SessionEnd                          | (tile removed)       | —      |        |

Pulses auto-decay after 5 min of no further activity so the tile stops
nagging. The frontend also dims itself after extended idle and shifts ±2px
every minute to prevent OLED burn-in.

## Runtime files

| Path                                | Purpose |
|-------------------------------------|---------|
| `/tmp/agent-dashboard-state.json`   | current state, indexed by Claude session id |
| `/tmp/agent-dashboard.lock`         | flock around state-file write |
| `/tmp/agent-dashboard-spawn.lock`   | flock around daemon spawn |
| `/tmp/agent-dashboard-devices.lock` | flock around devices.json mutations |
| `/tmp/agent-dashboard-daemon.pid`   | running daemon's PID |
| `/tmp/agent-dashboard-daemon.log`   | daemon stdout/stderr |
| `/tmp/agent-dashboard-bootstrap.log`| first-run venv setup log |
| `./devices.json`                    | paired devices (persistent, in plugin dir) |

## Caveats

- **All paired devices have full read access.** The `scopes` field exists
  but isn't enforced yet — every authenticated device sees every session.
- **Daemon auto-exits after 5h idle.** Next hook event respawns it (~1.5s).
- **State persists in `/tmp`**, wiped on reboot. Re-populates as you use
  Claude Code.
- **Single laptop only.** No multi-machine aggregation yet.
- **Claude Code only.** Codex / opencode adapters are future work.

## Manual sanity test

```bash
echo '{"hook_event_name":"UserPromptSubmit","session_id":"test","cwd":"/tmp"}' \
  | ./hook.sh
cat /tmp/agent-dashboard-state.json   # should show a test session
tail -f /tmp/agent-dashboard-daemon.log
```

## Reset / panic buttons

```bash
# kick all paired devices
rm devices.json

# wipe live session state
rm /tmp/agent-dashboard-state.json

# stop the daemon
lsof -ti:8765 | xargs kill

# completely start over
rm -rf .venv devices.json /tmp/agent-dashboard-*
```

## License

MIT — see [LICENSE](./LICENSE).
