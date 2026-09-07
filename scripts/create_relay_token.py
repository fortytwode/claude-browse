"""Create a private, non-overwriting relay credential; never print its value."""

import os
import secrets
from pathlib import Path

path = Path.home() / ".claude/agent-board/mission-control-relay.token"
path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise SystemExit("Existing relay credential must be a private regular file.")
    print("Using existing private relay credential.")
else:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(secrets.token_urlsafe(48))
    print("Created private relay credential.")
