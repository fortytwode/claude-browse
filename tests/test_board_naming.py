"""Tests for the auto-namer (board/naming.py): provisional -> Haiku upgrade."""

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
    monkeypatch.setattr(
        naming, "get_session_info",
        lambda path: {"name": None, "first_msg": "fix the generator priyansha feedback issue"},
    )
    calls = []

    def _get_client():
        calls.append("called")
        return _fake_client("fix generator priyansha feedback")

    monkeypatch.setattr(naming, "_get_client", _get_client)

    name = naming.compute_name("s1")

    assert name == "fix generator priyansha feedback"
    assert calls == ["called"]


def test_compute_name_prefers_existing_ai_title_skips_client_call(monkeypatch):
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(
        naming, "get_session_info",
        lambda path: {"name": "existing ai title", "first_msg": "whatever"},
    )
    calls = []
    monkeypatch.setattr(naming, "_get_client", lambda: calls.append("called"))

    name = naming.compute_name("s2")

    assert name == "existing ai title"
    assert calls == []


def test_compute_name_returns_none_when_client_construction_fails(monkeypatch):
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: "/fake/path.jsonl")
    monkeypatch.setattr(
        naming, "get_session_info",
        lambda path: {"name": None, "first_msg": "some prompt"},
    )

    def _raise():
        raise RuntimeError("no creds available")

    monkeypatch.setattr(naming, "_get_client", _raise)

    assert naming.compute_name("s3") is None


def test_compute_name_returns_none_when_no_jsonl_found(monkeypatch):
    monkeypatch.setattr(naming, "_find_jsonl_path", lambda sid: None)
    assert naming.compute_name("s4") is None


def test_maybe_name_upgrades_provisional_row(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s5", host="air", cwd="/tmp/proj", state="idle",
                 name="fix generator", name_source="provisional")

    monkeypatch.setattr(naming, "compute_name", lambda sid: "fix generator priyansha feedback")
    naming.maybe_name("s5")

    row = store.get("s5")
    assert row["name"] == "fix generator priyansha feedback"
    assert row["name_source"] == "haiku"


def test_maybe_name_is_noop_when_already_haiku_sourced(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s6", host="air", cwd="/tmp/proj", state="idle",
                 name="already-named", name_source="haiku")

    calls = []
    monkeypatch.setattr(naming, "compute_name", lambda sid: calls.append(sid) or "should-not-be-used")
    naming.maybe_name("s6")

    assert calls == []
    assert store.get("s6")["name"] == "already-named"


def test_maybe_name_leaves_provisional_untouched_on_compute_failure(tmp_path, monkeypatch):
    _fresh_store(tmp_path, monkeypatch)
    store.upsert("s7", host="air", cwd="/tmp/proj", state="idle",
                 name="provisional-name", name_source="provisional")

    monkeypatch.setattr(naming, "compute_name", lambda sid: None)
    naming.maybe_name("s7")

    row = store.get("s7")
    assert row["name"] == "provisional-name"
    assert row["name_source"] == "provisional"
