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


@pytest.mark.parametrize("bad_record", ["{", "[]", '{"sessionId": "unverified"}', None])
def test_claude_bad_record_before_valid_preserves_live_enrollment(
    tmp_path, monkeypatch, bad_record
):
    bad = tmp_path / "41.json"
    good = tmp_path / "42.json"
    if bad_record is not None:
        bad.write_text(bad_record)
    good.write_text(json.dumps({"pid": 42, "sessionId": "claude-live", "cwd": "/work"}))
    # Explicit order; the missing file models a record disappearing before open.
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([bad, good]))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: tmp_path)
    monkeypatch.setattr(presence, "_scan_codex", lambda root, deadline: presence._Scan(True))
    monkeypatch.setattr(
        presence, "_run",
        lambda args, timeout: _completed("42 ttys001 claude Fri Sep  5 01:02:03 2026\n"),
    )

    scan = presence._scan_cached("claude", tmp_path, presence.time.monotonic() + 10)
    assert not scan.complete
    assert scan.open_ids == frozenset({"claude-live"})
    assert scan.live == (presence._LiveSession("claude-live", "claude", "/work"),)
    assert presence.live_sessions() == [{
        "session_id": "claude-live", "provider": "claude", "cwd": "/work",
    }]
    assert presence.snapshot([_row("claude-live"), _row("unverified")]) == {
        "claude-live": "open", "unverified": "unknown",
    }


@pytest.mark.parametrize("failure", ["malformed", "ps", "deadline", "conflict", "invalid-pid"])
def test_claude_partial_scan_retains_only_verified_live_sessions(
    tmp_path, monkeypatch, failure
):
    good = tmp_path / "42.json"
    bad = tmp_path / "43.json"
    good.write_text(json.dumps({"pid": 42, "sessionId": "claude-live"}))
    conflicting = failure in {"conflict", "invalid-pid"}
    bad_payload = {
        "pid": 44 if conflicting else 43,
        "sessionId": "claude-live" if conflicting else "unverified",
    }
    if failure == "invalid-pid":
        bad_payload.pop("pid")
    bad.write_text("{" if failure == "malformed" else json.dumps(bad_payload))
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([good, bad]))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: tmp_path)
    monkeypatch.setattr(presence, "_scan_codex", lambda root, deadline: presence._Scan(True))
    clock = [0.0]
    monkeypatch.setattr(presence.time, "monotonic", lambda: clock[0])

    def run(args, timeout):
        if args[2] == "43":
            raise OSError("ps unavailable")
        if failure == "deadline":
            clock[0] = 10.0
        return _completed("42 ttys001 claude Fri Sep  5 01:02:03 2026\n")

    monkeypatch.setattr(presence, "_run", run)
    scan = presence._scan_cached("claude", tmp_path, 10.0)
    if conflicting:
        assert scan.open_ids == frozenset()
        assert scan.uncertain_ids == frozenset({"claude-live"})
        assert scan.live == ()
        assert presence.live_sessions() == []
    else:
        assert not scan.complete
        assert scan.open_ids == frozenset({"claude-live"})
        assert scan.live == (presence._LiveSession("claude-live", "claude", ""),)
        assert presence.live_sessions() == [{
            "session_id": "claude-live", "provider": "claude", "cwd": "",
        }]
    expected = "closed" if scan.complete else "unknown"
    assert presence.snapshot([_row("claude-live"), _row("unverified")]) == {
        "claude-live": "unknown" if conflicting else "open",
        "unverified": expected,
    }


@pytest.mark.parametrize("complete", [False, True])
def test_snapshot_uncertainty_overrides_open_evidence(monkeypatch, complete):
    monkeypatch.setattr(presence, "_scan_cached", lambda *args: presence._Scan(
        complete, frozenset({"verified", "conflicting"}), frozenset({"conflicting"}),
    ))
    assert presence.snapshot([
        _row("verified"), _row("conflicting"), _row("unmatched"),
    ]) == {
        "verified": "open", "conflicting": "unknown",
        "unmatched": "closed" if complete else "unknown",
    }


def test_discovery_from_partial_claude_scan_reports_enrolled_session_open(tmp_path, monkeypatch):
    from claude_browse.board import discovery, store, work_items

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    bad = sessions / "41.json"
    good = sessions / "42.json"
    bad.write_text("{")
    good.write_text(json.dumps({"pid": 42, "sessionId": "claude-live", "cwd": str(tmp_path)}))
    monkeypatch.setattr(Path, "iterdir", lambda self: iter([bad, good]))
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: sessions)
    monkeypatch.setattr(presence, "_scan_codex", lambda root, deadline: presence._Scan(True))
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    monkeypatch.setattr(
        presence, "_run",
        lambda args, timeout: _completed("42 ttys001 claude Fri Sep  5 01:02:03 2026\n"),
    )

    assert discovery.capture_live_sessions() == 1
    runtime = store.get("claude-live")
    assert runtime["host"] == "this-mac"
    assert work_items.get_for_session("claude-live")["status"] == "active"
    assert presence.snapshot([runtime, _row("unverified")]) == {
        "claude-live": "open", "unverified": "unknown",
    }
    assert discovery.capture_live_sessions() == 0


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


@pytest.mark.parametrize("duplicate,expected", [(False, "open"), (True, "unknown")])
def test_codex_partial_scan_preserves_verified_and_ambiguous_identities(
    tmp_path, monkeypatch, duplicate, expected
):
    root = tmp_path / "codex" / "sessions"
    descriptor = _rollout(root, "a-b-c-d-e")
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: root)

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n10 ttys002 codex\n")
        if args[-1] == "10":
            raise OSError("lsof unavailable")
        output = f"p9\nf20\nau\nn{descriptor}\n"
        if duplicate:
            output += f"f21\nau\nn{descriptor}\n"
        return _completed(output)

    monkeypatch.setattr(presence, "_run", run)
    scan = presence._scan_cached("codex", root, presence.time.monotonic() + 10)
    assert not scan.complete
    if duplicate:
        assert "a-b-c-d-e" in scan.uncertain_ids
    assert presence.snapshot([
        _row("a-b-c-d-e", "codex"), _row("unverified", "codex"),
    ]) == {"a-b-c-d-e": expected, "unverified": "unknown"}


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


def test_codex_partial_scan_retains_enrollment_and_verified_tty(tmp_path, monkeypatch):
    root = tmp_path / "codex" / "sessions"
    descriptor = _rollout(root, "a-b-c-d-e")
    descriptor.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "a-b-c-d-e", "thread_source": "user", "source": "cli",
        "originator": "codex-tui", "cwd": "/work",
    }}) + "\n")

    def run(args, timeout):
        if args[0] == "ps":
            return _completed("9 ttys001 codex\n10 ttys002 codex\n")
        if args[-1] == "10":
            raise OSError("lsof unavailable")
        return _completed(f"p9\nf20\nau\nn{descriptor}\n")

    monkeypatch.setattr(presence, "_run", run)
    scan = presence._scan_codex(root, presence.time.monotonic() + 10)
    assert not scan.complete
    assert scan.live == (presence._LiveSession("a-b-c-d-e", "codex", "/work", str(descriptor)),)
    assert scan.verified_ttys == frozenset({"ttys001"})


def test_unidentified_terminals_enumerates_processes_without_guessing_threads(tmp_path, monkeypatch):
    started = "Fri Sep  5 01:02:03 2026"
    calls = []
    monkeypatch.setattr(presence, "_scan_cached", lambda provider, *args: presence._Scan(
        True, verified_ttys=frozenset({"ttys005"}) if provider == "codex" else frozenset(),
    ))

    def run(args, timeout):
        calls.append(args)
        if args[0] == "ps":
            return _completed(
                f"33137 ttys005 codex {started}\n17973 ttys006 codex {started}\n"
                f"65284 ttys011 codex {started}\n11120 ttys020 claude {started}\n"
                f"123 ?? codex {started}\n124 ttys007 python {started}\n"
            )
        cwd = "/repos/YogaNidra" if args[-1] in {"17973", "65284"} else "/repos/team-operations"
        return _completed(f"p{args[-1]}\nfcwd\na \nn{cwd}\nf20\nar\nn/ignore-me\n")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.unidentified_terminals() == [
        {"pid": 17973, "tty": "ttys006", "provider": "codex", "cwd": "/repos/YogaNidra", "started": started},
        {"pid": 65284, "tty": "ttys011", "provider": "codex", "cwd": "/repos/YogaNidra", "started": started},
        {"pid": 11120, "tty": "ttys020", "provider": "claude", "cwd": "/repos/team-operations", "started": started},
    ]
    assert not any("args=" in part for args in calls for part in args)


@pytest.mark.parametrize("failure", ["error", "deadline"])
def test_unidentified_terminals_keeps_process_when_cwd_is_unavailable(monkeypatch, failure):
    clock = [0.0]
    monkeypatch.setattr(presence.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(presence, "_scan_cached", lambda *args: presence._Scan(False))

    def run(args, timeout):
        if args[0] == "ps":
            if failure == "deadline":
                clock[0] = presence.TOTAL_SCAN_TIMEOUT_S
            return _completed("9 ttys001 codex Fri Sep  5 01:02:03 2026\n")
        raise OSError("lsof unavailable")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.unidentified_terminals() == [{
        "pid": 9, "tty": "ttys001", "provider": "codex", "cwd": "",
        "started": "Fri Sep  5 01:02:03 2026",
    }]


def test_unidentified_terminals_cache_expires_and_cannot_be_mutated_by_caller(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr(presence.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(presence, "_scan_cached", lambda *args: presence._Scan(True))

    def run(args, timeout):
        calls.append(args)
        if args[0] == "ps":
            return _completed("9 ttys001 codex Fri Sep  5 01:02:03 2026\n")
        return _completed("fcwd\na \nn/work\n")

    monkeypatch.setattr(presence, "_run", run)
    original = presence.unidentified_terminals()
    original[0]["pid"] = 123
    assert presence.unidentified_terminals()[0]["pid"] == 9
    assert len(calls) == 2
    clock[0] = presence.CACHE_TTL_S + 0.1
    presence.unidentified_terminals()
    assert len(calls) == 4


@pytest.mark.parametrize("observed", [
    "42 ttys004 codex Fri Sep  5 01:02:03 2026\n",
    "42 ttys004 claude Fri Sep  5 01:02:03 2026\n",
    "42 ?? codex Fri Sep  5 01:02:03 2026\n",
    "42 ttys004 codex Fri Sep  6 01:02:03 2026\n",
    "43 ttys004 codex Fri Sep  5 01:02:03 2026\n",
    "",
])
def test_verified_process_tty_rechecks_exact_process_without_session_identity(monkeypatch, observed):
    calls = []

    def run(args, timeout):
        calls.append(args)
        return _completed(observed)

    monkeypatch.setattr(presence, "_run", run)
    tty, reason = presence.verified_process_tty(42, "codex", "Fri Sep  5 01:02:03 2026")
    if observed.startswith("42 ttys004 codex Fri Sep  5"):
        assert (tty, reason) == ("ttys004", "")
    else:
        assert tty is None
        assert reason
    assert len(calls) == 1
    assert calls[0][:3] == ["ps", "-p", "42"]


@pytest.mark.parametrize("pid,provider,started", [
    (True, "codex", "start"), (0, "codex", "start"), (-42, "codex", "start"),
    ("42", "codex", "start"), (42, "python", "start"), (42, "codex", ""),
    (42, [], "start"),
    (42, "codex", None), (42, "codex", "not a process start"),
])
def test_verified_process_tty_rejects_invalid_identity_before_native_commands(monkeypatch, pid, provider, started):
    monkeypatch.setattr(presence, "_run", lambda *args: pytest.fail("invalid identity must not run ps"))
    tty, reason = presence.verified_process_tty(pid, provider, started)
    assert tty is None
    assert reason


@pytest.mark.parametrize("evidence", ["verified", "readonly", "subagent", "duplicate", "mismatch"])
def test_unidentified_terminals_uses_native_proof_to_exclude_only_verified_windows(
    tmp_path, monkeypatch, evidence
):
    claude = tmp_path / "claude"
    claude.mkdir()
    started = "Fri Sep  5 01:02:03 2026"
    (claude / "42.json").write_text(json.dumps({
        "pid": 42, "sessionId": "claude-live", "procStart": started,
    }))
    # Broken Claude identity must leave its actual terminal visible.
    (claude / "43.json").write_text("{")
    codex = tmp_path / "codex"
    path = _rollout(codex, "a-b-c-d-e", source="subagent" if evidence == "subagent" else "user")
    if evidence == "mismatch":
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "wrong", "thread_source": "user",
        }}) + "\n")
    monkeypatch.setattr(presence, "_claude_sessions_root", lambda: claude)
    monkeypatch.setattr(presence, "_codex_sessions_root", lambda: codex)

    def run(args, timeout):
        if args[:2] == ["ps", "-p"]:
            return _completed(f"{args[2]} ttys001 claude {started}\n")
        if args[0] == "ps":
            clock = f" {started}" if "lstart=" in args[-1] else ""
            return _completed(
                f"42 ttys001 claude{clock}\n43 ttys002 claude{clock}\n9 ttys003 codex{clock}\n"
            )
        result = f"p{args[-1]}\nfcwd\na \nn/actual/cwd\n"
        if args[-1] == "9":
            result += f"f20\na{'r' if evidence == 'readonly' else 'u'}\nn{path}\n"
            if evidence == "duplicate":
                result += f"f21\nau\nn{path}\n"
        return _completed(result)

    monkeypatch.setattr(presence, "_run", run)
    windows = presence.unidentified_terminals()
    assert [w["pid"] for w in windows] == ([43] if evidence == "verified" else [43, 9])
    assert all(w["cwd"] == "/actual/cwd" for w in windows)
    assert all("session_id" not in w for w in windows)


def test_unidentified_terminals_does_not_cache_failed_process_inventory(monkeypatch):
    monkeypatch.setattr(presence, "_scan_cached", lambda *args: presence._Scan(True))
    calls = []

    def run(args, timeout):
        calls.append(args)
        return _result(1) if len(calls) == 1 else _completed("")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.unidentified_terminals() == []
    assert presence.unidentified_terminals() == []
    assert len(calls) == 2


def test_unidentified_terminals_deduplicates_ttys_and_excludes_verified_other_provider(monkeypatch):
    started = "Fri Sep  5 01:02:03 2026"
    monkeypatch.setattr(presence, "_scan_cached", lambda provider, *args: presence._Scan(
        True, verified_ttys=frozenset({"ttys001" if provider == "claude" else "ttys002"}),
    ))
    cwd_pids = []

    def run(args, timeout):
        if args[0] == "ps":
            return _completed(
                f"8 ttys001 codex {started}\n9 ttys002 claude {started}\n"
                f"10 ttys003 codex {started}\n11 ttys003 codex {started}\n"
            )
        cwd_pids.append(args[-1])
        return _completed("fcwd\na \nn/work\n")

    monkeypatch.setattr(presence, "_run", run)
    assert presence.unidentified_terminals() == [{
        "pid": 10, "tty": "ttys003", "provider": "codex", "cwd": "/work", "started": started,
    }]
    assert cwd_pids == ["10"]


@pytest.mark.parametrize("failure", [_result(1), OSError(), TimeoutError()])
def test_verified_process_tty_fails_closed_on_native_failure(monkeypatch, failure):
    def run(args, timeout):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(presence, "_run", run)
    tty, reason = presence.verified_process_tty(42, "codex", "Fri Sep  5 01:02:03 2026")
    assert tty is None
    assert reason
