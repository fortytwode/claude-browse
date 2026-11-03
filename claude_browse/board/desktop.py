"""Private local-server entry point owned by the macOS desktop companion."""

import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from claude_browse.web import run_server


def main():
    parent = os.getppid()
    # localStorage is origin-scoped; keep the same origin on every app launch.
    port_file = Path.home() / "Library/Application Support/Agent Board/desktop-port"
    port = int(port_file.read_text()) if port_file.exists() else 0
    if port != 0 and not 1024 <= port <= 65535:
        raise ValueError(f"Invalid saved desktop port in {port_file}")

    def ready(url):
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(str(urlparse(url).port))
        print(url, flush=True)

    def watch_parent():
        # Also clean up after a crash or force-quit of the native app.
        while os.getppid() == parent:
            time.sleep(2)
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()
    run_server(
        str(Path.home()),
        port=port,
        open_browser=False,
        on_ready=ready,
    )


if __name__ == "__main__":
    main()
