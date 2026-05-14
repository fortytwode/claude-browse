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
    """No matter what's in the suffix, the row ends with hidden metadata."""
    info = _info(context="a\nb\nc\nd")
    row = format_row(info, query="anything")
    assert row.rstrip().endswith(
        f"abc-123{browse.ROW_META_SEP}/home/alice/proj{browse.ROW_META_SEP}claude"
    )


def test_format_row_shows_match_recency_and_thread_activity_when_query_active():
    info = _info(
        context="pokpok brief",
        timestamp="2026-05-01T10:00:00Z",
        last_timestamp="2026-05-10T10:00:00Z",
        match_timestamp="2026-05-02T10:00:00Z",
    )
    row = format_row(info, query="pokpok")
    assert "active" in row


def test_format_row_shows_feedback_reason_tag_for_descriptive_query():
    info = _info(
        context="Nevena feedback summary for the ClickUp task",
        match_intent_score=6.0,
    )
    row = format_row(info, query="where i was asking nevena about feedback")
    assert "feedback" in row


def test_format_row_shows_folder_match_for_single_anchor_workspace_query():
    info = _info(
        cwd="/Users/Shamanth/tiktoker",
        context="tiktoker repo work",
    )
    row = format_row(info, query="tiktoker")
    assert "folder match" in row


def test_format_row_shows_opening_match_when_anchor_is_in_first_message():
    info = _info(
        cwd="/Users/Shamanth/team-operations",
        name="Review Immutable audit and consolidate findings",
        first_msg="AppsFlyer Search Ads API check (Tiktoker context).",
        context="AppsFlyer Search Ads API check (Tiktoker context).",
    )
    row = format_row(info, query="tiktoker")
    assert "opening match" in row


def test_format_row_shows_mentioned_in_thread_when_anchor_is_only_later_context():
    info = _info(
        cwd="/Users/Shamanth/team-operations",
        name="Review Immutable audit and consolidate findings",
        first_msg="General weekly review",
        context="AppsFlyer Search Ads API check (Tiktoker context).",
    )
    row = format_row(info, query="tiktoker")
    assert "mentioned in thread" in row


def test_format_row_shows_critique_reason_tag_for_opportunity_query():
    info = _info(
        context="The opportunities feel forced and need better evidence",
        match_intent_score=5.0,
    )
    row = format_row(info, query="pokpok brief where we questioned the opportunities")
    assert "critique" in row


def test_format_row_shows_older_topic_tag_when_match_is_not_latest_activity():
    info = _info(
        context="pokpok brief",
        timestamp="2026-05-01T10:00:00Z",
        last_timestamp="2026-05-10T10:00:00Z",
        match_timestamp="2026-05-02T10:00:00Z",
    )
    row = format_row(info, query="pokpok")
    assert "older topic" in row


def test_format_row_prioritizes_match_context_when_query_active():
    info = _info(
        name="Weekly Creator Briefs for MaxRewards",
        context="Nevena feedback summary for Neil's performance",
    )
    row = format_row(info, query="nevena feedback")
    assert row.index("Nevena feedback summary") < row.index("Weekly Creator Briefs")


def test_format_row_allows_visible_triple_hash_without_breaking_metadata():
    info = _info(context="### Step 1. Export your ChatGPT history")
    row = format_row(info, query="chatgpt history")
    assert "### Step 1. Export your ChatGPT history" in row
    assert browse._split_row_metadata(row) == (
        row.split(browse.ROW_META_SEP)[0],
        "abc-123",
        "/home/alice/proj",
        "claude",
    )


def test_format_query_coach_row_suggests_anchor_summary_for_descriptive_query():
    row = browse.format_query_coach_row("last closeout session for Musopia")
    assert row is not None
    visible, sid, cwd, provider = browse._split_row_metadata(row)
    assert "Looking for: musopia + closeout" in visible
    assert sid == browse.COACH_SESSION_ID
    assert cwd == ""
    assert provider == browse.COACH_PROVIDER


def test_format_query_coach_row_shows_low_confidence_guidance():
    row = browse.format_query_coach_row("that we discussed, please?")
    assert row is not None
    visible, sid, _cwd, _provider = browse._split_row_metadata(row)
    assert "Add one anchor" in visible
    assert sid == browse.COACH_SESSION_ID


def test_render_query_coach_preview_explains_descriptive_query_interpretation():
    preview = browse.render_query_coach_preview(
        "where i was asking nevena about feedback"
    )
    assert "Looking for: nevena + feedback" in preview
    assert "Anchors: nevena, feedback" in preview
    assert "Sentence-style query detected." in preview


def test_render_query_coach_preview_handles_low_confidence_query():
    preview = browse.render_query_coach_preview("that we discussed, please?")
    assert "Add one concrete anchor" in preview
    assert "nevena feedback" in preview


def test_write_search_script_reads_query_from_fzf_env(tmp_path):
    script_path = tmp_path / "search.py"
    browse._write_search_script(
        str(script_path),
        "/tmp/test.db",
        "/tmp/pkg",
        None,
        25,
    )
    text = script_path.read_text()
    assert 'q = os.environ.get("FZF_QUERY", "")' in text
    assert 'sys.argv[1]' not in text


def test_write_preview_script_reads_query_from_fzf_env(tmp_path):
    script_path = tmp_path / "preview.py"
    browse._write_preview_script(
        str(script_path),
        "/tmp/test.db",
        "/tmp/pkg",
    )
    text = script_path.read_text()
    assert 'query = os.environ.get("FZF_QUERY", "")' in text
    assert 'sys.argv[2]' not in text


def test_build_fzf_cmd_avoids_raw_query_placeholder_in_shell_commands():
    cmd = browse._build_fzf_cmd(
        "Claude",
        "/tmp/search.py",
        "/tmp/preview.py",
        "/tmp/enter_guard.py",
    )
    bind = next(part for part in cmd if part.startswith("--bind=change:"))
    preview = next(part for part in cmd if part.startswith("--preview="))
    assert bind == (
        "--bind=change:execute-silent(python3 /tmp/enter_guard.py note-change)"
        "+reload(python3 /tmp/search.py)"
    )
    assert preview == "--preview=python3 /tmp/preview.py {}"
    assert "{q}" not in bind
    assert "{q}" not in preview


def test_build_fzf_cmd_keeps_enter_open_with_multiline_paste_guard():
    cmd = browse._build_fzf_cmd(
        "Claude",
        "/tmp/search.py",
        "/tmp/preview.py",
        "/tmp/enter_guard.py",
    )
    assert (
        "--bind=enter:transform(python3 /tmp/enter_guard.py maybe-accept)"
        in cmd
    )
    assert "--bind=ctrl-o:accept" in cmd


def test_write_enter_guard_script_records_changes_and_gates_accept(tmp_path):
    script_path = tmp_path / "guard.py"
    state_path = tmp_path / "guard_state.txt"
    browse._write_enter_guard_script(str(script_path), str(state_path))
    text = script_path.read_text()
    assert "THRESHOLD_MS = 250" in text
    assert 'if mode == "note-change":' in text
    assert 'if mode == "maybe-accept":' in text
    assert "print(\"accept\")" in text
    assert "change-header(" in text


def test_default_target_provider_follows_entrypoint_name():
    assert browse._default_target_provider("claude-browse") == "claude"
    assert browse._default_target_provider("/tmp/codex-browse") == "codex"
    assert browse._default_target_provider("/tmp/gemini-browse") == "gemini"
    assert browse._default_target_provider("/tmp/copilot-browse") == "copilot"
    assert browse._default_target_provider("/tmp/cursor-browse") == "cursor"


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


def test_main_empty_state_message_is_dynamic(monkeypatch, capsys):
    specs = {
        "claude": type(
            "Spec",
            (),
            {"has_local_state": lambda self: False, "display_name": "Claude", "binary": "claude"},
        )(),
        "copilot": type(
            "Spec",
            (),
            {"has_local_state": lambda self: False, "display_name": "Copilot", "binary": "copilot"},
        )(),
    }

    monkeypatch.setattr(browse, "_check_fzf", lambda: None)
    monkeypatch.setattr(
        browse,
        "provider_ids",
        lambda **kwargs: ("claude", "copilot"),
    )
    monkeypatch.setattr(browse, "get_provider", lambda provider: specs[provider])
    monkeypatch.setattr(browse.sys, "argv", ["claude-browse"])

    with pytest.raises(SystemExit) as excinfo:
        browse.main()

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "No local Claude or Copilot sessions found." in captured.out
    assert "Run `claude` or `copilot` at least once" in captured.out


def test_main_list_providers_prints_without_fzf(monkeypatch, capsys):
    entries = (
        type(
            "Entry",
            (),
            {
                "source_type": "builtin",
                "origin": "builtin",
                "spec": type(
                    "Spec",
                    (),
                    {
                        "provider_id": "claude",
                        "source_capable": True,
                        "target_capable": True,
                        "experimental": False,
                        "binary": "claude",
                        "is_available": lambda self: True,
                        "auth_status": lambda self: None,
                    },
                )(),
            },
        )(),
        type(
            "Entry",
            (),
            {
                "source_type": "file",
                "origin": "file:/tmp/mystery_provider.py",
                "spec": type(
                    "Spec",
                    (),
                    {
                        "provider_id": "mystery",
                        "source_capable": False,
                        "target_capable": True,
                        "experimental": True,
                        "binary": "mystery",
                        "is_available": lambda self: False,
                        "auth_status": lambda self: "signed-out",
                    },
                )(),
            },
        )(),
    )

    monkeypatch.setattr(
        browse,
        "provider_ids",
        lambda **kwargs: ("claude", "codex", "gemini", "copilot", "cursor"),
    )
    monkeypatch.setattr(browse, "provider_entries", lambda **kwargs: entries)
    monkeypatch.setattr(
        browse,
        "_check_fzf",
        lambda: (_ for _ in ()).throw(AssertionError("fzf should not be required")),
    )
    monkeypatch.setattr(browse.sys, "argv", ["claude-browse", "--list-providers"])

    browse.main()

    captured = capsys.readouterr()
    assert "provider" in captured.out
    assert "claude" in captured.out
    assert "mystery" in captured.out
    assert "origin: file:/tmp/mystery_provider.py" in captured.out


def test_parse_fzf_output_handles_print_query_safe_marker():
    row = (
        f"match {browse.ROW_META_SEP}abc-123{browse.ROW_META_SEP}"
        f"/home/alice/proj{browse.ROW_META_SEP}claude"
    )
    parsed = browse._parse_fzf_output(
        f"pokpok\nSAFE:\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", "open_safe", "pokpok")


def test_parse_fzf_output_handles_print_query_default_accept():
    row = (
        f"match {browse.ROW_META_SEP}abc-123{browse.ROW_META_SEP}"
        f"/home/alice/proj{browse.ROW_META_SEP}codex"
    )
    parsed = browse._parse_fzf_output(
        f"claude browse\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", "open_yolo", "claude browse")


def test_parse_fzf_output_handles_print_query_prompt_marker():
    row = (
        f"match {browse.ROW_META_SEP}abc-123{browse.ROW_META_SEP}"
        f"/home/alice/proj{browse.ROW_META_SEP}claude"
    )
    parsed = browse._parse_fzf_output(
        f"pokpok\nPROMPT:\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", "print_prompt", "pokpok")


def test_parse_fzf_output_handles_print_query_topic_marker():
    row = (
        f"match {browse.ROW_META_SEP}abc-123{browse.ROW_META_SEP}"
        f"/home/alice/proj{browse.ROW_META_SEP}claude"
    )
    parsed = browse._parse_fzf_output(
        f"pokpok\nTOPIC:\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", "reenter_topic", "pokpok")


def test_parse_fzf_output_handles_print_query_brief_marker():
    row = (
        f"match {browse.ROW_META_SEP}abc-123{browse.ROW_META_SEP}"
        f"/home/alice/proj{browse.ROW_META_SEP}claude"
    )
    parsed = browse._parse_fzf_output(
        f"pokpok\nBRIEF:\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", "print_brief", "pokpok")


def test_parse_fzf_output_handles_print_query_handoff_marker():
    row = (
        f"match {browse.ROW_META_SEP}abc-123{browse.ROW_META_SEP}"
        f"/home/alice/proj{browse.ROW_META_SEP}claude"
    )
    parsed = browse._parse_fzf_output(
        f"pokpok\nHANDOFF:\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", "print_handoff", "pokpok")


def test_parse_fzf_output_handles_print_query_status_marker():
    row = (
        f"match {browse.ROW_META_SEP}abc-123{browse.ROW_META_SEP}"
        f"/home/alice/proj{browse.ROW_META_SEP}claude"
    )
    parsed = browse._parse_fzf_output(
        f"pokpok\nSTATUS:\n{row}\n",
        "claude",
    )
    assert parsed == (row, "claude", "print_status", "pokpok")


def test_parse_fzf_output_ignores_visible_triple_hash_in_snippet():
    row = (
        "Apr 23   claude team-operations "
        "→ ### Step 1. Export your ChatGPT history"
        f"{browse.ROW_META_SEP}abc-123{browse.ROW_META_SEP}"
        f"/home/alice/proj{browse.ROW_META_SEP}claude"
    )
    parsed = browse._parse_fzf_output(
        f"last session from Musopia?\n{row}\n",
        "codex",
    )
    assert parsed == (row, "codex", "open_yolo", "last session from Musopia?")


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
        lambda *args, **kwargs: captured.append(("handoff", args, kwargs)),
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
        lambda *args, **kwargs: captured.append(("handoff", args, kwargs)),
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


def test_open_in_target_provider_reenter_topic_uses_handoff_even_when_source_matches(
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
        lambda *args, **kwargs: captured.append(("handoff", args, kwargs)),
    )

    browse._open_in_target_provider(
        session,
        "claude",
        "claude",
        "abc-123",
        "/home/alice/proj",
        (),
        True,
        "pokpok",
        reenter_topic=True,
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
        lambda _session, target_provider, selection_query="", reenter_topic=False: (
            "/tmp/claude_browse_import.md"
            if target_provider == "gemini" and not reenter_topic
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


def test_continue_in_provider_reenter_topic_updates_prompt(monkeypatch):
    session = _info(path="/tmp/session.jsonl")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="", reenter_topic=False: (
            "/tmp/claude_browse_import.md"
            if target_provider == "gemini" and reenter_topic
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
            "pokpok",
            reenter_topic=True,
        )

    assert "re-enter the earlier matched topic" in captured["cmd"][-1]


def test_continue_in_provider_from_gemini_execs_claude_with_add_dir(
    monkeypatch,
):
    session = _info(provider="gemini", path="/tmp/gemini-session.json")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="", reenter_topic=False: (
            "/tmp/codex_browse_import.md"
            if target_provider == "claude" and not reenter_topic
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
        "--",
        (
            "Continue the imported Gemini session context from "
            "/tmp/codex_browse_import.md. Treat it as prior conversation "
            "state, read that file first, use the Reopen Intent section as "
            "the reason this thread was selected, prioritize the "
            "end-of-thread state and most recent turns over the original "
            "opening prompt, then continue the work in this directory."
        ),
    ]


def test_continue_in_provider_from_claude_execs_cursor_with_inline_context(
    monkeypatch,
):
    session = _info(path="/tmp/session.jsonl")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "build_import_markdown",
        lambda _session, _target_provider, _selection_query="", reenter_topic=False: (
            "# Imported Session Context\n\n## Reopen Intent\n\n- pokpok\n"
        ),
    )
    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("should not write temp file")
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
            "cursor",
            "/home/alice/proj",
            (),
            True,
            "pokpok",
        )

    assert captured["binary"] == "cursor-agent"
    assert captured["cmd"][0:2] == ["cursor-agent", "--force"]
    assert "# Imported Session Context" in captured["cmd"][2]
    assert "Continue the imported Claude session context below." in captured["cmd"][2]


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
