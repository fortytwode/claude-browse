"""Install the two per-user background services for the Mission Control board."""

import importlib.util
import os
import plistlib
import subprocess
import sys
from pathlib import Path


def install() -> None:
    if importlib.util.find_spec("websockets") is None:
        raise SystemExit('Install the relay dependency first: python -m pip install ".[board-relay]"')
    repo = Path(__file__).resolve().parent.parent
    directory = Path.home() / "Library/LaunchAgents"
    state = Path.home() / ".claude/agent-board"
    token = state / "mission-control-relay.token"
    if not token.is_file():
        raise SystemExit("Create the private relay credential first.")
    services = {
        "com.rocketshiphq.agent-board-backend": [
            sys.executable,
            str(repo / "scripts/serve_board_for_relay.py"),
        ],
        "com.rocketshiphq.agent-board-relay": [
            sys.executable,
            "-m",
            "claude_browse.board.relay",
            "--machine-id",
            "shamanth-macbook-pro",
            "--token-file",
            str(token),
            "--port",
            "51444",
        ],
    }
    directory.mkdir(parents=True, exist_ok=True)
    for label, arguments in services.items():
        target = directory / f"{label}.plist"
        config = {
            "Label": label,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(repo),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "EnvironmentVariables": {
                "PYTHONPATH": str(repo),
                "PYTHONUNBUFFERED": "1",
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "BROWSER": "/usr/bin/true",
            },
            "StandardOutPath": str(state / f"{label}.log"),
            "StandardErrorPath": str(state / f"{label}.log"),
        }
        data = plistlib.dumps(config)
        if target.exists() and target.read_bytes() != data:
            raise SystemExit(f"Existing service differs: {target}; inspect before replacing it.")
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)
        domain = f"gui/{os.getuid()}"
        loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True)
        if loaded.returncode:
            subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
        print(f"Installed {label}")


if __name__ == "__main__":
    install()
