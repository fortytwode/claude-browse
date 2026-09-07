"""Native Terminal focus only follows an exact, provider-verified TTY."""

from __future__ import annotations

from types import SimpleNamespace

from claude_browse.board import terminal_focus


def test_focus_session_passes_verified_tty_as_an_argv_value(monkeypatch):
    calls = []
    monkeypatch.setattr(
        terminal_focus.presence, "verified_terminal_tty", lambda *_args: ("ttys004", "")
    )
    monkeypatch.setattr(
        terminal_focus.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(
            returncode=0, stdout="focused\n"
        ),
    )

    assert terminal_focus.focus_session("session-id", "codex") == {"focused": True, "reason": ""}
    argv, kwargs = calls[0]
    assert argv[:3] == ["osascript", "-e", terminal_focus._FOCUS_SCRIPT]
    assert argv[-2:] == ["--", "ttys004"]
    assert kwargs["timeout"] == 3


def test_focus_session_never_calls_applescript_without_exact_proof(monkeypatch):
    monkeypatch.setattr(
        terminal_focus.presence,
        "verified_terminal_tty",
        lambda *_args: (None, "No uniquely verified Codex terminal is open for this task."),
    )
    monkeypatch.setattr(
        terminal_focus.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not focus")),
    )

    assert terminal_focus.focus_session("session-id", "codex") == {
        "focused": False,
        "reason": "No uniquely verified Codex terminal is open for this task.",
    }


def test_focus_session_reports_disappearing_tab_without_opening_a_new_window(monkeypatch):
    monkeypatch.setattr(
        terminal_focus.presence, "verified_terminal_tty", lambda *_args: ("ttys004", "")
    )
    monkeypatch.setattr(
        terminal_focus.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="not-found\n"),
    )

    assert terminal_focus.focus_session("session-id", "claude") == {
        "focused": False,
        "reason": "The verified Terminal tab disappeared before it could be focused.",
    }
