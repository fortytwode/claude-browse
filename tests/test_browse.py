"""Tests for claude_browse.browse: row formatting for fzf.

The browse module is mostly fzf integration (hard to unit-test), but
format_row is a pure function and worth pinning — it has historically been
the source of subtle bugs where embedded control characters split a logical
row into multiple visual rows in the picker.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys

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
        f"{browse.ROW_META_SEP}mentioned later{browse.ROW_META_SEP}{browse.ROW_META_SEP}medium"
        f"{browse.ROW_META_SEP}"
        f"{browse.ROW_META_SEP}anything"
    )


def test_format_row_shows_match_recency_and_thread_activity_when_query_active():
    info = _info(
        context="pokpok brief",
        timestamp="2026-05-01T10:00:00Z",
        last_timestamp="2026-05-10T10:00:00Z",
        match_timestamp="2026-05-02T10:00:00Z",
    )
    row = format_row(info, query="pokpok")
    assert row.startswith("May 10")
    assert "matched May 02" in row


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


def test_format_row_labels_phrase_fallback_results():
    info = _info(
        context="I built the MaxRewards list for Morgan.",
        phrase_fallback=True,
    )

    row = format_row(info, query='"MaxRewards built me a list"')

    assert "near phrase" in row


def test_format_row_labels_prefix_fallback_results():
    info = _info(
        context="Ayan Kartik Tanushree assignment review.",
        prefix_fallback=True,
    )

    row = format_row(info, query="ayan kar")

    assert "prefix match" in row


def test_format_row_labels_exact_phrase_context_results():
    info = _info(
        context='The call said "\x01Guitar Hero for chess\x02" and named the skill floor.',
        timestamp="2026-06-21T10:00:00Z",
        last_timestamp="2026-06-21T10:00:00Z",
    )

    row = format_row(info, query='"Guitar Hero for chess"')

    assert "exact phrase" in row
    assert "high" in row


def test_format_row_labels_unquoted_contiguous_phrase_context_results():
    info = _info(
        context='The call said "\x01Guitar Hero for chess\x02" and named the skill floor.',
        timestamp="2026-06-21T10:00:00Z",
        last_timestamp="2026-06-21T10:00:00Z",
    )

    row = format_row(info, query="Guitar Hero for chess")

    assert "exact phrase" in row
    assert "high" in row


def test_format_row_lowers_exact_phrase_confidence_when_thread_moved_on():
    info = _info(
        context='The call said "\x01Guitar Hero for chess\x02" and named the skill floor.',
        timestamp="2026-06-21T10:00:00Z",
        last_timestamp="2026-06-22T10:00:00Z",
        match_timestamp="2026-06-21T10:05:00Z",
    )

    row = format_row(info, query='"Guitar Hero for chess"')

    assert "exact phrase" in row
    assert "medium" in row


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
    assert "mentioned later" in row


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
    assert "drifted" in row


def test_format_row_shows_primary_subject_for_descriptive_query():
    info = _info(
        context="The opportunities feel forced and need better evidence",
        first_msg="Can you look at the Sherlock output and review this brief?",
        match_term_count=4,
        _quality_score=7.0,
        _metadata_anchor_score=5.0,
    )
    row = format_row(info, query="pokpok brief where we questioned the opportunities")
    assert "primary subject" in row


def test_format_row_prioritizes_match_context_when_query_active():
    info = _info(
        name="Weekly Creator Briefs for MaxRewards",
        context="Nevena feedback summary for Neil's performance",
    )
    row = format_row(info, query="nevena feedback")
    assert row.index("Nevena feedback summary") < row.index("Weekly Creator Briefs")


def test_format_row_trims_snippet_lead_so_match_term_is_visible():
    # FTS5 centers the snippet on the match; the leading filler pushes the
    # highlighted term off-screen in a narrow pane. Trim it so the term leads.
    info = _info(context="…or reporting. Let me verify the \x01Bible\x02 assignment")
    row = format_row(info, query="bible")
    visible = re.sub(r"\x1b\[[0-9;]*m", "", browse._split_row_metadata(row)[0])
    assert "…Bible assignment" in visible
    # The leading filler is gone: nothing of "or reporting" survives the trim.
    assert "or reporting" not in visible


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


def test_decode_row_metadata_exposes_match_provenance_fields():
    row = browse._encode_row_metadata(
        "visible",
        "abc-123",
        "/tmp/proj",
        "claude",
        match_label="primary subject",
        match_timestamp="2026-05-12T10:00:00Z",
        match_confidence="high",
    )
    meta = browse._decode_row_metadata(row)
    assert meta == {
        "visible": "visible",
        "session_id": "abc-123",
        "cwd": "/tmp/proj",
        "provider": "claude",
        "match_label": "primary subject",
        "match_timestamp": "2026-05-12T10:00:00Z",
        "match_confidence": "high",
        "match_segment_idx": "",
        "query": "",
    }


def test_format_row_carries_query_for_preview_context():
    row = format_row(
        _info(context='call said "\x01Guitar Hero for chess\x02"'),
        query='"Guitar Hero for chess"',
    )
    meta = browse._decode_row_metadata(row)
    assert meta["query"] == '"Guitar Hero for chess"'


def test_format_row_carries_match_segment_idx_for_preview_context():
    row = format_row(
        _info(
            context='call said "\x01Guitar Hero for chess\x02"',
            match_segment_idx=7,
        ),
        query='"Guitar Hero for chess"',
    )
    meta = browse._decode_row_metadata(row)
    assert meta["match_segment_idx"] == "7"


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
    assert "Phrase boost: nevena feedback" in preview
    assert "Sentence-style query detected." in preview


def test_render_query_coach_preview_shows_exact_phrase_boost():
    preview = browse.render_query_coach_preview("cfo update")

    assert "Anchors: cfo, update" in preview
    assert "Exact phrase boost: cfo update" in preview


def test_render_query_coach_preview_shows_phrase_no_hit_fallback():
    preview = browse.render_query_coach_preview('"MaxRewards built me a list"')

    assert "Exact phrase boost: maxrewards built me a list" in preview
    assert "No-hit fallback: maxrewards + list" in preview


def test_render_query_coach_preview_handles_low_confidence_query():
    preview = browse.render_query_coach_preview("that we discussed, please?")
    assert "Add one concrete anchor" in preview
    assert "teammate feedback" in preview


def test_write_search_script_reads_query_from_fzf_env(tmp_path):
    script_path = tmp_path / "search.py"
    browse._write_search_script(
        str(script_path),
        "/tmp/test.db",
        "/tmp/pkg",
        None,
        "/tmp/current",
        25,
    )
    text = script_path.read_text()
    assert 'q = os.environ.get("FZF_QUERY", "")' in text
    assert "CURRENT_CWD = '/tmp/current'" in text
    assert "conn = fts.open_db(DB_PATH, read_only=True)" in text
    assert "current_cwd=CURRENT_CWD" in text
    assert "search_log.log_search(" in text
    assert "elapsed_ms=(time.perf_counter() - start) * 1000" in text
    assert 'sys.argv[1]' not in text
    # Empty-query reload must also float current-folder threads, matching the
    # initial paint (folder-first must not vanish after type-then-clear).
    assert "_folder_first_order" in text
    # No --here filter: the cwd-scoped branch must not leak into this variant.
    assert "CWD_FILTER = None" in text


def test_write_search_script_here_mode_uses_sessions_for_cwd(tmp_path):
    """--here's empty-query reload fetches folder sessions directly (not a
    post-filter of the global slice) and its typed-query filter is
    boundary-aware so /w/app doesn't match /w/app-legacy."""
    script_path = tmp_path / "search_here.py"
    browse._write_search_script(
        str(script_path),
        "/tmp/test.db",
        "/tmp/pkg",
        "/w/app",
        "/w/app",
        25,
    )
    text = script_path.read_text()
    assert "CWD_FILTER = '/w/app'" in text
    assert "fts.sessions_for_cwd(conn, CWD_FILTER" in text
    assert 'startswith(_base + "/")' in text
    # The rendered script must be valid Python -- a template typo would
    # otherwise only surface as a silently broken picker at runtime.
    compile(text, str(script_path), "exec")


def test_main_web_mode_dispatches_to_run_server(monkeypatch, tmp_path):
    """--web skips fzf entirely and forwards cwd, prefixes, --here, --all."""
    calls = []
    monkeypatch.setattr(
        "claude_browse.web.run_server",
        lambda cwd, prefixes=(), cwd_filter=None, limit=100: calls.append(
            (cwd, prefixes, cwd_filter, limit)
        ),
    )
    monkeypatch.setattr(
        browse,
        "_check_fzf",
        lambda: (_ for _ in ()).throw(AssertionError("--web must not require fzf")),
    )
    monkeypatch.setattr(browse, "_providers_with_local_state", lambda: ["claude"])
    monkeypatch.setattr(browse, "_folder_prefixes", lambda: ())

    class Conn:
        def execute(self, *a, **k):
            class Cur:
                def fetchone(self):
                    return (1,)

            return Cur()

        def close(self):
            pass

    monkeypatch.setattr(browse.fts, "open_db", lambda *a, **k: Conn())
    monkeypatch.setattr(browse, "_spawn_background_index_refresh", lambda: None)
    monkeypatch.setattr(browse.sys, "argv", ["claude-browse", "--web", "--all"])

    browse.main()

    assert len(calls) == 1
    cwd, _prefixes, cwd_filter, limit = calls[0]
    assert cwd == os.getcwd()
    assert cwd_filter is None
    assert limit == 999  # --all must widen the web sidebar too


def test_write_preview_script_reads_query_from_row_metadata_with_fzf_env_fallback(tmp_path):
    script_path = tmp_path / "preview.py"
    browse._write_preview_script(
        str(script_path),
        "/tmp/test.db",
        "/tmp/pkg",
    )
    text = script_path.read_text()
    assert '(row_meta or {}).get("query", "")' in text
    assert 'or os.environ.get("FZF_QUERY", "")' in text
    assert "conn = fts.open_db(DB_PATH, read_only=True)" in text
    assert '"match_segment_idx": row_meta.get("match_segment_idx", "")' in text
    assert "query.strip().strip" in text
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
    assert "PASTE_JUMP_CHARS = 16" in text
    assert "PASTE_GUARD_MS = 1500" in text
    assert 'if mode == "note-change":' in text
    assert 'if mode == "maybe-accept":' in text
    assert "print(\"accept\")" in text
    assert "change-header(" in text


def test_enter_guard_detects_large_paste_and_blocks_immediate_accept(tmp_path):
    script_path = tmp_path / "guard.py"
    state_path = tmp_path / "guard_state.txt"
    browse._write_enter_guard_script(str(script_path), str(state_path))

    env = dict(os.environ, FZF_QUERY="Pricing should feel modular and frugal: likely $4K-$6K depending on mix. Keep the big retainer")
    subprocess.run(
        [sys.executable, str(script_path), "note-change"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    state = json.loads(state_path.read_text())
    assert state["last_query"].startswith("Pricing should feel modular and frugal")
    assert state["paste_guard_until_ms"] > state["last_change_ms"]

    first_result = subprocess.run(
        [sys.executable, str(script_path), "maybe-accept"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert first_result.stdout.startswith(
        "ignore+change-header(Pasted query detected."
    )

    state = json.loads(state_path.read_text())
    assert state["guard_pending_query"].startswith(
        "Pricing should feel modular and frugal"
    )

    second_result = subprocess.run(
        [sys.executable, str(script_path), "maybe-accept"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert second_result.stdout.strip() == "accept"


def test_enter_guard_accepts_when_query_is_stable_and_not_paste_guarded(tmp_path):
    script_path = tmp_path / "guard.py"
    state_path = tmp_path / "guard_state.txt"
    browse._write_enter_guard_script(str(script_path), str(state_path))
    state_path.write_text(
        json.dumps(
            {
                "last_change_ms": 0,
                "last_query": "doug",
                "paste_guard_until_ms": 0,
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(script_path), "maybe-accept"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "accept"


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


def test_native_resume_forks_with_codexmobile_on_mobile_ssh(monkeypatch, tmp_path):
    class Tty:
        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            return None

    codexmobile = tmp_path / "codexmobile"
    codexmobile.write_text("#!/bin/sh\n", encoding="utf-8")
    codexmobile.chmod(0o755)
    captured: dict[str, object] = {}

    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    monkeypatch.delenv("CODEX_BROWSE_MOBILE_DISABLE", raising=False)
    monkeypatch.setattr(browse.sys, "stdin", Tty())
    monkeypatch.setattr(browse.sys, "stdout", Tty())
    monkeypatch.setattr(browse, "_codex_mobile_binary", lambda: str(codexmobile))

    def fake_execvp(binary: str, cmd: list[str]) -> None:
        captured["binary"] = binary
        captured["cmd"] = cmd
        raise SystemExit(0)

    monkeypatch.setattr(browse.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit):
        browse._native_resume(_info(provider="codex"), "codex", "abc-123", "/tmp/proj", (), True)

    assert captured["binary"] == str(codexmobile)
    assert captured["cmd"] == [str(codexmobile), "--yolo", "fork", "abc-123"]


def test_native_resume_keeps_codex_native_when_mobile_disabled(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    monkeypatch.setenv("CODEX_BROWSE_MOBILE_DISABLE", "1")
    monkeypatch.setattr(browse.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def fake_execvp(binary: str, cmd: list[str]) -> None:
        captured["binary"] = binary
        captured["cmd"] = cmd
        raise SystemExit(0)

    monkeypatch.setattr(browse.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit):
        browse._native_resume(_info(provider="codex"), "codex", "abc-123", "/tmp/proj", (), True)

    assert captured["binary"] == "codex"
    assert captured["cmd"] == [
        "codex",
        "fork",
        "abc-123",
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_open_in_target_provider_default_uses_native_resume(monkeypatch):
    # Same source/target, no relocate: must take the native --resume path,
    # which is bound to the thread's original project directory.
    calls: dict[str, bool] = {"native": False, "handoff": False}
    monkeypatch.setattr(
        browse, "_native_resume", lambda *a, **k: calls.__setitem__("native", True)
    )
    monkeypatch.setattr(
        browse, "_continue_in_provider", lambda *a, **k: calls.__setitem__("handoff", True)
    )

    browse._open_in_target_provider(
        _info(), "claude", "claude", "abc-123", "/home/alice/proj", (), True
    )

    assert calls == {"native": True, "handoff": False}


def test_open_in_target_provider_relocate_forces_handoff(monkeypatch):
    # With relocate=True, even a same-vendor resume must route through the
    # handoff path (fresh session in the current dir) because native
    # `claude --resume <id>` cannot find a session outside its origin folder.
    calls: dict[str, bool] = {"native": False, "handoff": False}
    monkeypatch.setattr(
        browse, "_native_resume", lambda *a, **k: calls.__setitem__("native", True)
    )
    monkeypatch.setattr(
        browse, "_continue_in_provider", lambda *a, **k: calls.__setitem__("handoff", True)
    )

    browse._open_in_target_provider(
        _info(), "claude", "claude", "abc-123", "/home/alice/proj", (), True,
        relocate=True,
    )

    assert calls == {"native": False, "handoff": True}


def test_should_auto_relocate_same_folder_resumes_natively():
    # Launched from the thread's own folder: native resume, no relocate.
    assert browse._should_auto_relocate("/work/proj", "/work/proj") is False


def test_should_auto_relocate_cross_folder_relocates():
    # Launched somewhere else than the thread's origin: relocate here.
    assert browse._should_auto_relocate("/work/proj", "/work/other") is True


def test_should_auto_relocate_missing_origin_relocates():
    # No known origin folder (or pruned): nothing to chdir back to, relocate.
    assert browse._should_auto_relocate(None, "/work/here") is True
    assert browse._should_auto_relocate("", "/work/here") is True


def test_should_auto_relocate_casing_split_counts_as_same_folder(monkeypatch):
    # /Users vs /users for the same user canonicalizes to one path, so a
    # casing-only difference must NOT trigger a relocate.
    monkeypatch.setenv("USER", "Shamanth")
    assert (
        browse._should_auto_relocate(
            "/Users/Shamanth/team-operations",
            "/users/shamanth/team-operations",
        )
        is False
    )


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


def test_main_does_not_rebuild_when_first_indexing_is_locked(monkeypatch, capsys):
    class CountCursor:
        def fetchone(self):
            return (0,)

    class Conn:
        def execute(self, _sql):
            return CountCursor()

        def close(self):
            return None

    monkeypatch.setattr(browse, "_check_fzf", lambda: None)
    monkeypatch.setattr(browse, "_providers_with_local_state", lambda: ["claude"])
    monkeypatch.setattr(browse.fts, "open_db", lambda *args, **kwargs: Conn())
    monkeypatch.setattr(
        browse.fts,
        "reindex",
        lambda _conn, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )
    monkeypatch.setattr(
        browse.fts,
        "reset_db",
        lambda: (_ for _ in ()).throw(AssertionError("must not reset on lock")),
    )
    monkeypatch.setattr(browse.sys, "argv", ["claude-browse"])

    with pytest.raises(SystemExit) as excinfo:
        browse.main()

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "Indexing sessions for the first time..." in captured.err
    assert "Search index is locked by another process" in captured.err
    assert "Search index corrupted" not in captured.err


def test_main_warm_start_paints_immediately_and_refreshes_in_background(
    monkeypatch, capsys
):
    """A warm launch paints immediately and refreshes in the background."""

    class CountCursor:
        def fetchone(self):
            return (1,)

    class Conn:
        def execute(self, _sql):
            return CountCursor()

        def close(self):
            return None

    spawned = []
    monkeypatch.setattr(browse, "_check_fzf", lambda: None)
    monkeypatch.setattr(browse, "_providers_with_local_state", lambda: ["claude"])
    monkeypatch.setattr(browse, "_folder_prefixes", lambda: [])
    monkeypatch.setattr(browse.fts, "open_db", lambda *args, **kwargs: Conn())
    monkeypatch.setattr(
        browse.fts,
        "reindex",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("warm start must not reindex inline")
        ),
    )
    monkeypatch.setattr(
        browse, "_spawn_background_index_refresh", lambda: spawned.append(1)
    )
    monkeypatch.setattr(
        browse.fts, "list_recent", lambda _conn, limit, cwd=None: [_info()]
    )
    monkeypatch.setattr(
        browse.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 1, "stdout": ""})(),
    )
    monkeypatch.setattr(browse.sys, "argv", ["claude-browse"])

    with pytest.raises(SystemExit) as excinfo:
        browse.main()

    assert excinfo.value.code == 0
    assert spawned == [1]


def test_background_refresh_child_imports_from_any_cwd(monkeypatch, tmp_path):
    """The detached refresh child is a fresh interpreter: without an
    explicit sys.path fix-up it dies on a silent ModuleNotFoundError
    whenever claude_browse is not pip-installed (the git-clone shim
    case), freezing the index at its last refresh date. Observed live:
    two machines stuck showing nothing newer than the day this child
    shipped."""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return None

    monkeypatch.setattr(browse.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(browse.fts, "DB_PATH", str(tmp_path / "index.db"))
    browse._spawn_background_index_refresh()
    # browse.subprocess IS the subprocess module: undo the Popen patch so
    # the probe's subprocess.run below uses the real one.
    monkeypatch.undo()

    code = captured["cmd"][2]
    pkg_root = os.path.dirname(
        os.path.dirname(os.path.abspath(browse.__file__))
    )
    assert "sys.path.insert" in code
    assert pkg_root in code
    # The import must resolve from an unrelated cwd. Probe with the
    # refresh call swapped for a print so the test never touches a real
    # index database.
    probe = code.replace("_refresh_index_once()", "print('ok')")
    assert probe != code
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout.strip() == "ok", result.stderr


def test_refresh_index_once_heals_corruption_via_locked_rebuild(monkeypatch):
    class Conn:
        def close(self):
            return None

    rebuilt = []
    monkeypatch.setattr(browse.fts, "open_db", lambda *args, **kwargs: Conn())
    monkeypatch.setattr(
        browse.fts,
        "reindex",
        lambda *_a, **_k: (_ for _ in ()).throw(
            sqlite3.DatabaseError("malformed database schema")
        ),
    )
    monkeypatch.setattr(
        browse.fts,
        "rebuild_from_scratch",
        lambda: rebuilt.append(1) or (Conn(), (3, 0, 0)),
    )

    browse._refresh_index_once()

    assert rebuilt == [1]


def test_refresh_index_once_spares_healthy_index_on_app_error(monkeypatch):
    """An IntegrityError on a file that passes integrity_check is an
    indexing bug, not corruption: rebuilding would destroy a good index
    to mask it. The refresh must leave the index alone."""

    class Conn:
        def close(self):
            return None

    rebuilt = []
    monkeypatch.setattr(browse.fts, "open_db", lambda *args, **kwargs: Conn())
    monkeypatch.setattr(
        browse.fts,
        "reindex",
        lambda *_a, **_k: (_ for _ in ()).throw(
            sqlite3.IntegrityError(
                "UNIQUE constraint failed: semantic_terms.term"
            )
        ),
    )
    monkeypatch.setattr(browse.fts, "integrity_ok", lambda *a, **k: True)
    monkeypatch.setattr(
        browse.fts,
        "rebuild_from_scratch",
        lambda: rebuilt.append(1) or (Conn(), (3, 0, 0)),
    )

    browse._refresh_index_once()

    assert rebuilt == []


def test_refresh_index_once_rebuilds_when_file_fails_integrity(monkeypatch):
    """The live-observed shape: page corruption surfacing as an
    IntegrityError. The exception text proves nothing, but the file
    fails integrity_check -- rebuild."""

    class Conn:
        def close(self):
            return None

    rebuilt = []
    monkeypatch.setattr(browse.fts, "open_db", lambda *args, **kwargs: Conn())
    monkeypatch.setattr(
        browse.fts,
        "reindex",
        lambda *_a, **_k: (_ for _ in ()).throw(
            sqlite3.IntegrityError(
                "UNIQUE constraint failed: semantic_terms.term"
            )
        ),
    )
    monkeypatch.setattr(browse.fts, "integrity_ok", lambda *a, **k: False)
    monkeypatch.setattr(
        browse.fts,
        "rebuild_from_scratch",
        lambda: rebuilt.append(1) or (Conn(), (3, 0, 0)),
    )

    browse._refresh_index_once()

    assert rebuilt == [1]


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
        lambda *args, **kwargs: captured.append(("native", args)),
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
        lambda *args, **kwargs: captured.append(("native", args)),
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
        lambda *args, **kwargs: captured.append(("native", args)),
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
        lambda _session, target_provider, selection_query="", reenter_topic=False, relocate=False: (
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
        lambda _session, target_provider, selection_query="", reenter_topic=False, relocate=False: (
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
        lambda _session, target_provider, selection_query="", reenter_topic=False, relocate=False: (
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
        lambda _session, _target_provider, _selection_query="", reenter_topic=False, relocate=False: (
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


def test_continue_in_provider_to_codex_uses_codexmobile_on_mobile_ssh(
    monkeypatch,
    tmp_path,
):
    class Tty:
        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            return None

    session = _info(path="/tmp/session.jsonl")
    codexmobile = tmp_path / "codexmobile"
    codexmobile.write_text("#!/bin/sh\n", encoding="utf-8")
    codexmobile.chmod(0o755)
    captured: dict[str, object] = {}

    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    monkeypatch.delenv("CODEX_BROWSE_MOBILE_DISABLE", raising=False)
    monkeypatch.setattr(browse.sys, "stdin", Tty())
    monkeypatch.setattr(browse.sys, "stdout", Tty())
    monkeypatch.setattr(browse, "_codex_mobile_binary", lambda: str(codexmobile))
    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="", reenter_topic=False, relocate=False: (
            "/tmp/claude_browse_import.md"
        ),
    )

    def fake_execvp(binary: str, cmd: list[str]) -> None:
        captured["binary"] = binary
        captured["cmd"] = cmd
        raise SystemExit(0)

    monkeypatch.setattr(browse.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit):
        browse._continue_in_provider(
            session,
            "claude",
            "codex",
            "/home/alice/proj",
            (),
            True,
            "",
        )

    assert captured["binary"] == str(codexmobile)
    assert captured["cmd"][:3] == [str(codexmobile), "--yolo", "start"]
    assert "Continue the imported Claude session context from" in captured["cmd"][3]


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


class TestFormatThreadSpan:
    """`_format_thread_span` surfaces a thread's true start when the list
    column (last-activity) makes an old, resumed thread read as recent."""

    def test_shows_banner_on_meaningful_drift(self):
        line = browse._format_thread_span(
            "2026-04-16T17:09:30Z", "2026-05-27T07:44:19Z"
        )
        assert line == "Began Apr 16, 2026 (40d before last activity, May 27)"

    def test_no_banner_same_day(self):
        assert (
            browse._format_thread_span(
                "2026-06-04T09:00:00Z", "2026-06-04T18:00:00Z"
            )
            == ""
        )

    def test_no_banner_under_threshold(self):
        assert (
            browse._format_thread_span(
                "2026-05-20T09:00:00Z", "2026-05-21T10:00:00Z"
            )
            == ""
        )

    def test_empty_on_missing_timestamp(self):
        assert browse._format_thread_span(None, "2026-05-27T07:44:19Z") == ""
        assert browse._format_thread_span("2026-04-16T17:09:30Z", None) == ""

    def test_empty_on_malformed_timestamp(self):
        assert browse._format_thread_span("not-a-date", "also-bad") == ""


def test_continue_in_provider_relocate_adds_transcript_dir_not_cwd(monkeypatch):
    # Same-provider relocate must hand off (not native resume) and grant read
    # access to the transcript's directory, never the source/project cwd.
    session = _info(
        provider="claude",
        path="/home/alice/.claude/projects/-home-alice-proj/abc-123.jsonl",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="", reenter_topic=False, relocate=False: (
            "/tmp/claude_browse_import.md"
        ),
    )
    monkeypatch.setattr(browse.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def fake_execvp(binary: str, cmd: list[str]) -> None:
        captured["cmd"] = cmd
        raise SystemExit(0)

    monkeypatch.setattr(browse.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit):
        browse._continue_in_provider(
            session,
            "claude",
            "claude",
            "/home/alice/proj",
            (),
            True,
            "",
            relocate=True,
        )

    cmd = captured["cmd"]
    # Transcript directory is granted as an --add-dir...
    assert "/home/alice/.claude/projects/-home-alice-proj" in cmd
    # ...but the source/project folder is NOT.
    assert "/home/alice/proj" not in cmd


def test_continue_in_provider_relocate_without_path_adds_no_extra_dir(monkeypatch):
    # Routing-layer guard: relocate=True but the session has no transcript path
    # -> no transcript --add-dir, and never the source cwd.
    session = _info(provider="claude")
    session.pop("path", None)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="", reenter_topic=False, relocate=False: (
            "/tmp/claude_browse_import.md"
        ),
    )
    monkeypatch.setattr(browse.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def fake_execvp(binary: str, cmd: list[str]) -> None:
        captured["cmd"] = cmd
        raise SystemExit(0)

    monkeypatch.setattr(browse.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit):
        browse._continue_in_provider(
            session, "claude", "claude", "/home/alice/proj", (), True, "", relocate=True
        )

    cmd = captured["cmd"]
    # Only the import dir is added; no source cwd leaks in.
    assert cmd.count("--add-dir") == 1
    assert "/home/alice/proj" not in cmd


def test_continue_in_provider_no_relocate_adds_no_transcript_dir(monkeypatch):
    session = _info(
        provider="claude",
        path="/home/alice/.claude/projects/-home-alice-proj/abc-123.jsonl",
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        browse,
        "write_import_file",
        lambda _session, target_provider, selection_query="", reenter_topic=False, relocate=False: (
            "/tmp/claude_browse_import.md"
        ),
    )
    monkeypatch.setattr(browse.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    def fake_execvp(binary: str, cmd: list[str]) -> None:
        captured["cmd"] = cmd
        raise SystemExit(0)

    monkeypatch.setattr(browse.os, "execvp", fake_execvp)

    with pytest.raises(SystemExit):
        browse._continue_in_provider(
            session, "claude", "gemini", "/home/alice/proj", (), True, ""
        )

    # Only the import dir is added; no transcript projects dir leaks in.
    assert "/home/alice/.claude/projects/-home-alice-proj" not in captured["cmd"]


# --- folder-first default ordering (U3) -------------------------------------


def test_folder_first_order_floats_current_folder_and_subdirs():
    sessions = [
        {"cwd": "/w/other", "session_id": "o1"},
        {"cwd": "/w/family", "session_id": "f1"},
        {"cwd": "/w/family/sub", "session_id": "f2"},
        {"cwd": "/w/other2", "session_id": "o2"},
    ]
    out = browse._folder_first_order(sessions, "/w/family")
    ids = [s["session_id"] for s in out]
    # family + family/sub float up, preserving their input (recency) order.
    assert ids[:2] == ["f1", "f2"]
    # everything else follows, also order-preserved.
    assert ids[2:] == ["o1", "o2"]


def test_folder_first_order_preserves_all_sessions():
    sessions = [
        {"cwd": "/w/family", "session_id": "f1"},
        {"cwd": "/w/other", "session_id": "o1"},
    ]
    out = browse._folder_first_order(sessions, "/w/family")
    assert len(out) == len(sessions)
    assert {s["session_id"] for s in out} == {"f1", "o1"}


def test_folder_first_order_sibling_prefix_does_not_match():
    # "/w/family-archive" must NOT be treated as under "/w/family".
    sessions = [
        {"cwd": "/w/family-archive", "session_id": "arch"},
        {"cwd": "/w/family", "session_id": "fam"},
    ]
    out = browse._folder_first_order(sessions, "/w/family")
    assert [s["session_id"] for s in out] == ["fam", "arch"]


def test_folder_first_order_handles_missing_cwd():
    sessions = [
        {"cwd": "", "session_id": "blank"},
        {"cwd": "/w/family", "session_id": "fam"},
    ]
    out = browse._folder_first_order(sessions, "/w/family")
    assert [s["session_id"] for s in out] == ["fam", "blank"]


def test_folder_first_order_empty_current_cwd_returns_unchanged():
    sessions = [{"cwd": "/w/a", "session_id": "a"}, {"cwd": "/w/b", "session_id": "b"}]
    assert browse._folder_first_order(sessions, "") == sessions
    assert browse._folder_first_order(sessions, None) == sessions


def test_folder_first_order_canonicalizes_both_sides(monkeypatch):
    # Prove both stored cwd and current cwd go through canonicalize_path, so a
    # casing difference still groups together. Stub lowercases.
    monkeypatch.setattr(
        browse, "canonicalize_path", lambda p: (p or "").lower() or None
    )
    sessions = [
        {"cwd": "/Users/Shamanth/team-operations", "session_id": "team"},
        {"cwd": "/Users/Shamanth/personal-ops/family", "session_id": "fam"},
    ]
    out = browse._folder_first_order(sessions, "/users/shamanth/personal-ops/family")
    assert out[0]["session_id"] == "fam"


def test_main_waits_for_winner_when_cold_start_loses_election(monkeypatch, capsys):
    """A cold-start window that loses the writer election must wait for the
    winner and then proceed -- never silently exit (the old behavior that
    made concurrently launched windows just disappear)."""

    class CountCursor:
        def fetchone(self):
            return (0,)

    class Conn:
        def execute(self, _sql):
            return CountCursor()

        def close(self):
            return None

    reindex_calls = []

    def fake_reindex(_conn, **_kwargs):
        reindex_calls.append(1)
        if len(reindex_calls) == 1:
            return None  # lost the election
        return (5, 0, 0)  # winner finished; our turn is a fast no-op

    monkeypatch.setattr(browse, "_check_fzf", lambda: None)
    monkeypatch.setattr(browse, "_providers_with_local_state", lambda: ["claude"])
    monkeypatch.setattr(browse, "_folder_prefixes", lambda: [])
    monkeypatch.setattr(browse.fts, "open_db", lambda *args, **kwargs: Conn())
    monkeypatch.setattr(browse.fts, "reindex", fake_reindex)
    monkeypatch.setattr(
        browse.fts, "acquire_reindex_lock", lambda *args, **kwargs: 3
    )
    monkeypatch.setattr(browse.fts, "release_reindex_lock", lambda _fd: None)
    monkeypatch.setattr(
        browse.fts, "list_recent", lambda _conn, limit, cwd=None: [_info()]
    )
    monkeypatch.setattr(
        browse.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 1, "stdout": ""})(),
    )
    monkeypatch.setattr(browse.sys, "argv", ["claude-browse"])

    with pytest.raises(SystemExit) as excinfo:
        browse.main()

    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    assert "waiting for it to finish" in captured.err
    assert len(reindex_calls) == 2




# --- fork-on-collision -------------------------------------------------


def _ps_stub(lines: list[str]):
    """Fake `ps -Ao pid=,tty=,command=` output."""

    class _R:
        returncode = 0
        stdout = "\n".join(lines)

    return lambda *a, **k: _R()


def test_session_holder_matches_provider_process(monkeypatch):
    monkeypatch.setattr(
        browse.subprocess,
        "run",
        _ps_stub(["68683 ttys010 codex resume abc-123 --dangerously-bypass"]),
    )
    assert browse._session_holder("abc-123", "codex") == (68683, "ttys010")


def test_session_holder_ignores_non_provider_process(monkeypatch):
    """A shell or editor merely mentioning the id is not a holder."""
    monkeypatch.setattr(
        browse.subprocess,
        "run",
        _ps_stub(
            [
                "111 ttys001 vim /tmp/abc-123.jsonl",
                "222 ttys002 grep abc-123 /var/log/x",
                "333 ttys003 codex resume other-id",
            ]
        ),
    )
    assert browse._session_holder("abc-123", "codex") is None


def test_session_holder_ignores_self(monkeypatch):
    import os as _os

    monkeypatch.setattr(
        browse.subprocess,
        "run",
        _ps_stub([f"{_os.getpid()} ttys001 codex resume abc-123"]),
    )
    assert browse._session_holder("abc-123", "codex") is None


def test_native_resume_forks_when_thread_already_open(monkeypatch, capsys):
    monkeypatch.setattr(browse, "_require_binary", lambda p: None)
    monkeypatch.setattr(
        browse, "_session_holder", lambda sid, binary: (68683, "ttys010")
    )
    execd: list[list[str]] = []
    monkeypatch.setattr(browse.os, "execvp", lambda f, c: execd.append(c))

    browse._native_resume({}, "codex", "abc-123", "/proj", (), True)

    assert execd and execd[0][:3] == ["codex", "fork", "abc-123"]
    assert "already open in ttys010" in capsys.readouterr().out


def test_native_resume_forks_by_default_even_without_collision(monkeypatch):
    monkeypatch.setattr(browse, "_require_binary", lambda p: None)
    monkeypatch.setattr(browse, "_session_holder", lambda sid, binary: None)
    execd: list[list[str]] = []
    monkeypatch.setattr(browse.os, "execvp", lambda f, c: execd.append(c))

    browse._native_resume({}, "codex", "abc-123", "/proj", (), True)

    assert execd and execd[0][:3] == ["codex", "fork", "abc-123"]


def test_native_resume_no_fork_flag_skips_collision_check(monkeypatch):
    """--no-fork restores the old attach-anyway behavior."""
    monkeypatch.setattr(browse, "_require_binary", lambda p: None)
    called: list[str] = []
    monkeypatch.setattr(
        browse,
        "_session_holder",
        lambda sid, binary: called.append(sid) or (1, "ttys010"),
    )
    execd: list[list[str]] = []
    monkeypatch.setattr(browse.os, "execvp", lambda f, c: execd.append(c))

    browse._native_resume({}, "codex", "abc-123", "/proj", (), True, fork=False)

    assert not called
    assert execd and execd[0][:3] == ["codex", "resume", "abc-123"]


def test_native_resume_fork_flag_forces_fork(monkeypatch):
    monkeypatch.setattr(browse, "_require_binary", lambda p: None)
    monkeypatch.setattr(browse, "_session_holder", lambda sid, binary: None)
    execd: list[list[str]] = []
    monkeypatch.setattr(browse.os, "execvp", lambda f, c: execd.append(c))

    browse._native_resume({}, "claude", "abc-123", "/proj", (), False, fork=True)

    assert execd
    assert execd[0][:4] == ["claude", "--resume", "abc-123", "--fork-session"]


def test_native_resume_errors_when_provider_cannot_fork(monkeypatch, capsys):
    monkeypatch.setattr(browse, "_require_binary", lambda p: None)
    monkeypatch.setattr(
        browse, "_session_holder", lambda sid, binary: (99, "ttys004")
    )
    monkeypatch.setattr(browse.os, "execvp", lambda f, c: None)

    with pytest.raises(SystemExit):
        browse._native_resume({}, "gemini", "abc-123", "/proj", (), True)

    assert "cannot fork" in capsys.readouterr().err
