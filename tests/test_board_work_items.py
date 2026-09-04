from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from claude_browse.board import commands, hook, projects, store, sync, work_items


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    projects.resolve_project.cache_clear()


def test_session_work_item_mutation_and_done_filter(tmp_path):
    store.upsert("crud-session", cwd=str(tmp_path), name="Plan release", provider="claude")
    task = work_items.ensure_for_session(store.get("crud-session"))
    assert task["project_name"] == tmp_path.name
    assert task["status"] == "active"
    assert [row["task_id"] for row in work_items.list_items()] == [task["task_id"]]

    done, _publish_session = work_items.mutate(
        task["task_id"], title="Release", due_date="2026-09-05", status="done"
    )
    assert done["title"] == "Release"
    assert done["due_date"] == "2026-09-05"
    assert done["completed_at"] is not None
    assert work_items.list_items() == []
    assert work_items.list_items(include_done=True)[0]["task_id"] == task["task_id"]


def test_work_item_mutation_validation_and_one_task_per_session(tmp_path):
    store.upsert("validation", cwd=str(tmp_path), name="Validate", provider="claude")
    task = work_items.ensure_for_session(store.get("validation"))
    with pytest.raises(ValueError, match="title is required"):
        work_items.mutate(task["task_id"], title="")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.mutate(task["task_id"], due_date="tomorrow")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.mutate(task["task_id"], due_date="20260905")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.mutate(task["task_id"], due_date="2026-W36-6")
    with pytest.raises(ValueError, match="status must"):
        work_items.mutate(task["task_id"], status="urgent")
    for invalid in (None, "", 0, False):
        with pytest.raises(ValueError, match="status must"):
            work_items.mutate(task["task_id"], status=invalid)
        with pytest.raises(ValueError, match="priority must"):
            work_items.mutate(task["task_id"], priority=invalid)
    with pytest.raises(ValueError, match="unknown field"):
        work_items.mutate(task["task_id"], provider="codex")

    same = work_items.ensure_for_session(store.get("validation"))
    assert same["task_id"] == task["task_id"]


def test_mutation_uses_transactional_row_for_close_side_effects(tmp_path, monkeypatch):
    store.upsert("transactional", cwd=str(tmp_path), name="Atomic", provider="claude")
    task = work_items.ensure_for_session(store.get("transactional"))
    work_items.mutate(task["task_id"], status="done")
    with store.get_conn() as conn:
        conn.execute("UPDATE sessions SET acked_at = NULL WHERE session_id = ?", ("transactional",))

    stale = dict(task)
    stale["status"] = "active"
    monkeypatch.setattr(work_items, "get", lambda _task_id: stale)

    unchanged, publish_session = work_items.mutate(task["task_id"], status="done")

    assert unchanged["status"] == "done"
    assert publish_session is None
    assert store.get("transactional")["acked_at"] is None


def test_priority_defaults_validates_and_survives_hook_updates(tmp_path):
    store.upsert("priority", cwd=str(tmp_path), name="Prioritize", provider="claude")
    task = work_items.ensure_for_session(store.get("priority"))
    assert task["priority"] == "normal"
    assert isinstance(task["position"], int)

    updated, publish_session = work_items.mutate(task["task_id"], priority="urgent")
    assert updated["priority"] == "urgent"
    assert publish_session is None
    with pytest.raises(ValueError, match="priority must"):
        work_items.mutate(task["task_id"], title="must roll back", priority="later")
    assert work_items.get(task["task_id"])["title"] == "Prioritize"

    hook.dispatch(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "priority",
            "cwd": str(tmp_path),
            "prompt": "continue",
        }
    )
    preserved = work_items.get(task["task_id"])
    assert preserved["priority"] == "urgent"
    assert preserved["position"] == task["position"]


def test_task_reorder_is_atomic_bounded_and_project_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        projects,
        "resolve_project",
        lambda cwd: {"key": f"path:{cwd}", "name": Path(cwd).name, "path": cwd},
    )
    first_dir = str(tmp_path / "first")
    second_dir = str(tmp_path / "second")
    for sid in ("one", "two", "three"):
        store.upsert(sid, cwd=first_dir, name=sid)
        work_items.reconcile_sessions()
    store.upsert("elsewhere", cwd=second_dir, name="elsewhere")
    work_items.reconcile_sessions()
    rows = {row["session_id"]: row for row in work_items.list_items(include_done=True)}
    original_slots = sorted(rows[sid]["position"] for sid in ("one", "two", "three"))

    reordered = work_items.reorder_tasks(
        f"path:{first_dir}",
        [rows["three"]["task_id"], rows["one"]["task_id"], rows["two"]["task_id"]],
        priority="high",
    )
    assert [row["session_id"] for row in reordered] == ["three", "one", "two"]
    assert [row["position"] for row in reordered] == original_slots
    assert {row["priority"] for row in reordered} == {"high"}

    for sid, priority in (("one", "urgent"), ("two", "low"), ("three", "normal")):
        work_items.mutate(rows[sid]["task_id"], priority=priority)
    mixed = work_items.reorder_tasks(
        f"path:{first_dir}",
        [rows["two"]["task_id"], rows["three"]["task_id"], rows["one"]["task_id"]],
    )
    assert [row["session_id"] for row in mixed] == ["two", "three", "one"]
    assert {row["session_id"]: row["priority"] for row in mixed} == {
        "one": "urgent",
        "two": "low",
        "three": "normal",
    }

    snapshot = {sid: work_items.get(rows[sid]["task_id"]) for sid in rows}
    invalid_orders = (
        [rows["one"]["task_id"], rows["one"]["task_id"]],
        [rows["one"]["task_id"], rows["elsewhere"]["task_id"]],
        [rows["one"]["task_id"], "missing"],
    )
    for task_ids in invalid_orders:
        with pytest.raises(ValueError):
            work_items.reorder_tasks(f"path:{first_dir}", task_ids, priority="urgent")
        assert {sid: work_items.get(rows[sid]["task_id"]) for sid in rows} == snapshot

    work_items.mutate(rows["one"]["task_id"], status="done")
    with pytest.raises(ValueError, match="closed"):
        work_items.reorder_tasks(
            f"path:{first_dir}", [rows["one"]["task_id"]], priority="low"
        )


def test_project_settings_description_and_order_are_local_and_validated(tmp_path):
    for sid, cwd in (("one", tmp_path / "one"), ("two", tmp_path / "two")):
        store.upsert(sid, cwd=str(cwd), name=sid)
        work_items.ensure_for_session(store.get(sid))
    projects_before = work_items.list_projects()
    keys = [project["project_key"] for project in projects_before]

    saved = work_items.set_project_description(keys[0], "  Local planning notes  ")
    assert saved["description"] == "Local planning notes"
    with pytest.raises(ValueError, match="10000"):
        work_items.set_project_description(keys[0], "x" * 10_001)
    reordered = work_items.reorder_projects(list(reversed(keys)))
    assert [project["project_key"] for project in reordered] == list(reversed(keys))
    assert work_items.list_projects()[0]["project_key"] == keys[1]


def test_project_listing_does_not_mutate_settings(tmp_path):
    store.upsert("read-only", cwd=str(tmp_path), name="Read only")
    work_items.ensure_for_session(store.get("read-only"))
    with store.get_conn() as conn:
        before = conn.total_changes

    assert work_items.list_projects()

    with store.get_conn() as conn:
        assert conn.total_changes == before


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


def test_reconciliation_marks_projects_resolved_and_preserves_project_settings(
    tmp_path, monkeypatch
):
    nested = tmp_path / "repo" / "nested"
    nested.mkdir(parents=True)
    store.upsert("resolve-once", cwd=str(nested), name="Resolve once")
    item = work_items.ensure_for_session(store.get("resolve-once"))
    original_key = item["project_key"]
    work_items.set_project_description(original_key, "Keep this description")
    work_items.reorder_projects([original_key])
    original_position = work_items.list_projects()[0]["position"]
    calls = []

    def resolve(cwd):
        calls.append(cwd)
        return {"key": "repo:example/project", "name": "project", "path": str(nested.parent)}

    monkeypatch.setattr(projects, "resolve_project", resolve)

    assert work_items.reconcile_sessions() == 1
    assert work_items.reconcile_sessions() == 0
    assert calls == [str(nested)]
    updated = work_items.get_for_session("resolve-once")
    assert updated["project_resolved"] == 1
    assert updated["project_key"] == "repo:example/project"
    project = work_items.list_projects()[0]
    assert project["description"] == "Keep this description"
    assert project["position"] == original_position


def test_reconciliation_merges_distinct_transient_and_repository_descriptions(
    tmp_path, monkeypatch
):
    transient = tmp_path / "repo" / "nested"
    existing = tmp_path / "repo"
    transient.mkdir(parents=True)
    store.upsert("transient", cwd=str(transient), name="Transient")
    transient_item = work_items.ensure_for_session(store.get("transient"))
    nested_notes = "Nested " + "n" * 6_000
    repository_notes = "Repository " + "r" * 6_000
    work_items.set_project_description(transient_item["project_key"], nested_notes)
    store.upsert("existing", cwd=str(existing), name="Existing")
    existing_item = work_items.ensure_for_session(store.get("existing"))
    repo_key = "repo:example/project"
    with store.get_conn() as conn:
        conn.execute(
            "UPDATE work_items SET project_key = ?, project_resolved = 1 "
            "WHERE task_id = ?",
            (repo_key, existing_item["task_id"]),
        )
        conn.execute(
            "INSERT INTO project_settings VALUES (?, ?, ?, ?)",
            (repo_key, repository_notes, 1, time.time()),
        )
    monkeypatch.setattr(
        projects,
        "resolve_project",
        lambda _cwd: {"key": repo_key, "name": "project", "path": str(existing)},
    )

    assert work_items.reconcile_sessions() == 1

    project = next(
        project for project in work_items.list_projects() if project["project_key"] == repo_key
    )
    assert project["description"] == repository_notes
    assert project["inherited_descriptions"] == [
        {"source_key": transient_item["project_key"], "description": nested_notes}
    ]


def test_reconciliation_defers_a_session_changed_during_project_discovery(
    tmp_path, monkeypatch
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store.upsert("moving", cwd=str(first), name="Before")
    work_items.ensure_for_session(store.get("moving"))
    calls = []

    def resolve(cwd):
        calls.append(cwd)
        if len(calls) == 1:
            store.upsert("moving", cwd=str(second), name="After")
        return {"key": f"path:{cwd}", "name": Path(cwd).name, "path": cwd}

    monkeypatch.setattr(projects, "resolve_project", resolve)

    assert work_items.reconcile_sessions() == 0
    assert work_items.get_for_session("moving")["project_resolved"] == 0
    assert work_items.reconcile_sessions() == 1
    updated = work_items.get_for_session("moving")
    assert updated["project_resolved"] == 1
    assert updated["project_path"] == str(second)
    assert calls == [str(first), str(second)]


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
    work_items.mutate(created["task_id"], title="My renamed thread", due_date="2026-09-07")
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
        work_items.mutate(item["task_id"], status=closed_status)

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
    work_items.mutate(item["task_id"], status="done")
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
            "priority": "urgent",
            "position": 123,
            "project_description": "private notes",
        }
    )
    forbidden = {
        "transcript",
        "transcript_path",
        "due_date",
        "work_status",
        "status",
        "continuation_brief",
        "priority",
        "position",
        "project_description",
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
    assert linked["priority"] == "normal"
    assert linked["position"] == 1_000_000
    assert work_items.get("legacy-note")["position"] == 2_000_000
    assert work_items.list_items(include_done=True) == [linked]
    assert work_items.get("0b001368-52a5-4368-8638-bf7b79670851") is None
    assert work_items.migration_backup_path().exists()
    assert work_items.planning_migration_backup_path().exists()

    indexes = {
        row[1]
        for row in store.get_conn().execute("PRAGMA index_list(work_items)").fetchall()
    }
    assert "idx_work_items_project_priority_position" in indexes

    # Reopening and rerunning the column-driven migration is a no-op.
    store._conn_cache.close()
    store._conn_cache = None
    assert work_items.get("linked-task") == linked


def test_planning_migration_failure_rolls_back_and_keeps_new_backup(
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
          created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL,
          title_override TEXT, title_source TEXT NOT NULL DEFAULT 'automatic',
          session_cwd TEXT
        );
        INSERT INTO work_items VALUES
          ('task', 'Manual', 'path:/old', 'old', '/old', 'active', NULL,
           NULL, 'claude', 'note', 1, 2, NULL, NULL, 'automatic', NULL);
        """
    )
    conn.commit()
    original = conn.execute("SELECT * FROM work_items").fetchall()
    conn.close()
    monkeypatch.setattr(store, "_DB_PATH", db)
    monkeypatch.setattr(store, "_conn_cache", None)
    monkeypatch.setattr(work_items, "_PROJECT_SETTINGS_SCHEMA", "invalid SQL")

    with pytest.raises(sqlite3.Error):
        work_items.get("task")

    check = sqlite3.connect(db)
    columns = {row[1] for row in check.execute("PRAGMA table_info(work_items)")}
    assert "priority" not in columns
    assert "position" not in columns
    assert check.execute("SELECT * FROM work_items").fetchall() == original
    check.close()
    backup = sqlite3.connect(work_items.planning_migration_backup_path())
    assert backup.execute("SELECT * FROM work_items").fetchall() == original
    backup.close()


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
