"""Initialize Railway's root-owned volume, then run the server without root."""

import os
from pathlib import Path

root = Path(os.environ.get("BRIDGE_DATA_DIR", "/data"))
root.mkdir(parents=True, exist_ok=True)
if os.getuid() == 0:
    os.chown(root, 10001, 10001)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
os.execv("/app/.venv/bin/playstore-mcp-bridge", ["playstore-mcp-bridge"])  # noqa: S606
