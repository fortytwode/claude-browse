"""macOS native notifications. Best-effort -- never raises to the caller."""

from __future__ import annotations

import json
import subprocess


def notify(title: str, message: str) -> None:
    try:
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            capture_output=True,
            check=False,
        )
    except Exception:
        pass
