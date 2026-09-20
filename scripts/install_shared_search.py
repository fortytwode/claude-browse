#!/usr/bin/env python3
"""Install the resumable shared-search publisher for this macOS user."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.rocketshiphq.claude-browse-shared-search"


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("The scheduled publisher currently uses macOS launchd")
    repo = Path(__file__).resolve().parent.parent
    python = repo / ".venv" / "bin" / "python"
    if not python.exists():
        raise SystemExit("Install the board-sync extra into .venv first")
    log_dir = Path.home() / ".claude" / "shared-search"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "claude_browse.shared_search_cli"],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "StartInterval": 600,
        "EnvironmentVariables": {"CLAUDE_BROWSE_EMBEDDING_WINDOWS_PER_RUN": "4096"},
        "StandardOutPath": str(log_dir / "publisher.log"),
        "StandardErrorPath": str(log_dir / "publisher.err.log"),
    }
    plist.write_bytes(plistlib.dumps(config))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    backend = plist.parent / "com.rocketshiphq.agent-board-backend.plist"
    if backend.exists():
        backend_config = plistlib.loads(backend.read_bytes())
        backend_config.setdefault("EnvironmentVariables", {})[
            "CLAUDE_BROWSE_SHARED_SEARCH_ENABLED"
        ] = "1"
        backend.write_bytes(plistlib.dumps(backend_config))
        subprocess.run(["launchctl", "bootout", domain, str(backend)],
                       check=False, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(backend)], check=True)
    print(f"Installed {LABEL}; runs now and every 10 minutes")


if __name__ == "__main__":
    main()
