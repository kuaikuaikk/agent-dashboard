#!/usr/bin/env bash
# Thin shim called by Claude Code hooks. On first invocation, creates a
# Python venv and installs aiohttp + qrcode (one-time, sentinel-guarded).
# After that, just execs daemon.py --hook in the venv. Hooks must never
# fail hard — silently exit 0 on any bootstrap problem so Claude Code is
# never blocked by our setup.

set +e
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV="$SCRIPT_DIR/.venv"
SENTINEL="$VENV/.bootstrapped"
BOOT_LOG="/tmp/agent-dashboard-bootstrap.log"

if [ ! -e "$SENTINEL" ]; then
  {
    echo "[$(date)] bootstrapping venv at $VENV (first run)"
    if ! command -v python3 >/dev/null 2>&1; then
      echo "ERROR: python3 not found in PATH; install Python 3.11+ and retry"
      exit 0
    fi
    python3 -m venv "$VENV" \
      && "$VENV/bin/pip" install --quiet --disable-pip-version-check aiohttp qrcode \
      && touch "$SENTINEL" \
      && echo "[$(date)] bootstrap complete"
  } >>"$BOOT_LOG" 2>&1
fi

PYTHON="$VENV/bin/python"
if [ ! -x "$PYTHON" ]; then
  # Bootstrap failed (e.g., offline first run). Fall back to system python3
  # so subsequent hooks keep firing if user pip-installs the deps manually.
  PYTHON="$(command -v python3 || true)"
fi

if [ -z "$PYTHON" ]; then
  exit 0
fi

exec "$PYTHON" "$SCRIPT_DIR/daemon.py" --hook
