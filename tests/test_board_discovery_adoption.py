"""Background enrollment must leave token-authorized task adoption in control."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from claude_browse.board import (
    commands,
    discovery,
    hook,
    launches,
    presence,
    projects,
    store,
    work_items,
    workspace,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    monkeypatch.setattr(commands, "_indexed_session", lambda _sid: None)
    monkeypatch.setattr(commands, "_raw_provider_path", lambda *_args: None)
    monkeypatch.setattr(launches, "_available", lambda _provider: True)
    monkeypatch.setattr(presence, "live_sessions", lambda: [])
    monkeypatch.delenv(launches.TOKEN_ENV, raising=False)
    projects.resolve_project.cache_clear()


def pending_task(tmp_path, monkeypatch, *, kind="task-new", provider="codex"):
    cwd = tmp_path / "work"
    cwd.mkdir()
    store.upsert("original", provider=provider, cwd=str(cwd), name="Original")
    task = work_items.ensure_for_session(store.get("original"))
    task, _ = work_items.mutate(
        task["task_id"], title="Keep this title", status="archived",
        priority="high", due_date="2026-09-10",
    )
    context = workspace.context_for_task(task)
    token = launches.prepare(
        kind, task["task_id"], provider, full_access=False,
        launch_revision=context["launch_revision"],
    )
    launches.claim(token)
    monkeypatch.setenv(launches.TOKEN_ENV, token)
    monkeypatch.setattr(presence, "live_sessions", lambda: [{
        "session_id": "fresh", "provider": provider, "cwd": str(cwd),
    }])
    return task, cwd, token


def capture_and_reconcile():
    discovery.capture_live_sessions()
    work_items.reconcile_sessions()


@pytest.mark.parametrize("kind", ["task", "task-new"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("phase", ["claimed", "adopting"])
def test_background_pass_between_session_start_and_adoption(
    tmp_path, monkeypatch, kind, provider, phase
):
    task, cwd, token = pending_task(tmp_path, monkeypatch, kind=kind, provider=provider)
    target = launches if phase == "claimed" else work_items
    name = "adopt_session" if phase == "claimed" else "attach_continuation"
    original = getattr(target, name)
    observed = []

    def interleaved(*args):
        assert store.get("fresh") is not None
        assert launches.get(token)["state"] == phase
        capture_and_reconcile()
        assert work_items.get_for_session("fresh") is None
        assert len(work_items.list_items(include_done=True)) == 1
        observed.append(phase)
        return original(*args)

    monkeypatch.setattr(target, name, interleaved)
    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "fresh", "cwd": str(cwd)}, provider)

    assert observed == [phase]
    assert launches.get(token)["state"] == "consumed"
    adopted = work_items.get(task["task_id"])
    assert adopted["session_id"] == "fresh"
    for field in ("title", "title_source", "status", "priority", "due_date"):
        assert adopted[field] == task[field]
    assert [row["session_id"] for row in work_items.get_session_history(task["task_id"])] == ["original", "fresh"]


@pytest.mark.parametrize("resolution", ["expired", "failed"])
@pytest.mark.parametrize("capture", [discovery.capture_live_sessions, work_items.reconcile_sessions])
def test_unmatched_root_is_enrolled_after_pending_intent_resolves(
    tmp_path, monkeypatch, resolution, capture
):
    task, cwd, token = pending_task(tmp_path, monkeypatch)
    store.upsert("fresh", provider="codex", cwd=str(cwd))
    capture_and_reconcile()
    assert work_items.get_for_session("fresh") is None
    if resolution == "expired":
        with store.get_conn() as conn:
            conn.execute("UPDATE workspace_launch_intents SET expires_at = 0 WHERE token = ?", (token,))
    else:
        launches.fail(token, "Synthetic launch failure")

    capture()

    enrolled = work_items.get_for_session("fresh")
    assert enrolled is not None and enrolled["task_id"] != task["task_id"]
    assert work_items.get(task["task_id"])["session_id"] == "original"
    assert len(work_items.list_items(include_done=True)) == 2


@pytest.mark.parametrize("mismatch", ["provider", "cwd", "prepared", "list"])
@pytest.mark.parametrize("capture", [discovery.capture_live_sessions, work_items.reconcile_sessions])
def test_unrelated_intent_does_not_suppress_ordinary_enrollment(
    tmp_path, monkeypatch, mismatch, capture
):
    task, cwd, token = pending_task(tmp_path, monkeypatch)
    provider = "claude" if mismatch == "provider" else "codex"
    observed_cwd = str(tmp_path / "other") if mismatch == "cwd" else str(cwd)
    if mismatch in {"prepared", "list"}:
        field, value = ("state", "prepared") if mismatch == "prepared" else ("kind", "list")
        with store.get_conn() as conn:
            conn.execute(f"UPDATE workspace_launch_intents SET {field} = ? WHERE token = ?", (value, token))
    store.upsert("fresh", provider=provider, cwd=observed_cwd)
    monkeypatch.setattr(presence, "live_sessions", lambda: [{
        "session_id": "fresh", "provider": provider, "cwd": observed_cwd,
    }])

    capture()

    enrolled = work_items.get_for_session("fresh")
    assert enrolled is not None and enrolled["task_id"] != task["task_id"]


def test_equivalent_directory_alias_still_defers_enrollment(tmp_path, monkeypatch):
    _, cwd, _ = pending_task(tmp_path, monkeypatch)
    alias = tmp_path / "alias"
    alias.symlink_to(cwd, target_is_directory=True)
    store.upsert("fresh", provider="codex", cwd=str(alias))

    capture_and_reconcile()

    assert work_items.get_for_session("fresh") is None


@pytest.mark.parametrize("capture", [discovery.capture_live_sessions, work_items.reconcile_sessions])
def test_enrollment_guard_excludes_a_concurrent_intent_writer(tmp_path, monkeypatch, capture):
    _, cwd, token = pending_task(tmp_path, monkeypatch)
    store.upsert("fresh", provider="codex", cwd=str(cwd))
    original = launches.awaiting_task_adoption
    checked = []

    def contended_guard(conn, provider, directory):
        # Exercise the actual SQLite writer exclusion, not just the order of
        # mocked calls: a launch transition cannot commit during this check.
        contender = sqlite3.connect(store._DB_PATH, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute(
                    "UPDATE workspace_launch_intents SET state = 'adopting' WHERE token = ?",
                    (token,),
                )
        finally:
            contender.close()
        checked.append(True)
        return original(conn, provider, directory)

    monkeypatch.setattr(launches, "awaiting_task_adoption", contended_guard)

    capture()

    assert checked
    assert work_items.get_for_session("fresh") is None


def test_reconciliation_rechecks_links_after_adoption_and_delayed_hook(tmp_path, monkeypatch):
    task, cwd, token = pending_task(tmp_path, monkeypatch)
    store.upsert("fresh", provider="codex", cwd=str(cwd))
    entered, proceed = threading.Event(), threading.Event()
    original = projects.resolve_project
    failures = []

    def slow_resolution(path):
        if threading.current_thread() is worker and not entered.is_set():
            entered.set()
            assert proceed.wait(5), "test did not release project resolution"
        return original(path)

    def reconcile():
        try:
            work_items.reconcile_sessions()
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(projects, "resolve_project", slow_resolution)
    worker = threading.Thread(target=reconcile)
    worker.start()
    try:
        assert entered.wait(5), "reconciliation did not reach project resolution"
        assert launches.adopt_session("fresh", "codex")
    finally:
        proceed.set()
        worker.join(5)
    assert not worker.is_alive()
    assert failures == []

    # An old SessionStart sees a consumed token and must address its historic
    # link without manufacturing another owner or reviving archived work.
    hook.dispatch({"hook_event_name": "SessionStart", "session_id": "original", "cwd": str(cwd)}, "codex")
    capture_and_reconcile()

    assert launches.get(token)["state"] == "consumed"
    assert len(work_items.list_items(include_done=True)) == 1
    assert work_items.get_for_session("original")["task_id"] == task["task_id"]
    adopted = work_items.get(task["task_id"])
    assert adopted["session_id"] == "fresh"
    assert adopted["title"] == "Keep this title"
    assert adopted["status"] == "archived"
    assert len(work_items.get_session_history(task["task_id"])) == 2
