#!/usr/bin/env bash
# Claude Code hook entry point for agent-dashboard.
# Forwards the stdin event payload to daemon.py --hook,
# using the project-local venv so aiohttp is available.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"   # fall back to system python (daemon spawn may fail without aiohttp)
fi
exec "$PYTHON" "$SCRIPT_DIR/daemon.py" --hook
