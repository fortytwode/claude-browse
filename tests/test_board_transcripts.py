from __future__ import annotations

import concurrent.futures
import os
import threading
import time

import pytest

from claude_browse.board import commands, projects, store
from claude_browse.providers import claude as claude_provider
from claude_browse.providers import codex as codex_provider


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    commands._raw_path_cache.clear()
    projects.resolve_project.cache_clear()


def test_valid_index_path_beats_a_stale_hook_path_and_keeps_manual_runtime_fields(
    tmp_path,
):
    transcript = tmp_path / "indexed.jsonl"
    transcript.write_bytes(b"indexed")
    store.upsert(
        "same-session",
        provider="claude",
        cwd=str(tmp_path),
        name="Keep this manual name",
        name_source="manual",
        transcript_path=str(tmp_path / "removed.jsonl"),
    )

    session = commands.session_for_launch(
        "same-session",
        {
            "session_id": "same-session",
            "provider": "claude",
            "cwd": "/old-project",
            "name": "Indexed name",
            "path": str(transcript),
        },
    )

    assert session == {
        "session_id": "same-session",
        "provider": "claude",
        "cwd": str(tmp_path),
        "name": "Keep this manual name",
        "path": str(transcript),
        "source_size": len(b"indexed"),
    }


@pytest.mark.parametrize("provider", ("claude", "codex"))
def test_hook_only_session_finds_exact_builtin_transcript_without_fts(
    tmp_path, monkeypatch, provider
):
    root = tmp_path / provider
    root.mkdir()
    if provider == "claude":
        session_id = "claude-session"
        transcript = root / "project" / f"{session_id}.jsonl"
        transcript.parent.mkdir()
        monkeypatch.setattr(claude_provider, "SESSIONS_DIR", str(root))
    else:
        session_id = "one-two-three-four-five"
        transcript = root / "2026" / "rollout-2026-01-01T00-00-00-one-two-three-four-five.jsonl"
        transcript.parent.mkdir()
        monkeypatch.setattr(codex_provider, "CODEX_SESSIONS_DIR", str(root))
    transcript.write_bytes(b"fixture")
    store.upsert(session_id, provider=provider, cwd=str(tmp_path))

    session = commands.session_for_launch(session_id, indexed=None)

    assert session["provider"] == provider
    assert session["cwd"] == str(tmp_path)
    assert session["path"] == str(transcript)
    assert session["source_size"] == len(b"fixture")
    assert commands.action_status(
        session,
        "codex" if provider == "claude" else "claude",
        availability_check=lambda _provider: True,
    )["available"]


def test_missing_known_transcript_remains_unavailable_for_handoff(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    monkeypatch.setattr(claude_provider, "SESSIONS_DIR", str(root))
    store.upsert("missing-session", provider="claude", cwd=str(tmp_path))

    session = commands.session_for_launch("missing-session", indexed=None)
    status = commands.action_status(
        session, "codex", availability_check=lambda _provider: True
    )

    assert "path" not in session
    assert status == {
        "label": "Continue in CodeX",
        "available": False,
        "reason": "Thread transcript is unavailable for provider handoff.",
    }


def test_indexed_transcript_from_another_provider_is_never_used(tmp_path):
    foreign_transcript = tmp_path / "codex.jsonl"
    foreign_transcript.write_bytes(b"foreign")
    store.upsert("shared-id", provider="claude", cwd=str(tmp_path))

    session = commands.session_for_launch(
        "shared-id",
        {
            "session_id": "shared-id",
            "provider": "codex",
            "cwd": str(tmp_path),
            "path": str(foreign_transcript),
            "name": "Wrong conversation",
        },
    )

    assert session == {
        "session_id": "shared-id",
        "provider": "claude",
        "cwd": str(tmp_path),
    }


def test_unsafe_session_id_never_scans_provider_files(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    monkeypatch.setattr(claude_provider, "SESSIONS_DIR", str(root))
    monkeypatch.setattr(
        commands.glob,
        "iglob",
        lambda *_args, **_kwargs: pytest.fail("unsafe ID must not enumerate session files"),
    )
    store.upsert("../not-a-session", provider="claude", cwd=str(tmp_path))

    session = commands.session_for_launch("../not-a-session", indexed=None)

    assert session == {
        "session_id": "../not-a-session",
        "provider": "claude",
        "cwd": str(tmp_path),
    }


def test_cached_raw_path_is_revalidated_when_the_file_disappears(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    transcript = root / "project" / "cached-session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"fixture")
    monkeypatch.setattr(claude_provider, "SESSIONS_DIR", str(root))
    store.upsert("cached-session", provider="claude", cwd=str(tmp_path))

    assert commands.session_for_launch("cached-session", indexed=None)["path"] == str(transcript)
    transcript.unlink()

    session = commands.session_for_launch("cached-session", indexed=None)

    assert "path" not in session


def test_many_missing_ids_share_one_provider_directory_enumeration(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    calls = []
    monkeypatch.setattr(claude_provider, "SESSIONS_DIR", str(root))
    monkeypatch.setattr(
        commands.glob,
        "iglob",
        lambda *_args, **_kwargs: calls.append("listed") or iter(()),
    )
    for session_id in ("missing-one", "missing-two", "missing-three"):
        store.upsert(session_id, provider="claude", cwd=str(tmp_path))

    for session_id in ("missing-one", "missing-two", "missing-three"):
        assert "path" not in commands.session_for_launch(session_id, indexed=None)

    assert calls == ["listed"]


def test_expired_negative_snapshot_discovers_a_new_transcript(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    monkeypatch.setattr(claude_provider, "SESSIONS_DIR", str(root))
    store.upsert("arrived-later", provider="claude", cwd=str(tmp_path))

    assert "path" not in commands.session_for_launch("arrived-later", indexed=None)
    transcript = root / "project" / "arrived-later.jsonl"
    transcript.parent.mkdir()
    transcript.write_bytes(b"fixture")
    key = ("claude", os.path.realpath(root))
    expires_at, filenames = commands._raw_path_cache[key]
    commands._raw_path_cache[key] = (0.0, filenames)

    assert commands.session_for_launch("arrived-later", indexed=None)["path"] == str(transcript)
    assert expires_at > 0


def test_concurrent_exact_lookups_enumerate_once_per_provider_root(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    root.mkdir()
    calls = []
    calls_lock = threading.Lock()

    def list_files(*_args, **_kwargs):
        with calls_lock:
            calls.append("listed")
        time.sleep(0.02)
        return iter(())

    monkeypatch.setattr(claude_provider, "SESSIONS_DIR", str(root))
    monkeypatch.setattr(commands.glob, "iglob", list_files)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(
            lambda _index: commands._raw_provider_path("claude", "missing-thread"),
            range(8),
        ))

    assert results == [None] * 8
    assert calls == ["listed"]
