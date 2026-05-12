"""Tests for the internal provider registry."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_browse.providers import alternate_provider, get_provider, provider_ids
from claude_browse.providers import codex as codex_provider

FIXTURES = Path(__file__).parent / "fixtures"


def test_provider_ids_include_claude_and_codex():
    assert provider_ids() == ("claude", "codex")


def test_get_provider_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown provider: mystery"):
        get_provider("mystery")


def test_claude_native_resume_cmd_matches_current_shape():
    spec = get_provider("claude")
    assert spec.native_resume_cmd("abc-123", True) == [
        "claude",
        "--resume",
        "abc-123",
        "--dangerously-skip-permissions",
    ]


def test_codex_native_resume_cmd_matches_current_shape():
    spec = get_provider("codex")
    assert spec.native_resume_cmd("abc-123", True) == [
        "codex",
        "resume",
        "abc-123",
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_claude_handoff_cmd_matches_current_shape():
    spec = get_provider("claude")
    assert spec.handoff_cmd("/tmp", "continue", True) == [
        "claude",
        "--dangerously-skip-permissions",
        "--add-dir",
        "/tmp",
        "continue",
    ]


def test_codex_handoff_cmd_matches_current_shape():
    spec = get_provider("codex")
    assert spec.handoff_cmd("/tmp", "continue", True) == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--add-dir",
        "/tmp",
        "continue",
    ]


def test_alternate_provider_flips_between_current_browser_targets():
    assert alternate_provider("claude") == "codex"
    assert alternate_provider("codex") == "claude"


def test_provider_capabilities_match_current_products():
    claude = get_provider("claude")
    codex = get_provider("codex")

    assert claude.can_native_resume is True
    assert claude.assistant_turns_available is True
    assert codex.can_native_resume is True
    assert codex.assistant_turns_available is False


def test_claude_provider_exposes_file_backed_helpers():
    spec = get_provider("claude")

    info = spec.session_info(str(FIXTURES / "sample_session.jsonl"))
    fields = spec.fielded_corpus(str(FIXTURES / "sample_session.jsonl"))

    assert info is not None
    assert info["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "login page crashes" in fields["first_msg"]
    assert spec.session_files_reader is not None


def test_codex_provider_lists_index_records(monkeypatch, tmp_path):
    state_path = tmp_path / "state.sqlite"
    history_path = tmp_path / "history.jsonl"

    conn = sqlite3.connect(state_path)
    conn.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            title TEXT NOT NULL,
            first_user_message TEXT NOT NULL DEFAULT '',
            created_at_ms INTEGER,
            updated_at_ms INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            thread_source TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO threads (
            id, cwd, title, first_user_message, created_at_ms, updated_at_ms,
            created_at, updated_at, thread_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "019e-test-aaaa-bbbb-cccccccccccc",
            "/Users/alice/code/codex-app",
            "Fix onboarding bug",
            "Please debug the onboarding flow",
            1_776_000_000_000,
            1_776_000_600_000,
            1_776_000_000,
            1_776_000_600,
            "user",
        ),
    )
    conn.commit()
    conn.close()

    history_path.write_text(
        "\n".join([
            '{"session_id":"019e-test-aaaa-bbbb-cccccccccccc","ts":1776000000,"text":"Please debug the onboarding flow"}',
            '{"session_id":"019e-test-aaaa-bbbb-cccccccccccc","ts":1776000060,"text":"yes go ahead"}',
            '{"session_id":"019e-test-aaaa-bbbb-cccccccccccc","ts":1776000120,"text":"Now switch to paywall copy after that"}',
        ]) + "\n"
    )

    monkeypatch.setattr(codex_provider, "CODEX_STATE_DB", str(state_path))
    monkeypatch.setattr(codex_provider, "CODEX_HISTORY_PATH", str(history_path))
    codex_provider._CODEX_HISTORY_CACHE["mtime"] = None
    codex_provider._CODEX_HISTORY_CACHE["entries"] = {}

    spec = get_provider("codex")
    records = spec.list_index_records()

    assert len(records) == 1
    assert records[0]["provider"] == "codex"
    assert records[0]["session_id"] == "019e-test-aaaa-bbbb-cccccccccccc"
    assert "paywall copy" in records[0]["last_msg"]
