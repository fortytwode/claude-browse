"""Tests for the auto-namer (board/naming.py).

Covers the refresh design: a fresh/short session reuses Claude Code's own
ai-title for free; a session that has grown past _REFRESH_AFTER_MSGS gets a
name re-synthesized from its most RECENT turns, not frozen forever on its
opening prompt or a title set once at session start.
"""

from __future__ import annotations

from types import SimpleNamespace

from claude_browse.board import naming, store


def _fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")


def _fake_client(reply_text):
    content = SimpleNamespace(text=reply_text)
    response = SimpleNamespace(content=[content])
    messages = SimpleNamespace(create=lambda **kwargs: response)
    return SimpleNamespace(messages=messages)


def test_compute_name_calls_haiku_when_no_existing_title(monkeypatch):
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(naming, "transcript_turns", lambda path, sid: [
        ("user", "fix the generator priyansha feedback issue"),
    ])
    monkeypatch.setattr(
        naming, "get_session_info",
        lambda path: {"name": None, "first_msg": "fix the generator priyansha feedback issue",
                       "msg_count": 2},
    )
    calls = []

    def _get_client():
        calls.append("called")
        return _fake_client("fix generator priyansha feedback")

    monkeypatch.setattr(naming, "_get_client", _get_client)

    name = naming.compute_name("s1")

    assert name == "fix generator priyansha feedback"
    assert calls == ["called"]


def test_compute_name_reuses_existing_title_when_session_still_short(monkeypatch):
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(
        naming, "get_session_info",
        lambda path: {"name": "existing ai title", "first_msg": "whatever", "msg_count": 5},
    )
    calls = []
    monkeypatch.setattr(naming, "_get_client", lambda: calls.append("called"))

    name = naming.compute_name("s2")

    assert name == "existing ai title"
    assert calls == []  # short session -- no API call needed


def test_compute_name_refreshes_from_recent_turns_once_session_has_grown(monkeypatch):
    """The core fix: a session with an existing (possibly stale) title that
    has grown past the refresh threshold gets a fresh name from RECENT
    activity, not the frozen opening-derived title."""
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(
        naming, "get_session_info",
        lambda path: {
            "name": "continue codex session context import",  # stale opening title
            "first_msg": "continue the imported context",
            "msg_count": 45,  # well past _REFRESH_AFTER_MSGS
        },
    )
    monkeypatch.setattr(naming, "transcript_turns", lambda path, sid: [
        ("user", "let's build the agent thread status board feature"),
        ("assistant", "sounds good, let me plan it out"),
        ("user", "now let's fix the code review findings"),
        ("assistant", "fixing the notify.py emoji bug now"),
    ])

    captured_prompt = {}

    def _get_client():
        class _Client:
            class messages:
                @staticmethod
                def create(**kwargs):
                    captured_prompt["content"] = kwargs["messages"][0]["content"]
                    content = SimpleNamespace(text="fix code review findings for agent board")
                    return SimpleNamespace(content=[content])
        return _Client()

    monkeypatch.setattr(naming, "_get_client", _get_client)

    name = naming.compute_name("s2b")

    assert name == "fix code review findings for agent board"
    assert "fix the code review findings" in captured_prompt["content"]  # used recent turns
    assert "continue the imported context" not in captured_prompt["content"]  # not the stale opener


def test_compute_name_returns_existing_title_when_client_construction_fails(monkeypatch):
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(naming, "transcript_turns", lambda path, sid: [("user", "some prompt")])
    monkeypatch.setattr(
        naming, "get_session_info",
        lambda path: {"name": "fallback title", "first_msg": "some prompt", "msg_count": 30},
    )

    def _raise():
        raise RuntimeError("no creds available")

    monkeypatch.setattr(naming, "_get_client", _raise)

    assert naming.compute_name("s3") == "fallback title"  # degrades to existing title, not None


def test_compute_name_returns_none_when_no_jsonl_found(monkeypatch):
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: None)
    assert naming.compute_name("s4") is None


def test_maybe_name_names_a_never_named_row(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s5", host="air", cwd="/tmp/proj", state="idle",
                 name="fix generator", name_source="provisional")
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(naming, "get_session_info", lambda path: {"name": None, "msg_count": 3})
    monkeypatch.setattr(naming, "compute_name", lambda sid, info=None: "fix generator priyansha feedback")

    naming.maybe_name("s5")

    row = store.get("s5")
    assert row["name"] == "fix generator priyansha feedback"
    assert row["name_source"] == "haiku"
    assert row["named_at_msg_count"] == 3


def test_maybe_name_is_noop_when_recently_named_and_not_grown_enough(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s6", host="air", cwd="/tmp/proj", state="idle",
                 name="already-named", name_source="haiku", named_at_msg_count=10)
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(naming, "get_session_info", lambda path: {"name": None, "msg_count": 12})

    calls = []
    monkeypatch.setattr(naming, "compute_name", lambda sid, info=None: calls.append(sid) or "x")
    naming.maybe_name("s6")

    assert calls == []  # only grown by 2, below _REFRESH_AFTER_MSGS
    assert store.get("s6")["name"] == "already-named"


def test_maybe_name_refreshes_once_grown_past_threshold(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s6b", host="air", cwd="/tmp/proj", state="idle",
                 name="stale-name", name_source="haiku", named_at_msg_count=10)
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(naming, "get_session_info", lambda path: {"name": None, "msg_count": 31})
    monkeypatch.setattr(naming, "compute_name", lambda sid, info=None: "fresh-name-from-recent-work")

    naming.maybe_name("s6b")

    row = store.get("s6b")
    assert row["name"] == "fresh-name-from-recent-work"
    assert row["named_at_msg_count"] == 31


def test_maybe_name_leaves_provisional_untouched_on_compute_failure(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s7", host="air", cwd="/tmp/proj", state="idle",
                 name="provisional-name", name_source="provisional")
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(naming, "get_session_info", lambda path: {"name": None, "msg_count": 3})
    monkeypatch.setattr(naming, "compute_name", lambda sid, info=None: None)

    naming.maybe_name("s7")

    row = store.get("s7")
    assert row["name"] == "provisional-name"
    assert row["name_source"] == "provisional"
