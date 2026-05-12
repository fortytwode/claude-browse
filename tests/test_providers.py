"""Tests for the internal provider registry."""

from __future__ import annotations

import pytest

from claude_browse.providers import alternate_provider, get_provider, provider_ids


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
