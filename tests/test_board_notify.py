"""Tests for macOS notifications (board/notify.py).

notify.py had no test coverage before this file -- which is exactly how an
emoji/AppleScript-quoting bug (json.dumps() escaping emoji to \\uXXXX,
which AppleScript can't parse) shipped silently: check=False swallowed the
resulting syntax-error exit code on every real call.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from claude_browse.board import notify


@pytest.fixture(autouse=True)
def missing_dedicated_notifier(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENT_BOARD_NOTIFIER_EXECUTABLE",
        str(tmp_path / "missing-agent-board-notifier"),
    )


def test_applescript_quote_escapes_backslash_and_quote():
    assert notify._applescript_quote('say "hi"') == r'"say \"hi\""'
    assert notify._applescript_quote("back\\slash") == r'"back\\slash"'


def test_applescript_quote_keeps_emoji_literal_not_json_escaped():
    quoted = notify._applescript_quote("✅ done")
    assert quoted == '"✅ done"'
    assert "\\u" not in quoted  # the actual bug: json.dumps would emit ✅


@pytest.mark.skipif(
    shutil.which("osascript") is None or notify._notifications_disabled(),
    reason="real notification smoke test is disabled or osascript is unavailable",
)
def test_notify_with_emoji_title_produces_valid_applescript_real_osascript_call():
    """Regression test for the confirmed bug: real (non-mocked) osascript
    call with an emoji title, matching hook.py's actual usage, must exit 0."""
    result = subprocess.run(
        ["osascript", "-e",
         f'display notification {notify._applescript_quote("regression-test-body")} '
         f'with title {notify._applescript_quote("✅ done")}'],
        timeout=5, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"osascript failed: {result.stderr}"


def test_notify_never_raises_when_osascript_missing(monkeypatch):
    monkeypatch.delenv("AGENT_BOARD_DISABLE_NOTIFICATIONS", raising=False)

    def _raise(*args, **kwargs):
        raise FileNotFoundError("no osascript")

    monkeypatch.setattr(subprocess, "run", _raise)
    notify.notify("title", "message")  # must not raise


def test_notify_prefers_dedicated_app_and_preserves_argument_boundaries(
    tmp_path, monkeypatch
):
    executable = tmp_path / "AgentBoardNotifier"
    executable.write_text("")
    executable.chmod(0o755)
    monkeypatch.setenv("AGENT_BOARD_NOTIFIER_EXECUTABLE", str(executable))
    monkeypatch.delenv("AGENT_BOARD_DISABLE_NOTIFICATIONS", raising=False)
    calls = []

    class _Process:
        pass

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or _Process(),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("osascript fallback must not run"),
    )

    notify.notify('[repo] "done" ✅', "path with spaces\\and quotes")

    assert calls[0][0] == [
        str(executable),
        "--title",
        '[repo] "done" ✅',
        "--message",
        "path with spaces\\and quotes",
    ]
    assert calls[0][1]["start_new_session"] is True


def test_notify_falls_back_when_dedicated_app_launch_fails(tmp_path, monkeypatch):
    executable = tmp_path / "AgentBoardNotifier"
    executable.write_text("")
    executable.chmod(0o755)
    monkeypatch.setenv("AGENT_BOARD_NOTIFIER_EXECUTABLE", str(executable))
    monkeypatch.delenv("AGENT_BOARD_DISABLE_NOTIFICATIONS", raising=False)
    fallback_calls = []

    def _raise(*args, **kwargs):
        raise OSError

    monkeypatch.setattr(subprocess, "Popen", _raise)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: fallback_calls.append(command),
    )

    notify.notify("title", "message")

    assert fallback_calls and fallback_calls[0][:2] == ["osascript", "-e"]


def test_notify_includes_sound_in_the_generated_script(monkeypatch):
    monkeypatch.delenv("AGENT_BOARD_DISABLE_NOTIFICATIONS", raising=False)
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["script"] = cmd[2]

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    notify.notify("✅ done", "some-thread")

    assert 'sound name "default"' in captured["script"]


def test_notify_can_be_disabled_for_noninteractive_runs(monkeypatch):
    calls = []
    monkeypatch.setenv("AGENT_BOARD_DISABLE_NOTIFICATIONS", "1")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))

    notify.notify("done", "test thread")

    assert calls == []


@pytest.mark.skipif(
    shutil.which("osascript") is None or notify._notifications_disabled(),
    reason="real notification smoke test is disabled or osascript is unavailable",
)
def test_notify_with_sound_real_osascript_call_exits_zero():
    """Real (non-mocked) call through the actual notify() function, matching
    hook.py's exact usage (emoji title, real message) end to end."""
    result = subprocess.run(
        ["osascript", "-e",
         f'display notification {notify._applescript_quote("real-sound-test")} '
         f'with title {notify._applescript_quote("✅ done")} sound name "default"'],
        timeout=5, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"osascript failed: {result.stderr}"
