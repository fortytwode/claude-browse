"""Compatibility launcher for native Codex with its own scrollable transcript.

The old ``codexmobile`` command remains usable by existing SSH shells, but
rendering, editing, approvals, and thread persistence belong to Codex itself.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # The previous launcher accepted `start [prompt]`; native Codex takes
    # the prompt directly. Preserve callers using its old permission flag.
    index = 1 if args[:1] == ["--yolo"] else 0
    if args[index : index + 1] == ["start"]:
        del args[index]
    if "--legacy" in args:
        print("Use codex-mobile-json for the legacy transcript runner.", file=sys.stderr)
        return 2
    binary = os.environ.get("CODEX_REAL_BINARY") or "codex"
    # Keep Codex-owned scrollable history, but use its compact presentation:
    # raw mode expands technical tool payloads as well as assistant answers.
    os.execvp(binary, [
        binary,
        "-c", 'tui.alternate_screen="always"',
        "-c", "tui.fullscreen_transcript=true",
        "-c", "tui.raw_output_mode=false",
        *args,
    ])
    return 0
