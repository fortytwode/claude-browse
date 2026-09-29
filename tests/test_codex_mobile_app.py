"""codexmobile: transport choice, thread-holder handling, catch-up, picker rows.

The fake in fixtures/fake_codex_app_server.py speaks both transports: JSON
lines on stdio (`app-server`) and WebSocket frames (`app-server proxy`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from claude_browse import codex_mobile_app as app
from claude_browse import codex_mobile_json as legacy

ROOT = Path(__file__).resolve().parents[1]
FAKE = str(ROOT / "tests" / "fixtures" / "fake_codex_app_server.py")
THREAD = "aaaaaaaa-1111-2222-3333-444444444444"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run(args, tmp_path, scenario="ok", daemon=True, stdin="", seen=None, extra=None):
    home = tmp_path / "home"
    (home / ".cache" / "codexmobile").mkdir(parents=True, exist_ok=True)
    if seen is not None:
        (home / ".cache" / "codexmobile" / "seen.json").write_text(json.dumps(seen))
    env = dict(os.environ)
    env.pop("CODEX_MOBILE_PRIVATE", None)
    env.update(
        HOME=str(home),
        NO_COLOR="1",
        CODEX_REAL_BINARY=FAKE,
        FAKE_SCENARIO=scenario,
        FAKE_DAEMON="1" if daemon else "0",
        FAKE_DELAY="0.3",
    )
    env.update(extra or {})
    proc = subprocess.run(
        [sys.executable, str(ROOT / "codexmobile"), *args],
        input=stdin, capture_output=True, text=True, env=env, timeout=60,
    )
    return proc, home


def test_joins_the_shared_daemon_when_there_is_one(tmp_path):
    proc, _ = run(["--yolo", "start", "hello there"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "shared daemon" in proc.stdout
    assert "You asked: hello there. Done." in proc.stdout
    assert "\x1b" not in proc.stdout and "\r" not in proc.stdout


def test_private_server_when_no_daemon_and_never_legacy(tmp_path):
    proc, _ = run(["--yolo", "start", "hello"], tmp_path, daemon=False)
    assert proc.returncode == 0, proc.stderr
    assert "private server" in proc.stdout and "no shared Codex daemon" in proc.stdout
    assert "JSON mode" not in proc.stdout


def test_private_server_when_the_daemon_refuses_the_handshake(tmp_path):
    proc, _ = run(["--yolo", "start", "hello"], tmp_path, scenario="badinit")
    assert proc.returncode == 0, proc.stderr
    assert "private server" in proc.stdout and "daemon too old" in proc.stdout


def test_reconnects_when_the_daemon_is_restarting(tmp_path):
    counter = tmp_path / "count"
    proc, _ = run(["--yolo", "start", "hello"], tmp_path, extra={"FAKE_COUNTER": str(counter), "FAKE_DRAIN_CONNECTIONS": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "daemon is restarting; reconnecting" in proc.stdout
    assert "Done." in proc.stdout and "private server" not in proc.stdout


def test_active_writer_is_explained_and_never_falls_back_to_legacy(tmp_path):
    proc, _ = run(["--yolo", "resume", THREAD, "hello"], tmp_path, scenario="writer")
    text = " ".join(proc.stdout.split())
    assert "This thread is open in" in text and "Only one of them can write to it at a time" in text
    assert "codexmobile fork <id>" in text
    assert "JSON mode" not in text and "app-server unavailable" not in text
    assert "You asked" not in text
    assert proc.returncode == 1


def test_catches_up_on_a_turn_finished_while_away(tmp_path):
    seen = {THREAD: {"turn": "turn-0001", "items": ["u1", "a1"], "at": 1}}
    proc, home = run(["--yolo", "resume", THREAD], tmp_path, scenario="missed", seen=seen)
    out = proc.stdout
    assert "catching up on what you missed" in out
    assert "> Run the long build" in out and "$ sleep 10; echo finished-marker" in out
    assert "• the build passed" in out and "✳ done in 16.0s" in out
    assert "first answer" not in out
    state = json.loads((home / ".cache" / "codexmobile" / "seen.json").read_text())
    assert state[THREAD]["turn"] == "turn-0002"
    again, _ = run(["--yolo", "resume", THREAD], tmp_path, scenario="missed")
    assert "catching up" not in again.stdout and "finished-marker" not in again.stdout


def test_attaches_to_a_turn_still_running(tmp_path):
    seen = {THREAD: {"turn": "turn-0001", "items": ["u1", "a1", "u2", "a2"], "at": 1}}
    proc, _ = run(["--yolo", "resume", THREAD], tmp_path, scenario="inflight", seen=seen)
    out = proc.stdout
    assert "still running; following it live" in out
    assert out.count("$ sleep 10; echo finished-marker") == 1 and "exit 0 · 1 line" in out
    assert out.count("printed the marker") == 1
    assert "LEAK" not in out and "I’ll run the command now." not in out
    assert "✳ done in" in out


@pytest.mark.parametrize("width", [40, 58, 59, 80, 100])
def test_picker_rows_never_exceed_the_width(width):
    title = "Okay. The playback failure, yes, we should fix. Are we converging on a plan for it"
    for index, meta in ((1, "just now · 01a0e999-9c9d"), (10, "26m ago · 01a0e756-16b1")):
        lines = legacy._picker_lines(index, title, meta, width)
        assert 1 <= len(lines) <= 2
        assert all(len(ANSI.sub("", line)) <= width for line in lines)
        assert "…" in lines[0]
        assert meta in ANSI.sub("", lines[-1])
        if len(lines) == 2:
            assert lines[1].startswith("    ")


def test_a_session_with_a_living_shell_is_not_stale():
    assert app._terminal_lost({"tty": "??", "ppid": 4242})
    assert app._terminal_lost({"tty": "ttys000", "ppid": 1})
    assert app._terminal_lost({"tty": "ttys-gone-999", "ppid": 4242})
    tty = os.path.basename(os.ttyname(0)) if os.isatty(0) else None
    if tty:
        assert not app._terminal_lost({"tty": tty, "ppid": 4242})


@pytest.mark.parametrize(
    "command, ours",
    [
        ("/opt/homebrew/Cellar/python@3.14/3.14.6/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python /Users/s/.local/bin/codexmobile --yolo", True),
        ("python3 /Users/s/repos/claude-browse/codexmobile resume abc", True),
        ("/usr/bin/python3 -m claude_browse.codex_mobile_app --yolo", True),
        ("python3 -u /Users/s/.local/bin/codex-mobile-json", True),
        ("zsh -c python3 -m pytest tests/test_codex_mobile_app.py", False),
        ("codex --dangerously-bypass-approvals-and-sandbox fix codexmobile please", False),
        ("claude --resume 1234 look at codex_mobile_app.py", False),
        ("python3 t_real.py codexmobile", False),
        ("/Users/s/.codex/packages/app-server-daemon/releases/0.159.0/bin/codex app-server --listen unix:// --managed-daemon", False),
    ],
)
def test_only_the_client_itself_counts_as_ours(command, ours):
    assert app._is_ours(command) is ours


def test_release_refuses_when_any_holder_is_live_or_foreign():
    me = os.getpid()
    assert app._release([{"kind": "live", "pid": me, "owner": me}]) is False
    assert app._release([{"kind": "foreign", "pid": me}]) is False
    assert app._release([]) is False


def test_terminal_errors_are_recognised():
    assert app._terminal_error(app.TerminalGone("x"))
    assert app._terminal_error(BrokenPipeError())
    assert app._terminal_error(OSError(5, "Input/output error"))
    assert not app._terminal_error(OSError(2, "No such file"))
    assert not app._terminal_error(app.AppServerError("x"))
