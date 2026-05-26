#!/usr/bin/env python3
"""
agent-dashboard daemon

Modes:
    python3 daemon.py                  # long-running HTTP + WebSocket server
    python3 daemon.py --hook           # one-shot: read Claude Code hook from stdin
    python3 daemon.py --pair [name]    # add a device; print QR + URL with a fresh
                                       # per-device token (only its hash is stored)
    python3 daemon.py --list           # list paired devices
    python3 daemon.py --revoke <id>    # remove a device by id prefix

State file:    /tmp/agent-dashboard-state.json
PID file:      /tmp/agent-dashboard-daemon.pid
Log:           /tmp/agent-dashboard-daemon.log
Devices:       ./devices.json   (token hashes only; chmod 600)
"""

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path("/tmp/agent-dashboard-state.json")
LOCK_FILE  = Path("/tmp/agent-dashboard.lock")
PID_FILE   = Path("/tmp/agent-dashboard-daemon.pid")
LOG_FILE   = Path("/tmp/agent-dashboard-daemon.log")

DEVICES_FILE      = Path(__file__).parent / "devices.json"
DEVICES_LOCK_FILE = Path("/tmp/agent-dashboard-devices.lock")
LEGACY_TOKEN_FILE = Path(__file__).parent / "auth.token"  # migrated on first run
COOKIE_NAME       = "dash_auth"

HOST = "0.0.0.0"           # listen on all interfaces so phones on LAN can connect
PORT = 8765
POLL_INTERVAL_S = 0.2
IDLE_TIMEOUT_S = 5 * 60 * 60      # daemon self-exits after 5h with no state changes

PENDING_TTL_S      = 60 * 60      # an un-scanned --pair QR is discarded after 1h
IDLE_DEVICE_TTL_S  = 48 * 3600    # any device with no contact for 48h is revoked
CLEANUP_INTERVAL_S = 5 * 60       # how often the cleanup task scans devices.json


# ---------- state file helpers ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def state_lock():
    """Cross-process exclusive lock around read-modify-write of state file."""
    with open(LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_state() -> dict:
    # Safe to read without the lock: write_state uses atomic rename, so
    # readers always see a complete file (old or new version, never partial).
    if not STATE_FILE.exists():
        return {"sessions": {}, "updated_at": _now_iso()}
    try:
        with STATE_FILE.open("r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"sessions": {}, "updated_at": _now_iso()}


def write_state(state: dict) -> None:
    # Per-PID tmp file so concurrent writers don't clobber each other's tmp.
    # Atomic rename guarantees readers see a complete file.
    tmp = Path(f"{STATE_FILE}.tmp.{os.getpid()}")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


# ---------- auth: per-device tokens ----------

@contextlib.contextmanager
def devices_lock():
    with open(DEVICES_LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _hash_token(plain: str) -> str:
    return "sha256:" + hashlib.sha256(plain.encode()).hexdigest()


def load_devices() -> dict:
    if not DEVICES_FILE.exists():
        return {"devices": []}
    try:
        return json.loads(DEVICES_FILE.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {"devices": []}


def save_devices(data: dict) -> None:
    tmp = Path(f"{DEVICES_FILE}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(DEVICES_FILE)


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def parse_ua(ua: str) -> str:
    """Derive a friendly device label from a browser User-Agent string."""
    if not ua:
        return "device"
    if   "iPhone"     in ua: dev = "iPhone"
    elif "iPad"       in ua: dev = "iPad"
    elif "Android"    in ua: dev = "Android"
    elif "Macintosh"  in ua: dev = "Mac"
    elif "Windows"    in ua: dev = "Windows"
    elif "Linux"      in ua: dev = "Linux"
    else:                    dev = "device"
    if   "Edg/"       in ua: br = "Edge"
    elif "OPR/"       in ua: br = "Opera"
    elif "Firefox/"   in ua: br = "Firefox"
    elif "Chrome/" in ua and "Safari/" in ua: br = "Chrome"
    elif "Mobile" in ua and "Safari/" in ua:  br = "Safari"
    elif "Safari/"    in ua: br = "Safari"
    else:                    br = ""
    return f"{dev} {br}".strip()


def create_device(name: str | None = None) -> tuple[str, str]:
    """Create a new device. Returns (id, plaintext_token).
    The plaintext token is shown ONCE; only its hash is persisted.
    The device is created in 'pending' state — if no one ever scans within
    PENDING_TTL_S, the cleanup task removes it."""
    device_id = uuid.uuid4().hex[:8]
    token = secrets.token_urlsafe(32)
    pending_until = (_now_dt() + timedelta(seconds=PENDING_TTL_S)).isoformat()
    with devices_lock():
        data = load_devices()
        data["devices"].append({
            "id":            device_id,
            "name":          name or "(pending pair)",
            "token_hash":    _hash_token(token),
            "scopes":        ["read"],
            "created_at":    _now_iso(),
            "last_seen_at":  None,
            "pending_until": pending_until,
            "user_agent":    None,
        })
        save_devices(data)
    return device_id, token


def find_device_by_token(plain: str) -> dict | None:
    if not plain:
        return None
    h = _hash_token(plain)
    for d in load_devices().get("devices", []):
        stored = d.get("token_hash", "")
        if stored and secrets.compare_digest(stored, h):
            return d
    return None


def update_last_seen(device_id: str, user_agent: str | None) -> None:
    """Touch a device: update last_seen, commit (clear pending_until), and
    backfill name from User-Agent if still a placeholder. Any successful
    request is enough to commit a pending pairing."""
    try:
        with devices_lock():
            data = load_devices()
            for d in data.get("devices", []):
                if d.get("id") == device_id:
                    d["last_seen_at"]  = _now_iso()
                    d["pending_until"] = None
                    if user_agent:
                        if not d.get("user_agent"):
                            d["user_agent"] = user_agent[:200]
                        cur = d.get("name", "")
                        if cur.startswith(("(pending", "device-")) or not cur:
                            d["name"] = parse_ua(user_agent)
                    break
            save_devices(data)
    except Exception:
        pass  # best-effort; never fail a request because of this


def cleanup_devices() -> list[dict]:
    """Remove devices that are either:
    - pending past PENDING_TTL_S (QR generated but never scanned), or
    - inactive past IDLE_DEVICE_TTL_S (no contact for too long).
    Returns the list of removed devices for logging."""
    now = _now_dt()
    idle_cutoff = now - timedelta(seconds=IDLE_DEVICE_TTL_S)
    removed: list[dict] = []
    with devices_lock():
        data = load_devices()
        kept = []
        for d in data.get("devices", []):
            pu = _parse_iso(d.get("pending_until"))
            if pu and pu < now and not d.get("last_seen_at"):
                removed.append({**d, "_reason": "pending expired"})
                continue
            last_active = _parse_iso(d.get("last_seen_at")) or _parse_iso(d.get("created_at"))
            if last_active and last_active < idle_cutoff:
                removed.append({**d, "_reason": "idle > 48h"})
                continue
            kept.append(d)
        if removed:
            data["devices"] = kept
            save_devices(data)
    return removed


def revoke_device_by_prefix(prefix: str) -> dict | None:
    with devices_lock():
        data = load_devices()
        matches = [d for d in data["devices"] if d["id"].startswith(prefix)]
        if len(matches) != 1:
            return None  # zero or ambiguous match
        data["devices"] = [d for d in data["devices"] if d["id"] != matches[0]["id"]]
        save_devices(data)
    return matches[0]


def migrate_legacy_token() -> None:
    """One-shot: fold the old shared `auth.token` into devices.json so the
    already-paired phone keeps working, then delete the legacy file."""
    if not LEGACY_TOKEN_FILE.exists():
        return
    legacy = LEGACY_TOKEN_FILE.read_text().strip()
    if not legacy:
        LEGACY_TOKEN_FILE.unlink(missing_ok=True)
        return
    with devices_lock():
        data = load_devices()
        h = _hash_token(legacy)
        if not any(d.get("token_hash") == h for d in data["devices"]):
            data["devices"].append({
                "id":           uuid.uuid4().hex[:8],
                "name":         "Legacy (migrated)",
                "token_hash":   h,
                "scopes":       ["read"],
                "created_at":   _now_iso(),
                "last_seen_at": None,
                "user_agent":   "(migrated from auth.token)",
            })
            save_devices(data)
    LEGACY_TOKEN_FILE.unlink(missing_ok=True)


def get_lan_ip() -> str:
    """Best-effort detection of the LAN-facing IPv4 address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_pair_base_url() -> str:
    """Return the base URL to embed in pair QR codes.

    Resolution order:
    1. DASHBOARD_PUBLIC_URL env var (e.g., 'https://my.example.com')
    2. Tailscale serve: if `tailscale serve` is proxying our local PORT over
       HTTPS, use its tailnet hostname — gives a real Let's Encrypt cert and
       therefore a secure context, which the iOS Wake Lock API requires.
    3. Fallback: http://<lan-ip>:<PORT>
    """
    env = os.environ.get("DASHBOARD_PUBLIC_URL")
    if env:
        return env.rstrip("/")
    try:
        out = subprocess.run(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout)
            for hostport, cfg in (data.get("Web") or {}).items():
                for _path, h in (cfg.get("Handlers") or {}).items():
                    if h.get("Proxy", "").endswith(f":{PORT}"):
                        host = hostport.rsplit(":", 1)[0]
                        return f"https://{host}"
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, FileNotFoundError):
        pass
    return f"http://{get_lan_ip()}:{PORT}"


# ---------- transcript tailing (Claude Code's per-session jsonl) ----------
#
# Each Claude Code session writes a .jsonl transcript at the `transcript_path`
# we receive in hook payloads. Two entry types are useful for tile context:
#   {"type":"ai-title",    "aiTitle":"..."}      ← Claude's auto-summary
#   {"type":"last-prompt", "lastPrompt":"..."}   ← latest user message
# We tail just the file tail (~64 KB) and cache results by mtime so repeat
# reads of unchanged files cost nothing.

_TRANSCRIPT_CACHE: dict[str, tuple[float, dict]] = {}


def read_transcript_meta(path: str) -> dict:
    """Return the latest ai-title and last-prompt from a Claude session jsonl."""
    if not path:
        return {}
    try:
        st = os.stat(path)
    except OSError:
        return {}
    cached = _TRANSCRIPT_CACHE.get(path)
    if cached and cached[0] == st.st_mtime:
        return cached[1]

    title = None
    last_prompt = None
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            offset = max(0, size - 64 * 1024)
            f.seek(offset)
            tail = f.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()
        # If we started mid-file, the first line may be a partial — drop it.
        if offset > 0 and lines:
            lines = lines[1:]
        for line in reversed(lines):
            if title and last_prompt:
                break
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "ai-title" and not title:
                title = d.get("aiTitle")
            elif t == "last-prompt" and not last_prompt:
                last_prompt = d.get("lastPrompt")
    except OSError:
        pass

    meta = {}
    if title:       meta["title"] = title
    if last_prompt: meta["last_prompt"] = last_prompt
    _TRANSCRIPT_CACHE[path] = (st.st_mtime, meta)
    return meta


# ---------- hook mode ----------

EVENT_TO_STATE = {
    "SessionStart":      "idle",
    "UserPromptSubmit":  "thinking",
    "PreToolUse":        "tool_use",
    "PostToolUse":       "thinking",
    "Stop":              "done",
    # "Notification" handled specially (subtype depends on message content)
    # "SessionEnd" handled specially (removes the session)
}

ACTIVE_STATES = {"thinking", "tool_use", "done"}


def state_for_event(event: str, payload: dict) -> str | None:
    """Map hook event + payload to a tile state. Returns None to keep prior state."""
    if event == "Notification":
        msg = (payload.get("message") or "").lower()
        if "permission" in msg or "approve" in msg or "approval" in msg:
            return "awaiting_approval"
        if "waiting" in msg or "idle" in msg or "input" in msg:
            return "idle_waiting"
        return "notification"
    return EVENT_TO_STATE.get(event)


def hook_main() -> None:
    """Process one Claude Code hook event. Reads JSON from stdin.
    Wraps everything in try/except so the hook is never blocking or noisy."""
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {}

        event = (
            payload.get("hook_event_name")
            or os.environ.get("CLAUDE_HOOK_EVENT")
            or "Unknown"
        )
        session_id = (
            payload.get("session_id")
            or os.environ.get("CLAUDE_SESSION_ID")
            or "unknown"
        )
        cwd = payload.get("cwd") or os.getcwd()
        tool_name = payload.get("tool_name")
        transcript_path = payload.get("transcript_path")

        # Debug: record every hook event so we can reconstruct timelines.
        try:
            with open(LOG_FILE, "a") as logf:
                notif_kind = ""
                if event == "Notification":
                    msg = (payload.get("message") or "")[:60]
                    notif_kind = f' msg="{msg}"'
                logf.write(
                    f"{_now_iso()}  hook  {event:22s} "
                    f"sid={session_id[:8]} tool={tool_name or '-'}{notif_kind}\n"
                )
        except Exception:
            pass

        with state_lock():
            state = read_state()
            sessions = state.setdefault("sessions", {})

            if event == "SessionEnd":
                sessions.pop(session_id, None)
            else:
                sess = sessions.setdefault(session_id, {
                    "session_id": session_id,
                    "agent_type": "claude-code",
                    "started_at": _now_iso(),
                    "cwd": cwd,
                })
                sess["last_event_at"] = _now_iso()
                sess["last_event"] = event
                sess["cwd"] = cwd
                if tool_name:
                    sess["last_tool"] = tool_name
                if transcript_path:
                    sess["transcript_path"] = transcript_path

                new_state = state_for_event(event, payload)
                if new_state:
                    # Reset state_since only when the state value actually
                    # changes — so the tile timer measures "time in this state".
                    if new_state != sess.get("state") or "state_since" not in sess:
                        sess["state_since"] = _now_iso()
                    sess["state"] = new_state

                # `message` only carries system-side notification text
                # (e.g. "permission needed", "waiting for input"). We
                # deliberately do NOT echo the user's prompt — anyone glancing
                # at the dashboard from across the room would otherwise read
                # private input. Claude's abstracted `ai-title` is fine; the
                # literal prompt is not.
                if event == "Notification":
                    notif = payload.get("message")
                    if notif:
                        sess["message"] = notif[:300]
                elif event in ("UserPromptSubmit", "Stop", "PreToolUse", "PostToolUse"):
                    # Clear any leftover notification text once the session
                    # moves on (e.g. user approved, agent kept going).
                    sess.pop("message", None)

            state["updated_at"] = _now_iso()
            write_state(state)

        ensure_daemon_running()
    except Exception:
        # Hooks must not surface errors to Claude Code. Log and exit clean.
        try:
            with open(LOG_FILE, "a") as f:
                f.write(f"--- hook error {_now_iso()} ---\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
    sys.exit(0)


SPAWN_LOCK = Path("/tmp/agent-dashboard-spawn.lock")


def ensure_daemon_running() -> None:
    """Spawn a detached daemon if no live one is recorded.
    Serialized via fcntl flock so concurrent hooks can't double-spawn."""
    with open(SPAWN_LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            if PID_FILE.exists():
                try:
                    pid = int(PID_FILE.read_text().strip() or "0")
                    if pid > 0:
                        os.kill(pid, 0)
                        return  # daemon already alive
                except (OSError, ValueError):
                    pass  # stale or empty — fall through to spawn

            script = os.path.abspath(__file__)
            log_f = open(LOG_FILE, "a")
            proc = subprocess.Popen(
                [sys.executable, script],
                stdout=log_f,
                stderr=log_f,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            # Write child PID while still holding the lock so the next hook,
            # the moment it acquires the lock, sees an alive process and skips.
            PID_FILE.write_text(str(proc.pid))
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


# ---------- daemon mode ----------

async def daemon_main() -> None:
    try:
        from aiohttp import web
    except ImportError:
        sys.stderr.write("ERROR: aiohttp not installed. Run: pip3 install aiohttp\n")
        sys.exit(1)

    PID_FILE.write_text(str(os.getpid()))
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
    )
    log = logging.getLogger("agent-dashboard")
    log.info("daemon starting pid=%d", os.getpid())

    migrate_legacy_token()  # fold any existing shared token into devices.json

    @web.middleware
    async def auth_middleware(request, handler):
        # /auth bootstraps the session — let it through to validate the token.
        if request.path == "/auth":
            return await handler(request)
        cookie = request.cookies.get(COOKIE_NAME, "")
        query  = request.query.get("token", "")
        device = find_device_by_token(cookie) or find_device_by_token(query)
        if device:
            ua = request.headers.get("User-Agent", "")
            asyncio.create_task(asyncio.to_thread(update_last_seen, device["id"], ua))
            return await handler(request)
        return web.Response(
            status=401,
            text="unauthorized — pair this device first (run `daemon.py --pair`)",
        )

    async def handle_auth(request):
        token = request.query.get("token", "")
        device = find_device_by_token(token)
        if not device:
            return web.Response(status=401, text="invalid or unknown token")
        resp = web.HTTPFound("/")
        resp.set_cookie(
            COOKIE_NAME, token,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="Lax",
            path="/",
        )
        asyncio.create_task(asyncio.to_thread(
            update_last_seen, device["id"], request.headers.get("User-Agent", "")
        ))
        return resp

    clients: set = set()
    index_path = Path(__file__).parent / "index.html"

    async def handle_index(_request):
        return web.FileResponse(index_path)

    async def handle_state_api(_request):
        return web.json_response(read_state())

    async def handle_ws(request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        clients.add(ws)
        log.info("ws client connected (%d total)", len(clients))
        try:
            await ws.send_json({"type": "state", "data": read_state()})
            async for _msg in ws:
                pass
        finally:
            clients.discard(ws)
            log.info("ws client disconnected (%d remain)", len(clients))
        return ws

    async def broadcast(payload: dict) -> None:
        dead = set()
        for c in clients:
            try:
                await c.send_json(payload)
            except Exception:
                dead.add(c)
        clients.difference_update(dead)

    async def watch_state_loop() -> None:
        last_mtime = 0.0
        last_change = time.time()
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                mtime = STATE_FILE.stat().st_mtime
            except FileNotFoundError:
                mtime = 0.0
            if mtime != last_mtime:
                last_mtime = mtime
                last_change = time.time()
                await broadcast({"type": "state", "data": read_state()})
            elif time.time() - last_change > IDLE_TIMEOUT_S:
                log.info("idle timeout, exiting")
                os._exit(0)

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/",          handle_index)
    app.router.add_get("/auth",      handle_auth)
    app.router.add_get("/api/state", handle_state_api)
    app.router.add_get("/ws",        handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log.info("listening http://%s:%d", HOST, PORT)

    async def cleanup_devices_loop() -> None:
        # Do an immediate pass on startup so legacy state gets cleaned up.
        while True:
            removed = await asyncio.to_thread(cleanup_devices)
            for d in removed:
                log.info("auto-revoked device id=%s name=%r reason=%s",
                         d.get("id"), d.get("name"), d.get("_reason"))
            await asyncio.sleep(CLEANUP_INTERVAL_S)

    def _refresh_transcript_meta_sync() -> bool:
        """Tail each session's transcript for Claude's abstracted ai-title and
        fold it into the state file. We intentionally ignore last-prompt for
        privacy: the literal user prompt would expose private input to anyone
        glancing at the ambient display."""
        snapshot = read_state()
        per_sid: dict[str, str] = {}
        for sid, sess in snapshot.get("sessions", {}).items():
            meta = read_transcript_meta(sess.get("transcript_path") or "")
            title = meta.get("title") if meta else None
            if title:
                per_sid[sid] = title
        if not per_sid:
            return False
        changed = False
        with state_lock():
            state = read_state()
            for sid, title in per_sid.items():
                sess = state.get("sessions", {}).get(sid)
                if sess and sess.get("title") != title:
                    sess["title"] = title
                    changed = True
            if changed:
                state["updated_at"] = _now_iso()
                write_state(state)
        return changed

    async def transcript_meta_loop() -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                await asyncio.to_thread(_refresh_transcript_meta_sync)
            except Exception:
                pass  # best-effort, never crash the daemon

    asyncio.create_task(watch_state_loop())
    asyncio.create_task(cleanup_devices_loop())
    asyncio.create_task(transcript_meta_loop())
    # idle forever; watch loop handles shutdown
    while True:
        await asyncio.sleep(3600)


# ---------- pair / list / revoke modes ----------

def pair_main(name: str | None = None) -> None:
    """Create a new device entry and print a QR + URL with its fresh token.
    Also writes a standalone HTML file with the QR so it can be viewed cleanly
    in a browser (auto-opens on macOS) — handy when the terminal mangles the
    Unicode-block ASCII rendering."""
    try:
        import qrcode
        from qrcode.image.svg import SvgImage
    except ImportError:
        sys.stderr.write("ERROR: qrcode not installed. Run: .venv/bin/pip install qrcode\n")
        sys.exit(1)

    migrate_legacy_token()
    device_id, token = create_device(name)
    base = get_pair_base_url()
    url  = f"{base}/auth?token={token}"

    # SVG QR (no Pillow needed). qrcode generates standalone-XML SVG with
    # namespace prefixes — incompatible with inline HTML embedding. Embed via
    # a base64 data URI inside <img>, which browsers parse as a separate doc.
    import base64, io
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgImage)
    buf = io.BytesIO()
    img.save(buf)
    svg_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    qr_data_uri = f"data:image/svg+xml;base64,{svg_b64}"

    html_path = Path(f"/tmp/agent-dashboard-pair-{device_id}.html")
    html_path.write_text(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>pair {device_id}</title>
<style>
  body {{ background:#fff; color:#222; font-family:-apple-system,system-ui,sans-serif;
         display:flex; flex-direction:column; align-items:center; padding:32px; gap:18px; }}
  .qr {{ width:min(80vw, 480px); height:min(80vw, 480px);
        background:#fff; padding:16px; border-radius:12px;
        box-shadow:0 2px 12px rgba(0,0,0,0.08);
        image-rendering: pixelated; }}
  code {{ background:#f3f3f3; padding:6px 10px; border-radius:4px; font-size:12px;
          word-break:break-all; max-width:90vw; line-height:1.4; }}
  h1 {{ font-size:15px; font-weight:500; color:#555; margin:0; text-align:center; }}
  p  {{ color:#888; font-size:12px; margin:0; text-align:center; }}
</style></head>
<body>
  <h1>pair this device — <b>{(name or f'device-{device_id[:4]}')}</b>  ·  id <code>{device_id}</code></h1>
  <img class="qr" src="{qr_data_uri}" alt="pairing QR">
  <code>{url}</code>
  <p>scan with your phone camera, or open the URL on the new device</p>
</body></html>""")

    # ASCII fallback (still print in case the user wants it inline)
    qr.print_ascii(tty=sys.stdout.isatty())

    print()
    print(f"  device id:  {device_id}")
    print(f"  name:       {name or f'device-{device_id[:4]}'}  (auto-updates from User-Agent on first connect)")
    print(f"  URL:        {url}")
    print(f"  loopback:   http://127.0.0.1:{PORT}/auth?token={token}")
    print(f"  QR page:    {html_path}")
    print()
    print(f"  This token is shown ONLY ONCE. Only its SHA-256 hash is stored.")
    print(f"  Revoke later with:  python3 daemon.py --revoke {device_id}")

    # Auto-open the QR page on macOS so the user gets a clean scannable image
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", str(html_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def list_main() -> None:
    """Print all paired devices, marking pending / idle status."""
    migrate_legacy_token()
    devices = load_devices().get("devices", [])
    if not devices:
        print("(no devices paired — run `daemon.py --pair` to add one)")
        return
    now = _now_dt()
    print(f"{'ID':10} {'STATUS':10} {'NAME':24} {'CREATED':20} {'LAST SEEN':20}  UA")
    print("-" * 120)
    for d in devices:
        created = (d.get("created_at") or "")[:19].replace("T", " ")
        last    = (d.get("last_seen_at") or "(never)")[:19].replace("T", " ")
        ua      = (d.get("user_agent") or "")[:46]
        pu = _parse_iso(d.get("pending_until"))
        if pu and not d.get("last_seen_at"):
            remaining = pu - now
            mins = max(0, int(remaining.total_seconds() / 60))
            status = f"pend {mins}m" if remaining.total_seconds() > 0 else "expired"
        else:
            last_active = _parse_iso(d.get("last_seen_at"))
            if last_active:
                age_h = (now - last_active).total_seconds() / 3600
                status = f"{age_h:.0f}h ago" if age_h >= 1 else "active"
            else:
                status = "—"
        print(f"{d['id']:10} {status:10} {d['name'][:24]:24} {created:20} {last:20}  {ua}")


def revoke_main(id_prefix: str) -> None:
    """Remove a device. Subsequent requests with its token will 401."""
    removed = revoke_device_by_prefix(id_prefix)
    if not removed:
        sys.stderr.write(f"no unique device matches id prefix '{id_prefix}'\n")
        sys.stderr.write("(use --list to see ids; try a longer prefix if ambiguous)\n")
        sys.exit(1)
    print(f"revoked: {removed['name']} (id {removed['id']})")


# ---------- entrypoint ----------

def _arg_after(flag: str) -> str | None:
    """Return the positional value after `flag` in sys.argv, or None."""
    args = sys.argv[1:]
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return None


if __name__ == "__main__":
    if "--hook" in sys.argv:
        hook_main()
    elif "--pair" in sys.argv:
        pair_main(_arg_after("--pair"))
    elif "--list" in sys.argv:
        list_main()
    elif "--revoke" in sys.argv:
        prefix = _arg_after("--revoke")
        if not prefix:
            sys.stderr.write("usage: daemon.py --revoke <id-prefix>\n")
            sys.exit(2)
        revoke_main(prefix)
    else:
        try:
            asyncio.run(daemon_main())
        except KeyboardInterrupt:
            pass
