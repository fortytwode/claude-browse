"""Tests for claude_browse.browse: row formatting for fzf.

The browse module is mostly fzf integration (hard to unit-test), but
format_row is a pure function and worth pinning — it has historically been
the source of subtle bugs where embedded control characters split a logical
row into multiple visual rows in the picker.
"""

from __future__ import annotations

import pytest

from claude_browse import browse
from claude_browse.browse import format_row


def _info(**overrides) -> dict:
    base = {
        "session_id": "abc-123",
        "provider": "claude",
        "cwd": "/home/alice/proj",
        "name": "my session",
        "first_msg": "first user message",
        "last_msg": "",
        "msg_count": 10,
        "timestamp": "2026-05-01T10:00:00Z",
        "last_timestamp": "2026-05-01T10:00:00Z",
        "context": "",
    }
    base.update(overrides)
    return base


def test_format_row_strips_newlines_from_snippet():
    """fzf treats \\n as a row delimiter. A snippet that crosses a newline
    would split one logical row into two visual rows; only the second
    carries the ###sid### tail, so picking the first becomes a no-op.
    """
    info = _info(context="line one\nline two\nline three")
    row = format_row(info, query="anything")
    assert "\n" not in row


def test_format_row_strips_carriage_returns_and_tabs_from_snippet():
    info = _info(context="col1\tcol2\rwith CR")
    row = format_row(info, query="anything")
    assert "\n" not in row
    assert "\r" not in row
    assert "\t" not in row


def test_format_row_strips_newlines_from_last_msg():
    """The topic-drift suffix (when no query is active) uses last_msg.
    Same row-splitting risk if last_msg contains newlines.
    """
    info = _info(
        name="title with no overlap",
        last_msg="multi\nline\nlast message that drifted",
    )
    row = format_row(info, query="")
    assert "\n" not in row


def test_format_row_keeps_sid_tail_attached():
    """No matter what's in the suffix, the row ends with ###sid###cwd so
    fzf's --delimiter=### selection logic finds the sid."""
    info = _info(context="a\nb\nc\nd")
    row = format_row(info, query="anything")
    assert row.rstrip().endswith("abc-123###/home/alice/proj###claude")


def test_default_target_provider_follows_entrypoint_name():
    assert browse._default_target_provider("claude-browse") == "claude"
    assert browse._default_target_provider("/tmp/codex-browse") == "codex"


def test_parse_target_provider_allows_override():
    target, remaining = browse._parse_target_provider(
        ["--target", "codex", "--all"],
        "claude-browse",
    )
    assert target == "codex"
    assert remaining == ["--all"]


def test_open_in_target_provider_native_resume_when_source_matches_target(
    monkeypatch,
):
    session = _info()
    captured: list[object] = []

    monkeypatch.setattr(
        browse,
        "_native_resume",
        lambda *args: captured.append(("native", args)),
    )
    monkeypatch.setattr(
        browse,
        "_continue_in_provider",
        lambda *args: captured.append(("handoff", args)),
    )

    browse._open_in_target_provider(
        session,
        "claude",
        "claude",
        "abc-123",
        "/home/alice/proj",
        (),
        True,
    )

    assert captured and captured[0][0] == "native"


def test_open_in_target_provider_handoffs_when_source_differs_from_target(
    monkeypatch,
):
    session = _info(provider="codex")
    captured: list[object] = []

    monkeypatch.setattr(
        browse,
        "_native_resume",
        lambda *args: captured.append(("native", args)),
    )
    monkeypatch.setattr(
        browse,
        "_continue_in_provider",
        lambda *args: captured.append(("handoff", args)),
    )

    browse._open_in_target_provider(
        session,
        "codex",
        "claude",
        "abc-123",
        "/home/alice/proj",
        (),
        False,
    )

    assert captured and captured[0][0] == "handoff"


def test_continue_in_other_app_from_claude_execs_codex_with_add_dir(
    monkeypatch,
):
    session = _info(path="/tmp/session.jsonl")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="": (
            "/tmp/claude_browse_import.md"
            if target_provider == "codex"
            else "/tmp/unexpected.md"
        ),
    )
    monkeypatch.setattr(browse.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def fake_execvp(binary: str, cmd: list[str]) -> None:
        captured["binary"] = binary
        captured["cmd"] = cmd
        raise SystemExit(0)

    monkeypatch.setattr(browse.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit):
        browse._continue_in_other_app(
            session,
            "claude",
            "abc-123",
            "/home/alice/proj",
            (),
        )

    assert captured["binary"] == "codex"
    assert captured["cmd"] == [
        "codex",
        "--add-dir",
        "/tmp",
        (
            "Continue the imported Claude session context from "
            "/tmp/claude_browse_import.md. Treat it as prior conversation "
            "state, read that file first, use the Reopen Intent section as "
            "the reason this thread was selected, prioritize the "
            "end-of-thread state and most recent turns over the original "
            "opening prompt, then continue the work in this directory."
        ),
    ]


def test_continue_in_other_app_from_codex_execs_claude_with_add_dir(
    monkeypatch,
):
    session = _info(provider="codex", path="codex://abc-123")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="": (
            "/tmp/codex_browse_import.md"
            if target_provider == "claude"
            else "/tmp/unexpected.md"
        ),
    )
    monkeypatch.setattr(browse.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def fake_execvp(binary: str, cmd: list[str]) -> None:
        captured["binary"] = binary
        captured["cmd"] = cmd
        raise SystemExit(0)

    monkeypatch.setattr(browse.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit):
        browse._continue_in_other_app(
            session,
            "codex",
            "abc-123",
            "/home/alice/proj",
            (),
        )

    assert captured["binary"] == "claude"
    assert captured["cmd"] == [
        "claude",
        "--add-dir",
        "/tmp",
        (
            "Continue the imported CodeX session context from "
            "/tmp/codex_browse_import.md. Treat it as prior conversation "
            "state, read that file first, use the Reopen Intent section as "
            "the reason this thread was selected, prioritize the "
            "end-of-thread state and most recent turns over the original "
            "opening prompt, then continue the work in this directory."
        ),
    ]


def test_continue_in_other_app_errors_when_target_binary_missing(monkeypatch):
    session = _info(provider="codex", path="codex://abc-123")
    monkeypatch.setattr(browse.shutil, "which", lambda _binary: None)

    with pytest.raises(SystemExit) as exc:
        browse._continue_in_other_app(
            session,
            "codex",
            "abc-123",
            "/home/alice/proj",
            (),
        )

    assert exc.value.code == 1
