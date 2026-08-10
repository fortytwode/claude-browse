"""Tests for the internal provider registry."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from claude_browse.providers import claude as claude_provider
from claude_browse.providers import codex as codex_provider
from claude_browse.providers import common as provider_common
from claude_browse.providers import copilot as copilot_provider
from claude_browse.providers import gemini as gemini_provider
from claude_browse.providers import get_provider, provider_entries, provider_ids
from claude_browse.providers.base import PROVIDER_API_VERSION, ProviderSpec

FIXTURES = Path(__file__).parent / "fixtures"


def test_provider_ids_include_claude_codex_gemini_copilot_and_cursor():
    assert provider_ids() == ("claude", "codex", "gemini", "copilot", "cursor")


def test_provider_ids_filter_external_target_only_provider(monkeypatch, tmp_path):
    module_path = tmp_path / "mystery_provider.py"
    module_path.write_text(
        """
from claude_browse.providers.base import ProviderSpec

PROVIDER = ProviderSpec(
    provider_id="mystery",
    display_name="Mystery",
    binary="mystery",
    native_resume_prefix=("mystery", "resume"),
    list_index_records_reader=lambda: [],
    preview_messages_reader=lambda path, session_id: [],
    transcript_turns_reader=lambda path, session_id: [],
    source_capable=False,
    target_capable=True,
    experimental=True,
)
"""
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("CLAUDE_BROWSE_PROVIDER_MODULES", "mystery_provider")

    assert provider_ids() == (
        "claude",
        "codex",
        "gemini",
        "copilot",
        "cursor",
        "mystery",
    )
    assert provider_ids(source_capable=True) == (
        "claude",
        "codex",
        "gemini",
        "copilot",
    )
    assert provider_ids(target_capable=True) == (
        "claude",
        "codex",
        "gemini",
        "copilot",
        "cursor",
        "mystery",
    )
    assert get_provider("mystery").experimental is True


def test_provider_dirs_load_external_provider(monkeypatch, tmp_path):
    provider_file = tmp_path / "mystery_provider.py"
    provider_file.write_text(
        f"""
from claude_browse.providers.base import ProviderSpec

PROVIDER_API_VERSION = {PROVIDER_API_VERSION}

PROVIDER = ProviderSpec(
    provider_id="mystery",
    display_name="Mystery",
    binary="mystery",
    native_resume_prefix=("mystery", "resume"),
    list_index_records_reader=lambda: [],
    preview_messages_reader=lambda path, session_id: [],
    transcript_turns_reader=lambda path, session_id: [],
    source_capable=False,
    target_capable=True,
    experimental=True,
)
"""
    )
    monkeypatch.setenv("CLAUDE_BROWSE_PROVIDER_DIRS", str(tmp_path))

    assert provider_ids() == (
        "claude",
        "codex",
        "gemini",
        "copilot",
        "cursor",
        "mystery",
    )
    mystery_entry = next(
        entry for entry in provider_entries()
        if entry.spec.provider_id == "mystery"
    )

    assert get_provider("mystery").binary == "mystery"
    assert mystery_entry.source_type == "file"
    assert mystery_entry.origin == f"file:{provider_file}"


def test_external_provider_api_version_mismatch_is_skipped(
    monkeypatch,
    tmp_path,
    capsys,
):
    provider_file = tmp_path / "future_provider.py"
    provider_file.write_text(
        """
from claude_browse.providers.base import ProviderSpec

PROVIDER_API_VERSION = 999

PROVIDER = ProviderSpec(
    provider_id="future",
    display_name="Future",
    binary="future",
    native_resume_prefix=("future", "resume"),
    list_index_records_reader=lambda: [],
    preview_messages_reader=lambda path, session_id: [],
    transcript_turns_reader=lambda path, session_id: [],
)
"""
    )
    monkeypatch.setenv("CLAUDE_BROWSE_PROVIDER_DIRS", str(tmp_path))

    assert provider_ids() == ("claude", "codex", "gemini", "copilot", "cursor")
    assert "PROVIDER_API_VERSION" in capsys.readouterr().err


def test_duplicate_external_provider_is_skipped(monkeypatch, tmp_path, capsys):
    module_path = tmp_path / "duplicate_provider.py"
    module_path.write_text(
        """
from claude_browse.providers.base import ProviderSpec

PROVIDER = ProviderSpec(
    provider_id="claude",
    display_name="Shadow Claude",
    binary="shadow-claude",
    native_resume_prefix=("shadow-claude", "resume"),
    list_index_records_reader=lambda: [],
    preview_messages_reader=lambda path, session_id: [],
    transcript_turns_reader=lambda path, session_id: [],
)
"""
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("CLAUDE_BROWSE_PROVIDER_MODULES", "duplicate_provider")

    assert provider_ids() == ("claude", "codex", "gemini", "copilot", "cursor")
    assert get_provider("claude").binary == "claude"
    assert "duplicates an existing provider" in capsys.readouterr().err


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
        "--",
        "continue",
    ]


def test_claude_handoff_cmd_with_extra_dirs_adds_them_after_import_dir():
    spec = get_provider("claude")
    cmd = spec.handoff_cmd(
        "/tmp/import", "continue", False, extra_dirs=("/home/a/.claude/projects/bkt",)
    )
    assert cmd == [
        "claude",
        "--add-dir",
        "/tmp/import",
        "--add-dir",
        "/home/a/.claude/projects/bkt",
        "--",
        "continue",
    ]


def test_handoff_cmd_dedups_and_skips_empty_extra_dirs():
    spec = get_provider("claude")
    cmd = spec.handoff_cmd(
        "/tmp/import", "continue", False, extra_dirs=("", "/tmp/import", "/other")
    )
    # Empty skipped, duplicate of import_dir skipped, /other kept exactly once.
    assert cmd.count("--add-dir") == 2
    assert "/other" in cmd
    assert "" not in cmd


def test_codex_handoff_cmd_matches_current_shape():
    spec = get_provider("codex")
    assert spec.handoff_cmd("/tmp", "continue", True) == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "--add-dir",
        "/tmp",
        "continue",
    ]


def test_gemini_native_resume_cmd_matches_current_shape():
    spec = get_provider("gemini")
    assert spec.native_resume_cmd("abc-123", True) == [
        "gemini",
        "--resume",
        "abc-123",
        "--yolo",
    ]


def test_gemini_handoff_cmd_matches_current_shape():
    spec = get_provider("gemini")
    assert spec.handoff_cmd("/tmp", "continue", True) == [
        "gemini",
        "--yolo",
        "--include-directories",
        "/tmp",
        "--prompt-interactive",
        "continue",
    ]


def test_cursor_native_resume_cmd_matches_current_shape():
    spec = get_provider("cursor")
    assert spec.native_resume_cmd("abc-123", True) == [
        "cursor-agent",
        "--resume",
        "abc-123",
        "--force",
    ]


def test_cursor_handoff_cmd_matches_current_shape():
    spec = get_provider("cursor")
    assert spec.handoff_cmd(None, "continue", True) == [
        "cursor-agent",
        "--force",
        "continue",
    ]


def test_copilot_native_resume_cmd_matches_current_shape():
    spec = get_provider("copilot")
    assert spec.native_resume_cmd("abc-123", True) == [
        "copilot",
        "--resume",
        "abc-123",
        "--yolo",
    ]


def test_copilot_handoff_cmd_matches_current_shape():
    spec = get_provider("copilot")
    assert spec.handoff_cmd("/tmp", "continue", True) == [
        "copilot",
        "--yolo",
        "--add-dir",
        "/tmp",
        "continue",
    ]


def test_provider_capabilities_match_current_products():
    claude = get_provider("claude")
    codex = get_provider("codex")
    gemini = get_provider("gemini")
    copilot = get_provider("copilot")
    cursor = get_provider("cursor")

    assert claude.can_native_resume is True
    assert claude.assistant_turns_available is True
    assert codex.can_native_resume is True
    assert codex.assistant_turns_available is True
    assert gemini.can_native_resume is True
    assert gemini.assistant_turns_available is True
    assert copilot.source_capable is True
    assert copilot.target_capable is True
    assert copilot.assistant_turns_available is True
    assert cursor.source_capable is False
    assert cursor.target_capable is True
    assert cursor.handoff_via_file is False


def test_provider_spec_availability_and_auth_helpers():
    spec = ProviderSpec(
        provider_id="demo",
        display_name="Demo",
        binary="demo",
        native_resume_prefix=("demo", "resume"),
        list_index_records_reader=lambda: [],
        preview_messages_reader=lambda path, session_id: [],
        transcript_turns_reader=lambda path, session_id: [],
        availability_reader=lambda: True,
        auth_status_reader=lambda: "signed-in",
        source_capable=False,
        target_capable=True,
        experimental=True,
    )

    assert spec.is_available() is True
    assert spec.auth_status() == "signed-in"
    assert spec.source_capable is False
    assert spec.target_capable is True


def test_provider_entries_expose_origin_metadata():
    entries = provider_entries()
    builtin_ids = {
        entry.spec.provider_id
        for entry in entries
        if entry.source_type == "builtin"
    }

    assert {"claude", "codex", "gemini", "copilot", "cursor"} <= builtin_ids


def test_claude_provider_exposes_file_backed_helpers():
    spec = get_provider("claude")

    info = spec.session_info(str(FIXTURES / "sample_session.jsonl"))
    fields = spec.fielded_corpus(str(FIXTURES / "sample_session.jsonl"))

    assert info is not None
    assert info["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert "login page crashes" in fields["first_msg"]
    assert spec.session_files_reader is not None


def test_claude_provider_recovers_history_sessions_missed_by_glob(
    monkeypatch,
    tmp_path,
):
    sessions_dir = tmp_path / "projects"
    project_dir = sessions_dir / "-Users-alice-work"
    project_dir.mkdir(parents=True)
    session_path = project_dir / "hist-session.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "hist-session",
                        "cwd": "/Users/alice/work",
                        "timestamp": "2026-06-22T08:00:00Z",
                        "message": {
                            "role": "user",
                            "content": "Find the chess deck discussion",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "hist-session",
                        "cwd": "/Users/alice/work",
                        "timestamp": "2026-06-22T08:01:00Z",
                        "message": {
                            "role": "assistant",
                            "content": "The deck discussion is in this thread.",
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "display": "Find the chess deck discussion",
                "project": "/Users/alice/work",
                "sessionId": "hist-session",
            }
        )
        + "\n"
    )

    monkeypatch.setattr(claude_provider, "SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(claude_provider, "HISTORY_PATH", str(history_path))
    monkeypatch.setattr(claude_provider.glob, "glob", lambda _pattern: [])

    files = claude_provider.list_session_files()
    assert files == [str(session_path)]

    records = claude_provider.list_index_records()
    assert len(records) == 1
    assert records[0]["session_id"] == "hist-session"
    assert records[0]["first_msg"] == "Find the chess deck discussion"


def test_split_boilerplate_moves_agent_instruction_dump_out_of_body():
    text = (
        "# AGENTS.md instructions for /Users/alice/team-operations\n"
        "<INSTRUCTIONS>\n"
        "## Compound Codex Tool Mapping\n"
        "| CFO | Vela | Finance, cash flow, profitability |\n"
        "</INSTRUCTIONS>\n"
        "Please review the CFO weekly report."
    )

    body, boilerplate = provider_common.split_boilerplate(text)

    assert body == "Please review the CFO weekly report."
    assert len(boilerplate) == 1
    assert "AGENTS.md instructions" in boilerplate[0]
    assert "Vela" in boilerplate[0]


def test_claude_provider_keeps_instruction_dump_out_of_first_msg(tmp_path):
    session_path = tmp_path / "claude-agent-dump.jsonl"
    instruction_dump = (
        "# CLAUDE.md instructions for /Users/alice/team-operations\n"
        "<INSTRUCTIONS>\n"
        "## Team Operations\n"
        "| CFO | Vela | Finance, cash flow, profitability |\n"
        "</INSTRUCTIONS>"
    )
    session_path.write_text(
        "\n".join([
            json.dumps({
                "type": "user",
                "sessionId": "claude-agent-dump",
                "cwd": "/Users/alice/team-operations",
                "timestamp": "2026-05-12T08:00:00Z",
                "message": {
                    "role": "user",
                    "content": instruction_dump,
                },
            }),
            json.dumps({
                "type": "user",
                "sessionId": "claude-agent-dump",
                "cwd": "/Users/alice/team-operations",
                "timestamp": "2026-05-12T08:02:00Z",
                "message": {
                    "role": "user",
                    "content": "Please review the CFO weekly report.",
                },
            }),
        ])
        + "\n"
    )

    info = claude_provider.get_session_info(str(session_path))
    fields = claude_provider.extract_fielded_corpus(str(session_path))

    assert info is not None
    assert info["first_msg"] == "Please review the CFO weekly report."
    assert fields["first_msg"] == "please review the cfo weekly report."
    assert "vela" not in fields["first_msg"]
    assert "vela" not in fields["user_text"]
    assert "vela" in fields["boilerplate"]


def test_claude_provider_get_live_activity_reads_latest_timestamp(tmp_path):
    session_path = tmp_path / "session.jsonl"
    session_path.write_text(
        json.dumps({"timestamp": "2026-05-12T08:00:00Z"})
        + "\n"
        + ("x" * 5000)
        + "\n"
        + json.dumps({"timestamp": "2026-05-12T08:02:00Z"})
        + "\n"
    )

    timestamp, mtime = claude_provider.get_live_activity(str(session_path))

    assert timestamp == "2026-05-12T08:02:00Z"
    assert mtime == os.path.getmtime(session_path)


def test_codex_provider_lists_index_records(monkeypatch, tmp_path):
    state_path = tmp_path / "state.sqlite"
    history_path = tmp_path / "history.jsonl"
    sessions_dir = tmp_path / "sessions" / "2026" / "05" / "12"
    sessions_dir.mkdir(parents=True)
    session_path = sessions_dir / (
        "rollout-2026-05-12T09-05-25-019e-test-aaaa-bbbb-cccccccccccc.jsonl"
    )

    conn = sqlite3.connect(state_path)
    conn.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
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
            id, path, cwd, title, first_user_message, created_at_ms, updated_at_ms,
            created_at, updated_at, thread_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "019e-test-aaaa-bbbb-cccccccccccc",
            str(session_path),
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
    session_path.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-12T08:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Please debug the onboarding flow",
                },
            }),
            json.dumps({
                "timestamp": "2026-05-12T08:00:05.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "I found the root cause in the signup gate.",
                        }
                    ],
                },
            }),
            json.dumps({
                "timestamp": "2026-05-12T08:00:30.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Now switch to paywall copy after that",
                },
            }),
            json.dumps({
                "timestamp": "2026-05-12T08:01:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": (
                        "Now switch to paywall copy after that and keep the "
                        "same relief structure."
                    ),
                },
            }),
        ]) + "\n"
    )

    monkeypatch.setattr(codex_provider, "CODEX_STATE_DB", str(state_path))
    monkeypatch.setattr(codex_provider, "CODEX_HISTORY_PATH", str(history_path))
    monkeypatch.setattr(codex_provider, "CODEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    codex_provider._CODEX_HISTORY_CACHE["mtime"] = None
    codex_provider._CODEX_HISTORY_CACHE["entries"] = {}
    codex_provider._CODEX_SESSION_TURNS_CACHE["entries"] = {}

    spec = get_provider("codex")
    records = spec.list_index_records()

    assert len(records) == 1
    assert records[0]["provider"] == "codex"
    assert records[0]["session_id"] == "019e-test-aaaa-bbbb-cccccccccccc"
    assert records[0]["path"] == str(session_path)
    assert "paywall copy" in records[0]["last_msg"]
    assert "root cause in the signup gate" in records[0]["fields"]["asst_text"]


def test_codex_provider_keeps_agent_instructions_out_of_title_and_first_msg(tmp_path):
    session_path = tmp_path / "rollout-2026-05-12T09-05-25-019e-agent-dump.jsonl"
    instruction_dump = (
        "# AGENTS.md instructions for /Users/alice/team-operations\n"
        "<INSTRUCTIONS>\n"
        "## Compound Codex Tool Mapping\n"
        "| CFO | Vela | Finance, cash flow, profitability |\n"
        "</INSTRUCTIONS>"
    )
    session_path.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-12T08:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": instruction_dump,
                },
            }),
            json.dumps({
                "timestamp": "2026-05-12T08:02:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Please review the CFO weekly report.",
                },
            }),
        ])
        + "\n"
    )
    codex_provider._CODEX_SESSION_TURNS_CACHE["entries"] = {}

    record = codex_provider._build_index_record(
        "019e-agent-dump",
        str(session_path),
        {
            "first_user_message": instruction_dump,
            "title": instruction_dump,
            "cwd": "/Users/alice/team-operations",
            "created_ms": 1_776_000_000_000,
            "updated_ms": 1_776_000_600_000,
        },
        {
            "timestamp": "2026-05-12T08:00:00.000Z",
            "last_timestamp": "2026-05-12T08:02:00.000Z",
        },
        {},
    )

    assert record is not None
    assert record["first_msg"] == "Please review the CFO weekly report."
    assert record["name"] == "Please review the CFO weekly report."
    assert record["fields"]["title"] == ""
    assert record["fields"]["first_msg"] == "please review the cfo weekly report."
    assert "vela" not in record["fields"]["first_msg"]
    assert "vela" not in record["fields"]["user_text"]
    assert "vela" in record["fields"]["boilerplate"]


def test_codex_history_only_record_preserves_instruction_boilerplate():
    instruction_dump = (
        "# AGENTS.md instructions for /Users/alice/team-operations\n"
        "<INSTRUCTIONS>\n"
        "| CFO | Vela | Finance, cash flow, profitability |\n"
        "</INSTRUCTIONS>"
    )

    record = codex_provider._build_index_record(
        "019e-history-agent-dump",
        "",
        {
            "cwd": "/Users/alice/team-operations",
            "created_ms": 1_776_000_000_000,
            "updated_ms": 1_776_000_600_000,
        },
        {"timestamp": "2026-05-12T08:00:00.000Z"},
        {
            "019e-history-agent-dump": [
                {"text": instruction_dump, "ts": 1_776_000_000},
                {"text": "Please review the CFO weekly report.", "ts": 1_776_000_120},
            ]
        },
    )

    assert record is not None
    assert record["first_msg"] == "Please review the CFO weekly report."
    assert "vela" not in record["fields"]["user_text"]
    assert "vela" in record["fields"]["boilerplate"]


def test_codex_state_records_tolerate_locked_state_db(monkeypatch, tmp_path):
    state_path = tmp_path / "state.sqlite"
    state_path.write_text("placeholder")
    calls: list[dict[str, object]] = []

    class LockedConn:
        def execute(self, sql, *args):
            if str(sql).strip().upper().startswith("PRAGMA"):
                return self
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            return None

    def fake_connect(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return LockedConn()

    monkeypatch.setattr(codex_provider, "CODEX_STATE_DB", str(state_path))
    monkeypatch.setattr(codex_provider.sqlite3, "connect", fake_connect)

    records, mtime = codex_provider._load_state_records()

    assert records == []
    assert mtime == state_path.stat().st_mtime
    assert calls[0]["kwargs"]["uri"] is True
    assert calls[0]["kwargs"]["timeout"] == codex_provider.SQLITE_BUSY_TIMEOUT_MS / 1000


def test_codex_provider_indexes_jsonl_session_without_state_row(monkeypatch, tmp_path):
    sessions_dir = tmp_path / "sessions" / "2026" / "05" / "12"
    sessions_dir.mkdir(parents=True)
    session_path = sessions_dir / (
        "rollout-2026-05-12T09-05-25-jsonl-only-session.jsonl"
    )
    subagent_path = sessions_dir / (
        "rollout-2026-05-12T09-05-30-subagent-session.jsonl"
    )
    session_path.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-12T08:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "jsonl-only-session",
                    "cwd": "/Users/alice/code/codex-app",
                    "thread_source": "user",
                },
            }),
            json.dumps({
                "timestamp": "2026-05-12T08:00:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Find the Notion note about Neil's performance review.",
                },
            }),
            json.dumps({
                "timestamp": "2026-05-12T08:00:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "I found the matching Nevena strategy reset note.",
                },
            }),
        ]) + "\n"
    )
    subagent_path.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-12T08:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "subagent-session",
                    "cwd": "/Users/alice/code/codex-app",
                    "thread_source": "subagent",
                },
            }),
            json.dumps({
                "timestamp": "2026-05-12T08:00:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "This subagent transcript should stay hidden.",
                },
            }),
        ]) + "\n"
    )

    monkeypatch.setattr(codex_provider, "CODEX_STATE_DB", str(tmp_path / "missing.sqlite"))
    monkeypatch.setattr(codex_provider, "CODEX_HISTORY_PATH", str(tmp_path / "missing-history.jsonl"))
    monkeypatch.setattr(codex_provider, "CODEX_SESSIONS_DIR", str(tmp_path / "sessions"))
    codex_provider._CODEX_HISTORY_CACHE["mtime"] = None
    codex_provider._CODEX_HISTORY_CACHE["entries"] = {}
    codex_provider._CODEX_SESSION_TURNS_CACHE["entries"] = {}

    records = codex_provider.list_index_records()

    assert [record["session_id"] for record in records] == ["jsonl-only-session"]
    assert records[0]["path"] == str(session_path)
    assert records[0]["cwd"] == "/Users/alice/code/codex-app"
    assert "Neil's performance review" in records[0]["first_msg"]
    assert "nevena strategy reset note" in records[0]["fields"]["asst_text"]


def test_codex_provider_transcript_turns_prefer_session_file(monkeypatch, tmp_path):
    history_path = tmp_path / "history.jsonl"
    session_path = tmp_path / "rollout-2026-05-12T09-05-25-019e-test.jsonl"

    history_path.write_text(
        '{"session_id":"019e-test","ts":1776000000,"text":"fallback user text"}\n'
    )
    session_path.write_text(
        "\n".join([
            json.dumps({
                "timestamp": "2026-05-12T08:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "Port the travel relief sequence into everyday-life moments",
                },
            }),
            json.dumps({
                "timestamp": "2026-05-12T08:00:05.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Pokpok already knows how to stage parent stress and calmer outcome.",
                        }
                    ],
                },
            }),
        ]) + "\n"
    )

    monkeypatch.setattr(codex_provider, "CODEX_HISTORY_PATH", str(history_path))
    codex_provider._CODEX_HISTORY_CACHE["mtime"] = None
    codex_provider._CODEX_HISTORY_CACHE["entries"] = {}
    codex_provider._CODEX_SESSION_TURNS_CACHE["entries"] = {}

    assert codex_provider.transcript_turns(str(session_path), "019e-test") == [
        ("user", "Port the travel relief sequence into everyday-life moments"),
        (
            "assistant",
            "Pokpok already knows how to stage parent stress and calmer outcome.",
        ),
    ]


def test_gemini_provider_lists_index_records(monkeypatch, tmp_path):
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("HOME", "/home/alice")

    tmp_dir = tmp_path / "tmp"
    project_dir = tmp_dir / "team-operations"
    chats_dir = project_dir / "chats"
    chats_dir.mkdir(parents=True)

    (project_dir / ".project_root").write_text("/home/alice/team-operations\n")
    (chats_dir / "session-2026-05-12T08-00-abcdef12.json").write_text(
        """{
  "sessionId": "gemini-1234-uuid",
  "projectHash": "hash",
  "startTime": "2026-05-12T08:00:00Z",
  "lastUpdated": "2026-05-12T08:05:00Z",
  "messages": [
    {"id": "1", "timestamp": "2026-05-12T08:00:00Z", "type": "user", "content": [{"text": "Please review the homepage copy"}]},
    {"id": "2", "timestamp": "2026-05-12T08:01:00Z", "type": "gemini", "content": [{"text": "I found three issues in the headline."}]},
    {"id": "3", "timestamp": "2026-05-12T08:05:00Z", "type": "user", "content": [{"text": "Now switch to the pricing page"}]}
  ],
  "kind": "main"
}"""
    )

    monkeypatch.setattr(gemini_provider, "GEMINI_TMP_DIR", str(tmp_dir))
    monkeypatch.setattr(
        gemini_provider,
        "GEMINI_PROJECTS_PATH",
        str(tmp_path / "projects.json"),
    )

    spec = get_provider("gemini")
    records = spec.list_index_records()

    assert len(records) == 1
    assert records[0]["provider"] == "gemini"
    assert records[0]["session_id"] == "gemini-1234-uuid"
    assert records[0]["cwd"] == "/home/alice/team-operations"
    assert "pricing page" in records[0]["last_msg"]


def test_copilot_provider_lists_index_records(monkeypatch, tmp_path):
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("HOME", "/home/alice")

    session_dir = tmp_path / "session-state" / "copilot-session-1234"
    session_dir.mkdir(parents=True)

    (session_dir / "workspace.yaml").write_text(
        "\n".join([
            "name: Pricing rewrite",
            "context:",
            "  cwd: /Users/alice/team-operations",
        ]) + "\n"
    )
    (session_dir / "events.jsonl").write_text(
        "\n".join([
            (
                '{"id":"1","timestamp":"2026-05-12T09:00:00Z","parentId":null,'
                '"type":"session.context_changed","data":{"cwd":"/Users/alice/team-operations"}}'
            ),
            (
                '{"id":"2","timestamp":"2026-05-12T09:01:00Z","parentId":"1",'
                '"type":"user.message","data":{"content":"Please review the pricing page copy"}}'
            ),
            (
                '{"id":"3","timestamp":"2026-05-12T09:02:00Z","parentId":"2",'
                '"type":"assistant.message","data":{"messageId":"m1","content":"I found three issues in the hero section."}}'
            ),
            (
                '{"id":"4","timestamp":"2026-05-12T09:03:00Z","parentId":"3",'
                '"type":"user.message","data":{"content":"Now switch to the FAQ section"}}'
            ),
        ]) + "\n"
    )

    monkeypatch.setattr(copilot_provider, "SESSION_STATE_DIR", str(tmp_path / "session-state"))

    spec = get_provider("copilot")
    records = spec.list_index_records()

    assert len(records) == 1
    assert records[0]["provider"] == "copilot"
    assert records[0]["session_id"] == "copilot-session-1234"
    assert records[0]["name"] == "Pricing rewrite"
    assert records[0]["cwd"] == "/home/alice/team-operations"
    assert "faq section" in records[0]["last_msg"].lower()
    assert "hero section" in records[0]["fields"]["asst_text"]


def _make_codex_corpus(tmp_path, *, updated_ms=1_776_000_600_000):
    """State DB + history + one session file with pinned mtimes."""
    state_path = tmp_path / "state.sqlite"
    history_path = tmp_path / "history.jsonl"
    sessions_dir = tmp_path / "sessions" / "2026" / "05" / "12"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_path = sessions_dir / (
        "rollout-2026-05-12T09-05-25-019e-test-aaaa-bbbb-cccccccccccc.jsonl"
    )
    sid = "019e-test-aaaa-bbbb-cccccccccccc"

    if state_path.exists():
        state_path.unlink()
    conn = sqlite3.connect(state_path)
    conn.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
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
            id, path, cwd, title, first_user_message, created_at_ms, updated_at_ms,
            created_at, updated_at, thread_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            str(session_path),
            "/Users/alice/code/codex-app",
            "Fix onboarding bug",
            "Please debug the onboarding flow",
            1_776_000_000_000,
            updated_ms,
            1_776_000_000,
            updated_ms // 1000,
            "user",
        ),
    )
    conn.commit()
    conn.close()

    history_path.write_text(
        f'{{"session_id":"{sid}","ts":1776000000,"text":"Please debug the onboarding flow"}}\n'
        f'{{"session_id":"{sid}","ts":1776000120,"text":"Now switch to paywall copy"}}\n'
    )
    session_path.write_text(
        json.dumps({
            "timestamp": "2026-05-12T08:00:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Please debug the onboarding flow",
            },
        }) + "\n"
    )
    # Pin the session file mtime below the state row's freshness so the
    # per-session components are deterministic in the gate math.
    os.utime(session_path, (1_776_000_000, 1_776_000_000))
    return sid, state_path, history_path, session_path


def _patch_codex_paths(monkeypatch, tmp_path, state_path, history_path):
    monkeypatch.setattr(codex_provider, "CODEX_STATE_DB", str(state_path))
    monkeypatch.setattr(codex_provider, "CODEX_HISTORY_PATH", str(history_path))
    monkeypatch.setattr(
        codex_provider, "CODEX_SESSIONS_DIR", str(tmp_path / "sessions")
    )
    codex_provider._CODEX_HISTORY_CACHE["mtime"] = None
    codex_provider._CODEX_HISTORY_CACHE["entries"] = {}
    codex_provider._CODEX_SESSION_TURNS_CACHE["entries"] = {}


def test_codex_gate_stubs_unchanged_sessions_without_parsing(monkeypatch, tmp_path):
    sid, state_path, history_path, session_path = _make_codex_corpus(tmp_path)
    _patch_codex_paths(monkeypatch, tmp_path, state_path, history_path)
    spec = get_provider("codex")

    baseline = spec.list_index_records()
    assert len(baseline) == 1
    stored_mtime = baseline[0]["mtime"]
    known = {str(session_path): (sid, stored_mtime)}

    parse_calls: list[str] = []
    real_meta = codex_provider._load_session_metadata

    def counting_meta(path):
        parse_calls.append(path)
        return real_meta(path)

    monkeypatch.setattr(codex_provider, "_load_session_metadata", counting_meta)

    records = spec.list_index_records(known_sessions=known)
    assert parse_calls == [], "unchanged session file must not be parsed"
    assert len(records) == 1
    stub = records[0]
    assert stub.get("unchanged") is True
    assert stub["path"] == str(session_path)
    assert stub["provider"] == "codex"
    assert abs(float(stub["mtime"]) - float(stored_mtime)) <= 0.001


def test_codex_gate_reparses_when_history_advances(monkeypatch, tmp_path):
    sid, state_path, history_path, session_path = _make_codex_corpus(tmp_path)
    _patch_codex_paths(monkeypatch, tmp_path, state_path, history_path)
    spec = get_provider("codex")

    baseline = spec.list_index_records()
    known = {str(session_path): (sid, baseline[0]["mtime"])}

    # A new history entry for this sid, newer than every stored component.
    with open(history_path, "a") as f:
        f.write(
            json.dumps({"session_id": sid, "ts": 1_776_000_999, "text": "one more thing"})
            + "\n"
        )
    codex_provider._CODEX_HISTORY_CACHE["mtime"] = None
    codex_provider._CODEX_HISTORY_CACHE["entries"] = {}

    records = spec.list_index_records(known_sessions=known)
    assert len(records) == 1
    assert records[0].get("unchanged") is None, "history advance must force a re-parse"
    assert records[0]["session_id"] == sid
    assert float(records[0]["mtime"]) > float(baseline[0]["mtime"])


def test_codex_gate_reparses_when_state_row_advances(monkeypatch, tmp_path):
    sid, state_path, history_path, session_path = _make_codex_corpus(tmp_path)
    _patch_codex_paths(monkeypatch, tmp_path, state_path, history_path)
    spec = get_provider("codex")

    baseline = spec.list_index_records()
    known = {str(session_path): (sid, baseline[0]["mtime"])}

    conn = sqlite3.connect(state_path)
    conn.execute(
        "UPDATE threads SET updated_at_ms = ?, updated_at = ? WHERE id = ?",
        (1_776_000_800_000, 1_776_000_800, sid),
    )
    conn.commit()
    conn.close()

    records = spec.list_index_records(known_sessions=known)
    assert len(records) == 1
    assert records[0].get("unchanged") is None, "state advance must force a re-parse"
    assert float(records[0]["mtime"]) > float(baseline[0]["mtime"])


def test_codex_gate_reparses_when_session_file_advances(monkeypatch, tmp_path):
    sid, state_path, history_path, session_path = _make_codex_corpus(tmp_path)
    _patch_codex_paths(monkeypatch, tmp_path, state_path, history_path)
    spec = get_provider("codex")

    baseline = spec.list_index_records()
    known = {str(session_path): (sid, baseline[0]["mtime"])}

    os.utime(session_path, (1_776_000_900, 1_776_000_900))
    codex_provider._CODEX_SESSION_TURNS_CACHE["entries"] = {}

    records = spec.list_index_records(known_sessions=known)
    assert len(records) == 1
    assert records[0].get("unchanged") is None, "session mtime advance must re-parse"
    assert float(records[0]["mtime"]) > float(baseline[0]["mtime"])
