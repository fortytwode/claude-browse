"""Read-only local terminal presence checks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_browse.board import presence


def _row(session_id: str, provider: str = "claude", **extra):
    return {
        "session_id": session_id,
        "provider": provider,
        "host": "this-mac",
        "state": "ended",
        **extra,
    }


def _result(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _completed(stdout: str) -> SimpleNamespace:
    return _result(0, stdout)


@pytest.fixture(autouse=True)
def _isolated_presence(monkeypatch):
    presence._clear_cache()
    monkeypatch.setattr(presence, "_hostname", lambda: "this-mac")


def test_claude_exact_live_process_overrides_stale_runtime(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(json.dumps({
        "pid": 42, "sessionId": "claude-live", "procStart": "Fri Sep  5 01:02:03 2026",
    }))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)
    monkeypatch.setattr(
        presence,
        "_run",
        lambda args, timeout: _completed("42 ttys001 claude Fri Sep  5 01:02:03 2026\n"),
    )

    assert presence.snapshot([_row("claude-live")]) == {"claude-live": "open"}


def test_verified_terminal_tty_requires_the_exact_claude_process(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(json.dumps({
        "pid": 42, "sessionId": "claude-live", "procStart": "Fri Sep  5 01:02:03 2026",
    }))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)
    monkeypatch.setattr(
        presence,
        "_run",
        lambda _args, _timeout: _completed("42 ttys004 claude Fri Sep  5 01:02:03 2026\n"),
    )

    assert presence.verified_terminal_tty("claude-live", "claude") == ("ttys004", "")


def test_verified_terminal_tty_refuses_ambiguous_or_nonterminal_claude(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(json.dumps({
        "pid": 42, "sessionId": "claude-live", "procStart": "Fri Sep  5 01:02:03 2026",
    }))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)
    monkeypatch.setattr(
        presence,
        "_run",
        lambda _args, _timeout: _completed("42 ?? claude Fri Sep  5 01:02:03 2026\n"),
    )

    tty, reason = presence.verified_terminal_tty("claude-live", "claude")
    assert tty is None
    assert "could not be verified" in reason


@pytest.mark.parametrize(
    "ps_line",
    [
        "42 ?? claude Fri Sep  5 01:02:03 2026\n",
        "42 ttys001 python Fri Sep  5 01:02:03 2026\n",
        "42 ttys001 claude Fri Sep  6 01:02:03 2026\n",
    ],
)
def test_claude_non_terminal_wrong_command_or_pid_reuse_is_unknown(
    tmp_path, monkeypatch, ps_line
):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(json.dumps({
        "pid": 42, "sessionId": "claude-live", "procStart": "Fri Sep  5 01:02:03 2026",
    }))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)
    monkeypatch.setattr(presence, "_run", lambda args, timeout: _completed(ps_line))

    assert presence.snapshot([_row("claude-live")]) == {"claude-live": "unknown"}


def test_claude_exited_pid_with_macos_ps_shape_does_not_hide_live_sessions(
    tmp_path, monkeypatch
):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "41.json").write_text(json.dumps({
        "pid": 41, "sessionId": "claude-exited",
    }))
    (sessions / "42.json").write_text(json.dumps({
        "pid": 42, "sessionId": "claude-live",
        "procStart": "Fri Sep  5 01:02:03 2026",
    }))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)

    def run(args, timeout):
        if args[2] == "41":
            # macOS ps returns this exact shape for an exited selected PID.
            return _result(1)
        return _completed("42 ttys001 claude Fri Sep  5 01:02:03 2026\n")

    monkeypatch.setattr(presence, "_run", run)

    assert presence.snapshot([
        _row("claude-exited"), _row("claude-live"),
    ]) == {"claude-exited": "closed", "claude-live": "open"}
    assert presence.live_sessions() == [{
        "session_id": "claude-live", "provider": "claude", "cwd": "",
    }]


def test_claude_nonempty_ps_failure_remains_unknown(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(json.dumps({
        "pid": 42, "sessionId": "claude-live",
    }))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)
    monkeypatch.setattr(
        presence, "_run", lambda args, timeout: _result(1, stderr="ps failed")
    )

    assert presence.snapshot([_row("claude-live")]) == {"claude-live": "unknown"}


def test_successfully_unmatched_local_is_closed_but_foreign_and_hostless_are_unknown(
    tmp_path, monkeypatch
):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)

    assert presence.snapshot([
        _row("local"),
        _row("remote", host="other-mac"),
        _row("hostless", host=None),
    ]) == {"local": "closed", "remote": "unknown", "hostless": "unknown"}


def _rollout(root: Path, sid: str, *, source: str = "user") -> Path:
    path = root / "2026" / "09" / "05" / f"rollout-2026-09-05T01-02-03-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "thread_source": source}}) + "\n")
    return path


def test_codex_only_writable_canonical_matching_descriptor_opens(tmp_path, monkeypatch):
    root = tmp_path / "codex" / "sessions"
    writable = _rollout(root, "a-b-c-d-e")
    readonly = _rollout(root, "parent-a-b-c-d", source="user")
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: root)

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n")
        return _completed(
            f"p9\nf20\nar\nn{readonly}\nf21\nau\nn{writable}\n"
        )

    monkeypatch.setattr(presence, "_run", run)
    assert presence.snapshot([_row("a-b-c-d-e", "codex")]) == {"a-b-c-d-e": "open"}


def test_verified_terminal_tty_returns_only_one_exact_codex_process(tmp_path, monkeypatch):
    root = tmp_path / "codex" / "sessions"
    writable = _rollout(root, "a-b-c-d-e")
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: root)

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n")
        assert args[-1] == "9"
        return _completed(f"p9\nf21\nau\nn{writable}\n")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.verified_terminal_tty("a-b-c-d-e", "codex") == ("ttys001", "")


def test_verified_terminal_tty_refuses_multiple_codex_processes(tmp_path, monkeypatch):
    root = tmp_path / "codex" / "sessions"
    writable = _rollout(root, "a-b-c-d-e")
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: root)

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n10 ttys002 codex\n")
        return _completed(f"p{args[-1]}\nf21\nau\nn{writable}\n")

    monkeypatch.setattr(presence, "_run", run)
    tty, reason = presence.verified_terminal_tty("a-b-c-d-e", "codex")
    assert tty is None
    assert "uniquely verified" in reason


@pytest.mark.parametrize("access,source,expected", [("r", "user", "unknown"), ("u", "subagent", "unknown")])
def test_codex_readonly_parent_and_subagent_never_open(tmp_path, monkeypatch, access, source, expected):
    root = tmp_path / "codex" / "sessions"
    descriptor = _rollout(root, "a-b-c-d-e", source=source)
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: root)

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n")
        return _completed(f"p9\nf20\na{access}\nn{descriptor}\n")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.snapshot([_row("a-b-c-d-e", "codex")]) == {"a-b-c-d-e": expected}


def test_codex_identity_mismatch_and_noncanonical_path_are_unknown(tmp_path, monkeypatch):
    root = tmp_path / "codex" / "sessions"
    mismatch = _rollout(root, "a-b-c-d-e")
    mismatch.write_text(json.dumps({"type": "session_meta", "payload": {"id": "wrong", "thread_source": "user"}}) + "\n")
    outside = tmp_path / "other" / mismatch.name
    outside.parent.mkdir()
    outside.write_text(mismatch.read_text())
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: root)

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n")
        return _completed(f"p9\nf20\nau\nn{mismatch}\nf21\nau\nn{outside}\n")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.snapshot([_row("a-b-c-d-e", "codex")]) == {"a-b-c-d-e": "unknown"}


def test_codex_duplicate_writable_descriptors_are_ambiguous(tmp_path, monkeypatch):
    root = tmp_path / "codex" / "sessions"
    descriptor = _rollout(root, "a-b-c-d-e")
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: root)

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n")
        return _completed(f"p9\nf20\nau\nn{descriptor}\nf21\nau\nn{descriptor}\n")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.snapshot([_row("a-b-c-d-e", "codex")]) == {"a-b-c-d-e": "unknown"}


def test_process_failure_and_timeout_do_not_become_empty_scan(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(json.dumps({"pid": 42, "sessionId": "local"}))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)
    monkeypatch.setattr(presence, "_run", lambda args, timeout: (_ for _ in ()).throw(TimeoutError()))

    assert presence.snapshot([_row("local")]) == {"local": "unknown"}


def test_provider_guard_requires_the_matching_native_scanner(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(json.dumps({"pid": 42, "sessionId": "same-id"}))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: tmp_path / "codex")
    monkeypatch.setattr(
        presence,
        "_run",
        lambda args, timeout: _completed("42 ttys001 claude Fri Sep  5 01:02:03 2026\n"),
    )

    assert presence.snapshot([_row("same-id", "codex")]) == {"same-id": "closed"}


def test_cache_is_per_provider_root_and_does_not_cache_row_filtering(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "42.json").write_text(json.dumps({"pid": 42, "sessionId": "one"}))
    calls = 0
    roots = [sessions]
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: roots[0])

    def run(args, timeout):
        nonlocal calls
        calls += 1
        return _completed("")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.snapshot([_row("one")]) == {"one": "closed"}
    assert presence.snapshot([_row("two")]) == {"two": "closed"}
    assert calls == 1
    other_root = tmp_path / "other-sessions"
    other_root.mkdir()
    (other_root / "42.json").write_text(json.dumps({"pid": 42, "sessionId": "one"}))
    roots[0] = other_root
    assert presence.snapshot([_row("one")]) == {"one": "closed"}
    assert calls == 2


def test_snapshot_uses_one_deadline_without_discarding_cached_provider_scan(
    tmp_path, monkeypatch
):
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex"
    clock = [0.0]
    claude_calls = 0
    codex_calls = 0
    monkeypatch.setattr(presence.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: claude_root)
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: codex_root)

    def scan_claude(root, deadline):
        nonlocal claude_calls
        claude_calls += 1
        assert deadline == presence.TOTAL_SCAN_TIMEOUT_S
        clock[0] = deadline
        return presence._Scan(True, frozenset({"claude-live"}))

    def scan_codex(root, deadline):
        nonlocal codex_calls
        codex_calls += 1
        return presence._Scan(True, frozenset({"codex-live"}))

    monkeypatch.setattr(presence, "_scan_claude", scan_claude)
    monkeypatch.setattr(presence, "_scan_codex", scan_codex)

    # A prior cached result remains usable even if the aggregate time is
    # exhausted while scanning the other provider.
    assert presence._scan_cached(
        "codex", codex_root, presence.TOTAL_SCAN_TIMEOUT_S
    ) == presence._Scan(
        True, frozenset({"codex-live"})
    )
    assert presence.snapshot([
        _row("claude-live"), _row("codex-live", "codex"),
    ]) == {"claude-live": "open", "codex-live": "open"}
    assert (claude_calls, codex_calls) == (1, 1)

    # An unscanned provider is Unknown for this request, but that temporary
    # result must not enter the cache and hide a later successful scan.
    presence._clear_cache()
    clock[0] = 0.0
    assert presence.snapshot([
        _row("claude-live"), _row("codex-live", "codex"),
    ]) == {"claude-live": "open", "codex-live": "unknown"}
    assert codex_calls == 1
    assert presence.snapshot([
        _row("claude-live"), _row("codex-live", "codex"),
    ]) == {"claude-live": "open", "codex-live": "open"}
    assert codex_calls == 2


def test_live_sessions_uses_one_deadline_across_providers(tmp_path, monkeypatch):
    clock = [0.0]
    codex_calls = 0
    monkeypatch.setattr(presence.time, "monotonic", lambda: clock[0])

    def scan_claude(root, deadline):
        clock[0] = deadline
        return presence._Scan(
            True,
            live=(presence._LiveSession("claude-live", "claude", "/claude"),),
        )

    def scan_codex(root, deadline):
        nonlocal codex_calls
        codex_calls += 1
        return presence._Scan(
            True,
            live=(presence._LiveSession("codex-live", "codex", "/codex"),),
        )

    monkeypatch.setattr(presence, "_scan_claude", scan_claude)
    monkeypatch.setattr(presence, "_scan_codex", scan_codex)

    assert presence.live_sessions() == [{
        "session_id": "claude-live", "provider": "claude", "cwd": "/claude",
    }]
    assert codex_calls == 0
