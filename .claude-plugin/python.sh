#!/usr/bin/env bash
# Shared bootstrap + dispatch. Ensures the project-local venv exists,
# installs aiohttp + qrcode on first run, then execs the venv python
# with whatever args were passed. Both hook.sh and the /pair slash
# command go through here so /pair works even before any hook has
# fired (and vice versa).

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
      exit 1
    fi
    python3 -m venv "$VENV" \
      && "$VENV/bin/pip" install --quiet --disable-pip-version-check aiohttp qrcode \
      && touch "$SENTINEL" \
      && echo "[$(date)] bootstrap complete"
  } >>"$BOOT_LOG" 2>&1
fi

PYTHON="$VENV/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
fi

if [ -z "$PYTHON" ]; then
  echo "ERROR: no python3 available — see $BOOT_LOG" >&2
  exit 1
fi

exec "$PYTHON" "$@"
