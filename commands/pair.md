---
description: Generate a new agent-dashboard pairing QR (auto-opens in Safari)
---

Run exactly this command, then briefly confirm to the user:

!`${CLAUDE_PLUGIN_ROOT}/.venv/bin/python ${CLAUDE_PLUGIN_ROOT}/daemon.py --pair`

After running:
- Report only the **device id** that was created (one short line).
- Tell the user Safari should have auto-opened with the QR.
- **Do NOT print the token** in your reply — it is one-time-use and the user can see it in the auto-opened Safari page.
- No other commentary.
