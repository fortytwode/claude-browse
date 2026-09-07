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


def test_main_focuses_only_a_valid_session_provider_pair(monkeypatch):
    calls = []
    monkeypatch.setattr(
        terminal_focus,
        "focus_session",
        lambda session_id, provider: calls.append((session_id, provider))
        or {"focused": True, "reason": ""},
    )

    assert terminal_focus.main(["session-id", "codex"]) == 0
    assert calls == [("session-id", "codex")]
    assert terminal_focus.main(["session-id", "unknown"]) == 2


def test_set_session_title_persists_and_reads_back_only_the_verified_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(
        terminal_focus.store, "get", lambda _session_id: {"terminal_title_managed": 1}
    )
    monkeypatch.setattr(
        terminal_focus.presence, "verified_terminal_tty", lambda *_args: ("ttys004", "")
    )
    monkeypatch.setattr(
        terminal_focus.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(
            returncode=0, stdout="updated\n"
        ),
    )

    result = terminal_focus.set_session_title(
        "session-id", "codex", "Plan\nrelease\x1b\u009b\u009c\u009d"
    )

    assert result == {"updated": True, "reason": ""}
    argv, kwargs = calls[0]
    assert argv[:3] == ["osascript", "-e", terminal_focus._TITLE_SCRIPT]
    assert argv[-3:] == ["--", "ttys004", "Plan release"]
    assert kwargs["timeout"] == 3


def test_set_session_title_is_a_safe_noop_without_terminal_proof(monkeypatch):
    monkeypatch.setattr(
        terminal_focus.store, "get", lambda _session_id: {"terminal_title_managed": 1}
    )
    monkeypatch.setattr(
        terminal_focus.presence,
        "verified_terminal_tty",
        lambda *_args: (None, "No verified terminal."),
    )
    monkeypatch.setattr(
        terminal_focus.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call Terminal")),
    )

    assert terminal_focus.set_session_title("session-id", "claude", "Renamed") == {
        "updated": False,
        "reason": "No verified terminal.",
    }


def test_set_session_title_discards_an_older_rename_after_verification(monkeypatch):
    calls = []
    monkeypatch.setattr(
        terminal_focus.presence, "verified_terminal_tty", lambda *_args: ("ttys004", "")
    )
    monkeypatch.setattr(
        terminal_focus.store,
        "get",
        lambda _session_id: {
            "name": "Newer title",
            "name_source": "manual",
            "terminal_title_managed": 1,
        },
    )
    monkeypatch.setattr(
        terminal_focus.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(argv) or SimpleNamespace(returncode=0, stdout="updated\n"),
    )

    result = terminal_focus.set_session_title("session-id", "codex", "Older title")

    assert result == {
        "updated": False,
        "reason": "A newer session title replaced this rename.",
    }
    assert calls[0][-1] == "Newer title"


def test_set_session_title_reports_a_disappearing_verified_tab(monkeypatch):
    monkeypatch.setattr(
        terminal_focus.store, "get", lambda _session_id: {"terminal_title_managed": 1}
    )
    monkeypatch.setattr(
        terminal_focus.presence, "verified_terminal_tty", lambda *_args: ("ttys004", "")
    )
    monkeypatch.setattr(
        terminal_focus.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="not-found\n"),
    )

    assert terminal_focus.set_session_title("session-id", "codex", "Renamed") == {
        "updated": False,
        "reason": "The verified Terminal tab disappeared before it could be retitled.",
    }


def test_set_session_title_warns_for_an_existing_unmanaged_codex_terminal(monkeypatch):
    monkeypatch.setattr(
        terminal_focus.store, "get", lambda _session_id: {"terminal_title_managed": 0}
    )
    monkeypatch.setattr(
        terminal_focus.presence,
        "verified_terminal_tty",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not inspect Terminal")),
    )

    assert terminal_focus.set_session_title("session-id", "codex", "Renamed") == {
        "updated": False,
        "reason": (
            "This terminal was opened before managed titles were enabled. "
            "Reopen it through Agent Board to use its saved title."
        ),
    }
