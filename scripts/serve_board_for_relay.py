"""Keep the existing loopback board available to the outbound relay.

An already running board keeps ownership of its port and CSRF state. When
it stops, this supervisor starts the same server used by `claude-browse --web`.
"""

import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from claude_browse.web import run_server

if __name__ == "__main__":
    while True:
        try:
            with urlopen("http://127.0.0.1:51444/api/meta", timeout=5) as response:
                if not response.headers.get("Server", "").startswith("claude-browse-web/"):
                    raise SystemExit("Port 51444 belongs to a different application.")
            time.sleep(10)
        except (URLError, TimeoutError, ConnectionError):
            try:
                run_server(str(Path(__file__).resolve().parent.parent), limit=100000, port=51444)
            except OSError:
                time.sleep(5)  # Another board may have won the bind race.
