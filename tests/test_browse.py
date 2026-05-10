"""Tests for claude_browse.browse: row formatting for fzf.

The browse module is mostly fzf integration (hard to unit-test), but
format_row is a pure function and worth pinning — it has historically been
the source of subtle bugs where embedded control characters split a logical
row into multiple visual rows in the picker.
"""

from __future__ import annotations

from claude_browse.browse import format_row


def _info(**overrides) -> dict:
    base = {
        "session_id": "abc-123",
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
    assert row.rstrip().endswith("abc-123###/home/alice/proj")
