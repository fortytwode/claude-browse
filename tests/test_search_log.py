from __future__ import annotations

import json

from claude_browse import search_log


def test_log_search_writes_jsonl_with_top_results(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.delenv("CLAUDE_BROWSE_LOG", raising=False)

    search_log.log_search(
        "ARTEM ASC",
        [
            {
                "session_id": "abc-123",
                "provider": "codex",
                "cwd": "/tmp/musopia",
                "name": "Continue the imported Claude session context",
                "context": "Sent the clarified reply to \x01Artem\x02 in the ASA/\x01ASC\x02 thread.",
                "match_timestamp": "2026-05-20T07:20:00Z",
                "match_segment_idx": 42,
                "msg_count": 99,
            }
        ],
        ranker="v1",
        cwd_filter=None,
        limit=10,
        elapsed_ms=12.345,
    )

    event = json.loads(path.read_text().strip())
    assert event["event"] == "search"
    assert event["query"] == "ARTEM ASC"
    assert event["anchor_terms"] == ["artem", "asc"]
    assert event["ranker"] == "v1"
    assert event["top"][0]["session_id"] == "abc-123"
    assert event["top"][0]["context"] == (
        "Sent the clarified reply to Artem in the ASA/ASC thread."
    )


def test_log_event_can_be_disabled(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.setenv("CLAUDE_BROWSE_LOG", "0")

    search_log.log_event("search", {"query": "pokpok"})

    assert not path.exists()


def test_log_event_rotates_when_file_exceeds_limit(tmp_path, monkeypatch):
    path = tmp_path / "search.log.jsonl"
    path.write_text("x" * 20)
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_PATH", str(path))
    monkeypatch.setenv("CLAUDE_BROWSE_LOG_MAX_BYTES", "10")

    search_log.log_event("search", {"query": "pokpok"})

    assert (tmp_path / "search.log.jsonl.1").exists()
    event = json.loads(path.read_text().strip())
    assert event["query"] == "pokpok"
