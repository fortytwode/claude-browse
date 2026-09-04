from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time

import pytest

from claude_browse.board import commands, hook, projects, store, sync, work_items


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    projects.resolve_project.cache_clear()


def test_work_item_crud_and_done_filter(tmp_path):
    store.upsert("crud-session", cwd=str(tmp_path), name="Plan release", provider="claude")
    task = work_items.create(
        title="Plan release",
        project_path=tmp_path,
        due_date="2026-09-05",
        provider="claude",
        session_id="crud-session",
    )
    assert task["project_name"] == tmp_path.name
    assert task["status"] == "active"
    assert [row["task_id"] for row in work_items.list_items()] == [task["task_id"]]

    done = work_items.update(task["task_id"], title="Release", status="done")
    assert done["title"] == "Release"
    assert done["completed_at"] is not None
    assert work_items.list_items() == []
    assert work_items.list_items(include_done=True)[0]["task_id"] == task["task_id"]


def test_work_item_validation_and_one_task_per_session(tmp_path):
    with pytest.raises(ValueError, match="title is required"):
        work_items.create(title="", project_path=tmp_path, session_id="validation")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.create(title="x", project_path=tmp_path, due_date="tomorrow", session_id="date-1")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.create(title="x", project_path=tmp_path, due_date="20260905", session_id="date-2")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.create(title="x", project_path=tmp_path, due_date="2026-W36-6", session_id="date-3")
    with pytest.raises(ValueError, match="status must"):
        work_items.create(title="x", project_path=tmp_path, status="urgent", session_id="status")

    work_items.create(title="first", project_path=tmp_path, session_id="sid")
    with pytest.raises(ValueError, match="already in"):
        work_items.create(title="again", project_path=tmp_path, session_id="sid")
    linked = work_items.get_for_session("sid")
    with pytest.raises(ValueError, match="cannot change"):
        work_items.update(linked["task_id"], provider="codex")


def test_project_groups_git_subfolders_by_origin(tmp_path):
    repo = tmp_path / "checkout"
    nested = repo / "app" / "tests"
    nested.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Acme/Widget.git"],
        check=True,
    )
    project = projects.resolve_project(str(nested))
    assert project["key"] == "repo:github.com/acme/widget"
    assert project["name"] == "widget"
    assert project["path"] == str(repo)


def test_project_normalizes_ssh_and_https_origins_and_falls_back_to_exact_folder(
    tmp_path, monkeypatch
):
    responses = iter([str(tmp_path), "git@github.com:Acme/Widget.git"])
    monkeypatch.setattr(projects, "_git", lambda *_args: next(responses))
    ssh = projects.resolve_project(str(tmp_path))
    projects.resolve_project.cache_clear()
    responses = iter([str(tmp_path), "https://github.com/acme/widget.git"])
    https = projects.resolve_project(str(tmp_path))
    assert ssh["key"] == https["key"] == "repo:github.com/acme/widget"

    projects.resolve_project.cache_clear()
    missing = tmp_path / "missing" / "nested"
    monkeypatch.setattr(
        projects,
        "_git",
        lambda *_args: pytest.fail("missing cwd must use immediate folder fallback"),
    )
    fallback = projects.resolve_project(str(missing))
    assert fallback == {
        "key": f"path:{missing}",
        "name": "nested",
        "path": str(missing),
    }


def test_startup_reconciliation_is_bulk_idempotent_and_retains_exact_cwd(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    nested = repo / "packages" / "app"
    nested.mkdir(parents=True)
    store.upsert("older", cwd=str(nested), name="Older", provider="claude")
    store.upsert("newer", cwd=str(nested), name="Newer", provider="codex")
    store._raw_set_updated_at("older", 10)
    store._raw_set_updated_at("newer", 20)
    monkeypatch.setattr(
        projects,
        "resolve_project",
        lambda cwd: {"key": "repo:example/project", "name": "project", "path": str(repo)},
    )

    assert work_items.reconcile_sessions() == 2
    assert work_items.reconcile_sessions() == 0
    rows = work_items.list_items(include_done=True)
    assert [row["session_id"] for row in rows] == ["newer", "older"]
    assert all(row["project_path"] == str(repo) for row in rows)
    assert all(row["session_cwd"] == str(nested) for row in rows)


def test_direct_session_commands_have_one_fixed_argv_safe_shape(monkeypatch):
    monkeypatch.setattr(
        commands,
        "_agent_board_executable",
        lambda: "/opt/Agent Board/bin/agent-board",
    )

    command = commands.direct_session_command(
        "sid; touch /tmp/nope", "codex", full_access=False
    )

    assert command == (
        "'/opt/Agent Board/bin/agent-board' direct-session "
        "'sid; touch /tmp/nope' codex false"
    )
    assert commands.direct_session_command("abc", "claude", full_access=True).endswith(
        " direct-session abc claude true"
    )


def test_direct_session_launch_reuses_browse_policy_with_hook_transcript(
    tmp_path, monkeypatch
):
    import claude_browse.browse as browse

    transcript = tmp_path / "thread.jsonl"
    transcript.write_bytes(b"x" * 37)
    store.upsert(
        "hook-only",
        cwd=str(tmp_path),
        provider="codex",
        transcript_path=str(transcript),
    )
    opened = []
    monkeypatch.setattr(
        commands.fts,
        "open_db",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("no index")),
    )
    monkeypatch.setattr(
        browse,
        "_open_in_target_provider",
        lambda *args, **kwargs: opened.append((args, kwargs)),
    )

    original_cwd = os.getcwd()
    commands.launch_direct_session("hook-only", "codex", full_access=False)

    args, kwargs = opened[0]
    session = args[0]
    assert session["path"] == str(transcript)
    assert session["source_size"] == 37
    assert args[1:5] == ("codex", "codex", "hook-only", str(tmp_path))
    assert args[6] is False
    assert kwargs["fork"] is None
    assert os.getcwd() == str(tmp_path)
    os.chdir(original_cwd)


def test_direct_session_cross_provider_requires_transcript(tmp_path, monkeypatch):
    store.upsert("hook-only", cwd=str(tmp_path), provider="claude")
    monkeypatch.setattr(
        commands.fts,
        "open_db",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("no index")),
    )

    with pytest.raises(ValueError, match="transcript is unavailable"):
        commands.launch_direct_session("hook-only", "codex", full_access=True)


def test_sessionless_creation_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="session_id is required"):
        work_items.create(title="new", project_path=tmp_path, provider="claude")


def test_cross_provider_continuation_preserves_recent_context(tmp_path, monkeypatch):
    import claude_browse.browse as browse

    transcript = tmp_path / "thread.jsonl"
    transcript.write_text('{"message":{"role":"user","content":"recent context"}}\n')
    store.upsert(
        "old-session",
        provider="claude",
        cwd=str(tmp_path),
        transcript_path=str(transcript),
    )
    monkeypatch.setattr(
        commands.fts,
        "open_db",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("no index")),
    )
    continued = []
    monkeypatch.setattr(
        browse,
        "_continue_in_provider",
        lambda *args, **kwargs: continued.append((args, kwargs)),
    )

    original_cwd = os.getcwd()
    commands.launch_direct_session("old-session", "codex", full_access=True)
    os.chdir(original_cwd)

    args, _kwargs = continued[0]
    assert args[0]["path"] == str(transcript)
    assert args[1:5] == ("claude", "codex", str(tmp_path), ())
    assert args[5] is True


def test_session_start_hook_automatically_creates_one_thread_row(tmp_path):
    assert hook.dispatch(
        {"hook_event_name": "SessionStart", "session_id": "created-session", "cwd": str(tmp_path)},
        provider="codex",
    )
    created = work_items.get_for_session("created-session")
    assert created["session_id"] == "created-session"
    assert created["session_provider"] == "codex"
    assert created["status"] == "active"
    assert created["session_cwd"] == str(tmp_path)

    # A later hook changes runtime state but never overwrites user metadata.
    work_items.update(created["task_id"], title="My renamed thread", due_date="2026-09-07")
    hook.dispatch(
        {"hook_event_name": "UserPromptSubmit", "session_id": "created-session", "cwd": str(tmp_path), "prompt": "new prompt"},
        provider="codex",
    )
    preserved = work_items.get_for_session("created-session")
    assert preserved["title"] == "My renamed thread"
    assert preserved["due_date"] == "2026-09-07"


def test_automatic_capture_is_content_agnostic_concurrent_and_does_not_run_git(
    tmp_path, monkeypatch
):
    nested = tmp_path / "repo" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setattr(
        projects.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("hook capture must not run subprocesses"),
    )
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "same-session",
        "cwd": str(nested),
    }
    errors = []

    def capture():
        try:
            hook.dispatch(payload, provider="claude")
        except Exception as exc:  # pragma: no cover - assertion reports the worker failure
            errors.append(exc)

    threads = [threading.Thread(target=capture) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    rows = [row for row in work_items.list_items(include_done=True) if row["session_id"] == "same-session"]
    assert len(rows) == 1
    assert rows[0]["session_cwd"] == str(nested)


def test_hook_lifecycle_only_prompt_reactivates_done_and_never_archived(tmp_path):
    for session_id, closed_status in (("done-session", "done"), ("archived-session", "archived")):
        hook.dispatch(
            {"hook_event_name": "SessionStart", "session_id": session_id, "cwd": str(tmp_path)}
        )
        item = work_items.get_for_session(session_id)
        work_items.update(item["task_id"], status=closed_status)

        hook.dispatch({"hook_event_name": "SessionStart", "session_id": session_id, "cwd": str(tmp_path)})
        assert work_items.get_for_session(session_id)["status"] == closed_status

        hook.dispatch(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "cwd": str(tmp_path),
                "prompt": "continue work",
            }
        )
        expected = "active" if closed_status == "done" else "archived"
        assert work_items.get_for_session(session_id)["status"] == expected


def test_stop_on_closed_work_finishes_runtime_without_unattended_alert(tmp_path, monkeypatch):
    hook.dispatch(
        {"hook_event_name": "SessionStart", "session_id": "closed", "cwd": str(tmp_path)}
    )
    item = work_items.get_for_session("closed")
    work_items.update(item["task_id"], status="done")
    store.upsert("closed", state="working", working_since=time.time() - 5)
    notifications = []
    monkeypatch.setattr(hook.notify, "notify", lambda *args: notifications.append(args))

    hook.dispatch({"hook_event_name": "Stop", "session_id": "closed", "cwd": str(tmp_path)})

    runtime = store.get("closed")
    assert runtime["state"] == "idle"
    assert store.is_unattended(runtime) is False
    assert runtime["pending_alert"] is None
    assert notifications == []


def test_atomic_close_acknowledges_runtime_and_returns_post_commit_publication(tmp_path):
    store.upsert(
        "close-me",
        cwd=str(tmp_path),
        name="Close me",
        state="idle",
        done_at=20,
        acked_at=None,
        pending_alert="done",
        sync_revision=4,
    )
    item = work_items.ensure_for_session(store.get("close-me"))

    updated, publish_session = work_items.mutate(item["task_id"], status="done")

    runtime = store.get("close-me")
    assert updated["status"] == "done"
    assert runtime["acked_at"] >= runtime["done_at"]
    assert runtime["pending_alert"] is None
    assert runtime["sync_revision"] == 5
    assert publish_session == "close-me"


def test_live_state_projection_excludes_local_work_and_transcript_fields():
    projected = sync.session_doc(
        {
            "session_id": "private",
            "provider": "claude",
            "cwd": "/repo",
            "transcript": "secret",
            "transcript_path": "/secret/thread.jsonl",
            "due_date": "2026-09-05",
            "work_status": "archived",
            "status": "archived",
            "continuation_brief": "private brief",
        }
    )
    forbidden = {
        "transcript",
        "transcript_path",
        "due_date",
        "work_status",
        "status",
        "continuation_brief",
    }
    assert forbidden.isdisjoint(projected)


def test_legacy_overlay_migration_is_idempotent_and_preserves_sessionless_rows(
    tmp_path, monkeypatch
):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, cwd TEXT, name TEXT);
        CREATE TABLE work_items (
          task_id TEXT PRIMARY KEY, title TEXT NOT NULL, project_key TEXT NOT NULL,
          project_name TEXT NOT NULL, project_path TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'todo', due_date TEXT, session_id TEXT UNIQUE,
          session_provider TEXT, notes TEXT, created_at REAL NOT NULL,
          updated_at REAL NOT NULL, completed_at REAL
        );
        INSERT INTO sessions VALUES ('linked', '/repo/exact/nested', 'runtime title');
        INSERT INTO work_items VALUES
          ('linked-task', 'User title', 'path:/repo', 'repo', '/repo', 'waiting',
           '2026-09-08', 'linked', 'claude', '', 10, 20, NULL),
          ('legacy-note', 'Keep me', 'inbox', 'Inbox', '', 'todo', NULL,
           NULL, 'claude', 'legacy', 11, 21, NULL),
          ('0b001368-52a5-4368-8638-bf7b79670851', 'Prototype', 'inbox', 'Inbox', '',
           'todo', NULL, NULL, 'claude', '', 12, 22, NULL);
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "_DB_PATH", db)
    monkeypatch.setattr(store, "_conn_cache", None)

    linked = work_items.get("linked-task")
    assert linked["status"] == "active"
    assert linked["title_override"] == "User title"
    assert linked["title_source"] == "manual"
    assert linked["session_cwd"] == "/repo/exact/nested"
    assert linked["due_date"] == "2026-09-08"
    assert work_items.get("legacy-note") is not None
    assert work_items.list_items(include_done=True) == [linked]
    assert work_items.get("0b001368-52a5-4368-8638-bf7b79670851") is None
    assert work_items.migration_backup_path().exists()

    # Reopening and rerunning the column-driven migration is a no-op.
    store._conn_cache.close()
    store._conn_cache = None
    assert work_items.get("linked-task") == linked


def test_overlay_migration_failure_rolls_back_and_backup_preserves_legacy_rows(
    tmp_path, monkeypatch
):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, cwd TEXT, name TEXT);
        CREATE TABLE work_items (
          task_id TEXT PRIMARY KEY, title TEXT NOT NULL, project_key TEXT NOT NULL,
          project_name TEXT NOT NULL, project_path TEXT NOT NULL, status TEXT NOT NULL,
          due_date TEXT, session_id TEXT UNIQUE, session_provider TEXT, notes TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL
        );
        INSERT INTO sessions VALUES ('linked', '/exact', 'runtime');
        INSERT INTO work_items VALUES
          ('task', 'Manual', 'path:/old', 'old', '/old', 'waiting', '2026-09-09',
           'linked', 'claude', 'note', 1, 2, NULL);
        """
    )
    conn.commit()
    original = conn.execute("SELECT * FROM work_items").fetchall()
    conn.close()
    monkeypatch.setattr(store, "_DB_PATH", db)
    monkeypatch.setattr(store, "_conn_cache", None)
    monkeypatch.setattr(
        work_items,
        "_migrate_legacy_rows",
        lambda _conn: (_ for _ in ()).throw(RuntimeError("injected migration failure")),
    )

    with pytest.raises(RuntimeError, match="injected migration failure"):
        work_items.get("task")

    check = sqlite3.connect(db)
    assert {row[1] for row in check.execute("PRAGMA table_info(work_items)")} == {
        "task_id", "title", "project_key", "project_name", "project_path",
        "status", "due_date", "session_id", "session_provider", "notes",
        "created_at", "updated_at", "completed_at",
    }
    assert check.execute("SELECT * FROM work_items").fetchall() == original
    check.close()

    backup = sqlite3.connect(work_items.migration_backup_path())
    assert backup.execute("SELECT * FROM work_items").fetchall() == original
    backup.close()


def test_terminal_launch_passes_command_as_argv_not_applescript(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(
        commands.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or Result(),
    )
    dangerous_text = "claude 'quoted'\nsecond line"
    commands.open_in_terminal(dangerous_text)
    argv, _kwargs = calls[0]
    assert dangerous_text not in argv[2]
    assert argv[-2:] == ["--", dangerous_text]


def test_terminal_launch_normalizes_timeout_to_os_error(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("osascript", 10)

    monkeypatch.setattr(commands.subprocess, "run", timeout)
    with pytest.raises(OSError, match="could not open Terminal"):
        commands.open_in_terminal("echo hello")


def test_terminal_launch_normalizes_missing_osascript_to_scoped_error(monkeypatch):
    monkeypatch.setattr(
        commands.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("osascript")),
    )

    with pytest.raises(OSError, match="could not open Terminal: osascript"):
        commands.open_in_terminal("echo hello")
