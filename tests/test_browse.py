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
    assert browse._default_target_provider("/tmp/gemini-browse") == "gemini"


def test_default_target_provider_supports_dynamic_plugin_shims(monkeypatch):
    monkeypatch.setattr(
        browse,
        "provider_ids",
        lambda **kwargs: ("claude", "codex", "mystery"),
    )
    assert browse._default_target_provider("/tmp/mystery-browse") == "mystery"


def test_parse_target_provider_allows_override():
    target, remaining = browse._parse_target_provider(
        ["--target", "codex", "--all"],
        "claude-browse",
    )
    assert target == "codex"
    assert remaining == ["--all"]


def test_parse_target_provider_allows_gemini_override():
    target, remaining = browse._parse_target_provider(
        ["--target=gemini", "--here"],
        "claude-browse",
    )
    assert target == "gemini"
    assert remaining == ["--here"]


def test_parse_target_provider_uses_target_capable_provider_list(monkeypatch):
    calls: list[tuple[bool | None, bool | None]] = []

    def fake_provider_ids(*, source_capable=None, target_capable=None):
        calls.append((source_capable, target_capable))
        return ("claude", "cursor")

    monkeypatch.setattr(browse, "provider_ids", fake_provider_ids)

    target, remaining = browse._parse_target_provider(
        ["--target", "cursor"],
        "claude-browse",
    )

    assert target == "cursor"
    assert remaining == []
    assert calls[0] == (None, True)


def test_providers_with_local_state_use_source_capability_filter(monkeypatch):
    calls: list[tuple[bool | None, bool | None]] = []

    def fake_provider_ids(*, source_capable=None, target_capable=None):
        calls.append((source_capable, target_capable))
        return ("claude", "gemini")

    specs = {
        "claude": type("Spec", (), {"has_local_state": lambda self: True})(),
        "gemini": type("Spec", (), {"has_local_state": lambda self: False})(),
    }

    monkeypatch.setattr(browse, "provider_ids", fake_provider_ids)
    monkeypatch.setattr(browse, "get_provider", lambda provider: specs[provider])

    assert browse._providers_with_local_state() == ["claude"]
    assert calls[0] == (True, None)


def test_parse_fzf_output_handles_print_query_safe_marker():
    row = "match ###abc-123###/home/alice/proj###claude"
    parsed = browse._parse_fzf_output(
        f"pokpok\nSAFE:\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", False, "pokpok")


def test_parse_fzf_output_handles_print_query_default_accept():
    row = "match ###abc-123###/home/alice/proj###codex"
    parsed = browse._parse_fzf_output(
        f"claude browse\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", True, "claude browse")


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


def test_continue_in_provider_from_claude_execs_gemini_with_include_directories(
    monkeypatch,
):
    session = _info(path="/tmp/session.jsonl")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="": (
            "/tmp/claude_browse_import.md"
            if target_provider == "gemini"
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
        browse._continue_in_provider(
            session,
            "claude",
            "gemini",
            "/home/alice/proj",
            (),
            True,
            "",
        )

    assert captured["binary"] == "gemini"
    assert captured["cmd"] == [
        "gemini",
        "--yolo",
        "--include-directories",
        "/tmp",
        "--prompt-interactive",
        (
            "Continue the imported Claude session context from "
            "/tmp/claude_browse_import.md. Treat it as prior conversation "
            "state, read that file first, use the Reopen Intent section as "
            "the reason this thread was selected, prioritize the "
            "end-of-thread state and most recent turns over the original "
            "opening prompt, then continue the work in this directory."
        ),
    ]


def test_continue_in_provider_from_gemini_execs_claude_with_add_dir(
    monkeypatch,
):
    session = _info(provider="gemini", path="/tmp/gemini-session.json")
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
        browse._continue_in_provider(
            session,
            "gemini",
            "claude",
            "/home/alice/proj",
            (),
            True,
            "",
        )

    assert captured["binary"] == "claude"
    assert captured["cmd"] == [
        "claude",
        "--dangerously-skip-permissions",
        "--add-dir",
        "/tmp",
        (
            "Continue the imported Gemini session context from "
            "/tmp/codex_browse_import.md. Treat it as prior conversation "
            "state, read that file first, use the Reopen Intent section as "
            "the reason this thread was selected, prioritize the "
            "end-of-thread state and most recent turns over the original "
            "opening prompt, then continue the work in this directory."
        ),
    ]


def test_continue_in_provider_errors_when_target_binary_missing(monkeypatch):
    session = _info(provider="gemini", path="/tmp/gemini-session.json")
    monkeypatch.setattr(browse.shutil, "which", lambda _binary: None)

    with pytest.raises(SystemExit) as exc:
        browse._continue_in_provider(
            session,
            "gemini",
            "claude",
            "/home/alice/proj",
            (),
            True,
            "",
        )

    assert exc.value.code == 1
