"""Tests for claude_browse.core: parsing, formatting, path canonicalization.

No network. No real ~/.claude/projects access. Everything uses fixture files
under tests/fixtures/.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_browse import core

FIXTURES = Path(__file__).parent / "fixtures"


# --- get_session_info -------------------------------------------------------


def test_get_session_info_basic():
    info = core.get_session_info(str(FIXTURES / "sample_session.jsonl"))
    assert info is not None
    assert info["session_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert info["cwd"] == "/Users/alice/code/webapp"
    assert info["name"] == "Debug login flow"
    assert info["first_msg"].startswith("the login page crashes")
    # 2 user messages + 2 assistant messages
    assert info["msg_count"] == 4


def test_get_session_info_first_msg_strips_newlines():
    info = core.get_session_info(str(FIXTURES / "sample_session.jsonl"))
    # No literal newlines or leading whitespace in first_msg
    assert "\n" not in info["first_msg"]
    assert info["first_msg"] == info["first_msg"].strip()


def test_get_session_info_handles_content_list_format():
    """Messages with content as a list (with text items) should parse."""
    info = core.get_session_info(str(FIXTURES / "sample_session.jsonl"))
    # We took the first user message, which is a string. Check that list-form
    # messages are still counted.
    assert info["msg_count"] == 4


def test_get_session_info_malformed_file_survives():
    """Invalid JSON lines are skipped; valid lines still produce results."""
    info = core.get_session_info(str(FIXTURES / "malformed.jsonl"))
    assert info is not None
    assert info["session_id"] == "11111111-2222-3333-4444-555555555555"
    assert "does this still parse" in info["first_msg"]


def test_get_session_info_empty_file():
    info = core.get_session_info(str(FIXTURES / "empty.jsonl"))
    assert info is not None
    # Empty file → everything falsy but no crash
    assert info["session_id"] is None
    assert info["first_msg"] == ""
    assert info["msg_count"] == 0


def test_get_session_info_nonexistent_file():
    info = core.get_session_info("/does/not/exist.jsonl")
    assert info is None


def test_get_session_info_modern_titles():
    """Modern sessions emit ai-title (auto) and custom-title (manual).
    Custom should win over AI when both are present.
    """
    info = core.get_session_info(str(FIXTURES / "modern_session.jsonl"))
    assert info is not None
    # Fixture has both ai-title and a later custom-title; custom wins
    assert info["name"] == "Q3 platform hiring plan"


def test_get_session_info_ai_title_only():
    """When only ai-title is present (most common modern shape), it's used."""
    import json
    import tempfile

    events = [
        {
            "type": "ai-title",
            "aiTitle": "Refactor billing service",
            "sessionId": "x",
            "cwd": "/Users/alice/x",
            "timestamp": "2026-04-01T00:00:00Z",
        },
        {
            "type": "user",
            "sessionId": "x",
            "cwd": "/Users/alice/x",
            "timestamp": "2026-04-01T00:00:01Z",
            "message": {"role": "user", "content": "let's refactor billing"},
        },
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False
    ) as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
        path = f.name
    try:
        info = core.get_session_info(path)
        assert info["name"] == "Refactor billing service"
    finally:
        import os as _os
        _os.unlink(path)


def test_get_session_info_last_msg_skips_confirmations():
    """last_msg should be the latest substantive user message, not 'yes go ahead'."""
    info = core.get_session_info(str(FIXTURES / "modern_session.jsonl"))
    assert info is not None
    # The fixture's final user messages are "yes go ahead" and a tool-result
    # wrapper. The latest substantive message is the topic switch to hiring.
    assert "hiring plan" in info["last_msg"].lower()
    assert "yes go ahead" not in info["last_msg"].lower()


def test_get_session_info_first_and_last_can_differ():
    """When the conversation drifts to a new topic, first_msg and last_msg
    capture the start and the end. That's what the list view uses to
    surface topic drift.
    """
    info = core.get_session_info(str(FIXTURES / "modern_session.jsonl"))
    assert "signup is broken" in info["first_msg"].lower()
    assert "hiring plan" in info["last_msg"].lower()


def test_get_session_info_legacy_summary_still_works():
    """Older sessions only had `summary` events. Still produce a name."""
    info = core.get_session_info(str(FIXTURES / "sample_session.jsonl"))
    assert info["name"] == "Debug login flow"


# --- extract_search_corpus --------------------------------------------------


def test_extract_search_corpus_includes_both_sides():
    """User AND assistant text should be findable.

    Users searching for a term don't remember whether they or the assistant
    first said it. Older versions excluded assistant text and missed
    obvious cases like assistant-introduced acronyms.
    """
    text = core.extract_search_corpus(str(FIXTURES / "sample_session.jsonl"))
    assert "login page crashes" in text  # user
    assert "email validation" in text  # user
    assert "login handler" in text  # assistant
    assert "short-circuiting" in text  # assistant


def test_extract_search_corpus_lowercases():
    text = core.extract_search_corpus(str(FIXTURES / "sample_session.jsonl"))
    assert text == text.lower()


def test_extract_search_corpus_missing_file():
    assert core.extract_search_corpus("/does/not/exist.jsonl") == ""


def test_extract_search_corpus_filters_user_noise():
    """Tool-result wrappers and system-reminders shouldn't pollute the corpus."""
    text = core.extract_search_corpus(str(FIXTURES / "modern_session.jsonl"))
    # The modern fixture includes a user message wrapped in
    # <task-notification>; it must not appear in the searchable text.
    assert "task-notification" not in text
    assert "tool noise that should be filtered" not in text


def test_extract_user_text_alias_still_works():
    """Backwards compat: existing code that imports extract_user_text gets
    the same expanded corpus without behavior changes mid-import path."""
    a = core.extract_user_text(str(FIXTURES / "sample_session.jsonl"))
    b = core.extract_search_corpus(str(FIXTURES / "sample_session.jsonl"))
    assert a == b


# --- format_date ------------------------------------------------------------


def test_format_date_minutes_ago():
    ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    assert core.format_date(ts) == "30m ago"


def test_format_date_hours_ago():
    ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    assert core.format_date(ts) == "5h ago"


def test_format_date_none():
    assert core.format_date(None) == "???"


def test_format_date_malformed_falls_back():
    # Return the first 10 chars if parsing fails
    assert core.format_date("not-a-real-timestamp") == "not-a-real"


# --- canonicalize_path ------------------------------------------------------


@pytest.fixture(autouse=True)
def _stable_user(monkeypatch):
    """Pin USER so canonicalization is deterministic across CI environments."""
    monkeypatch.setenv("USER", "alice")
    # HOME is what canonicalize rewrites Mac/Linux paths to
    monkeypatch.setenv("HOME", "/home/alice")


def test_canonicalize_mac_to_home():
    assert core.canonicalize_path("/Users/alice/proj") == "/home/alice/proj"
    assert core.canonicalize_path("/Users/alice") == "/home/alice"


def test_canonicalize_linux_path_to_home():
    # /home/alice paths pass through unchanged since HOME is /home/alice
    assert core.canonicalize_path("/home/alice/proj") == "/home/alice/proj"


def test_canonicalize_unrelated_path_unchanged():
    assert core.canonicalize_path("/opt/thing") == "/opt/thing"
    assert core.canonicalize_path("/Users/bob/proj") == "/Users/bob/proj"


def test_canonicalize_none_and_empty():
    assert core.canonicalize_path(None) is None
    assert core.canonicalize_path("") == ""


def test_canonicalize_case_insensitive_match():
    """Mac HFS+ paths can have mismatched case; still canonicalize."""
    assert core.canonicalize_path("/Users/ALICE/proj") == "/home/alice/proj"


def test_canonicalize_custom_aliases(monkeypatch):
    monkeypatch.setenv(
        "CLAUDE_BROWSE_PATH_ALIASES",
        "/workspaces/repo=/home/alice/repo",
    )
    assert (
        core.canonicalize_path("/workspaces/repo/sub")
        == "/home/alice/repo/sub"
    )


def test_canonicalize_custom_alias_exact_match(monkeypatch):
    monkeypatch.setenv(
        "CLAUDE_BROWSE_PATH_ALIASES",
        "/workspaces/repo=/home/alice/repo",
    )
    assert core.canonicalize_path("/workspaces/repo") == "/home/alice/repo"


# --- folder_name ------------------------------------------------------------


def test_folder_name_last_component():
    # HOME was set to /home/alice
    assert core.folder_name("/home/alice/code/webapp") == "webapp"


def test_folder_name_with_prefix():
    assert (
        core.folder_name("/home/alice/monorepo/apps/checkout", ("monorepo/apps/",))
        == "checkout"
    )


def test_folder_name_prefix_matches_but_no_remainder():
    """If the prefix matches but nothing's after it, show the prefix's tail."""
    assert (
        core.folder_name("/home/alice/monorepo/apps/", ("monorepo/apps/",))
        == "apps"
    )


def test_folder_name_empty_and_none():
    assert core.folder_name(None) == "?"
    assert core.folder_name("") == "?"


# --- display_cwd ------------------------------------------------------------


def test_display_cwd_abbreviates_home():
    assert core.display_cwd("/home/alice/proj") == "~/proj"


def test_display_cwd_outside_home():
    assert core.display_cwd("/opt/tools") == "/opt/tools"


def test_display_cwd_empty():
    assert core.display_cwd(None) == ""
    assert core.display_cwd("") == ""


# --- extract_query_terms ---------------------------------------------------


def test_extract_query_terms_single_word():
    assert core.extract_query_terms("postiz") == ["postiz"]


def test_extract_query_terms_multiple_words():
    assert core.extract_query_terms("postiz tiktok") == ["postiz", "tiktok"]


def test_extract_query_terms_phrase():
    assert core.extract_query_terms('"tiktok ads"') == ["tiktok ads"]


def test_extract_query_terms_word_and_phrase():
    assert core.extract_query_terms('postiz "tiktok ads"') == [
        "postiz",
        "tiktok ads",
    ]


def test_extract_query_terms_empty():
    assert core.extract_query_terms("") == []
    assert core.extract_query_terms("   ") == []


# --- highlight_terms -------------------------------------------------------


def test_highlight_terms_wraps_match():
    out = core.highlight_terms(
        "the postiz pipeline", ["postiz"], open_marker="[", close_marker="]"
    )
    assert out == "the [postiz] pipeline"


def test_highlight_terms_case_insensitive():
    out = core.highlight_terms(
        "Postiz and POSTIZ", ["postiz"], open_marker="[", close_marker="]"
    )
    assert out == "[Postiz] and [POSTIZ]"


def test_highlight_terms_longer_first():
    """Phrase 'tiktok ads' should highlight as one span, not 'tiktok' + ' ads'."""
    out = core.highlight_terms(
        "scaling tiktok ads spend",
        ["tiktok", "tiktok ads"],
        open_marker="[",
        close_marker="]",
    )
    assert out == "scaling [tiktok ads] spend"


def test_highlight_terms_empty_terms_returns_text_unchanged():
    assert core.highlight_terms("hello world", []) == "hello world"


def test_highlight_terms_regex_chars_escaped():
    """Terms that look like regex shouldn't break the matcher."""
    out = core.highlight_terms(
        "use a.b? and c+d", ["a.b?"], open_marker="[", close_marker="]"
    )
    assert out == "use [a.b?] and c+d"
