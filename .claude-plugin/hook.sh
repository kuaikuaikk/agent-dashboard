#!/usr/bin/env bash
# Thin shim called by Claude Code hooks. Delegates venv bootstrap +
# python dispatch to python.sh (shared with the /pair slash command),
# then runs daemon.py --hook. Always exit 0 — hooks must never block
# Claude Code, even if bootstrap fails.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
"$SCRIPT_DIR/python.sh" "$SCRIPT_DIR/daemon.py" --hook 2>/dev/null
exit 0
