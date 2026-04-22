"""Tests for claude_browse.core — parsing, formatting, path canonicalization.

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


# --- extract_user_text ------------------------------------------------------


def test_extract_user_text_only_user_messages():
    text = core.extract_user_text(str(FIXTURES / "sample_session.jsonl"))
    assert "login page crashes" in text
    assert "email validation" in text
    # Assistant text must NOT leak in
    assert "login handler" not in text
    assert "short-circuiting" not in text


def test_extract_user_text_lowercases():
    text = core.extract_user_text(str(FIXTURES / "sample_session.jsonl"))
    assert text == text.lower()


def test_extract_user_text_missing_file():
    assert core.extract_user_text("/does/not/exist.jsonl") == ""


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
