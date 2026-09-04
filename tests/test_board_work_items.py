from __future__ import annotations

import subprocess

import pytest

from claude_browse.board import commands, hook, projects, store, work_items


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    projects.resolve_project.cache_clear()


def test_work_item_crud_and_done_filter(tmp_path):
    task = work_items.create(
        title="Plan release",
        project_path=tmp_path,
        due_date="2026-09-05",
        provider="claude",
    )
    assert task["project_name"] == tmp_path.name
    assert task["status"] == "todo"
    assert [row["task_id"] for row in work_items.list_items()] == [task["task_id"]]

    done = work_items.update(task["task_id"], title="Release", status="done")
    assert done["title"] == "Release"
    assert done["completed_at"] is not None
    assert work_items.list_items() == []
    assert work_items.list_items(include_done=True)[0]["task_id"] == task["task_id"]


def test_work_item_validation_and_one_task_per_session(tmp_path):
    with pytest.raises(ValueError, match="title is required"):
        work_items.create(title="", project_path=tmp_path)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.create(title="x", project_path=tmp_path, due_date="tomorrow")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.create(title="x", project_path=tmp_path, due_date="20260905")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        work_items.create(title="x", project_path=tmp_path, due_date="2026-W36-6")
    with pytest.raises(ValueError, match="status must"):
        work_items.create(title="x", project_path=tmp_path, status="urgent")

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


def test_resume_commands_are_cwd_qualified_and_provider_correct(tmp_path):
    claude = commands.resume_command("abc", "claude", str(tmp_path), full_access=True)
    codex = commands.resume_command("xyz", "codex", str(tmp_path), full_access=True)
    safe = commands.resume_command("abc", "claude", str(tmp_path), full_access=False)
    assert claude == f"cd -- {tmp_path} && claude --resume abc --dangerously-skip-permissions"
    assert codex == f"cd -- {tmp_path} && codex resume xyz --dangerously-bypass-approvals-and-sandbox"
    assert "dangerously" not in safe


def test_attach_session_links_a_started_task(tmp_path):
    task = work_items.create(title="new", project_path=tmp_path, provider="claude")
    linked = work_items.attach_session(task["task_id"], "new-session", "codex")
    assert linked["session_id"] == "new-session"
    assert linked["session_provider"] == "codex"


def test_cross_provider_continuation_preserves_context_and_task_link(tmp_path, monkeypatch):
    import claude_browse.browse as browse

    import_file = tmp_path / "handoff.md"
    import_file.write_text("context")
    monkeypatch.setattr(browse, "write_import_file", lambda *_args: str(import_file))
    command = commands.continue_command(
        {
            "session_id": "old-session",
            "provider": "claude",
            "cwd": str(tmp_path),
            "path": str(tmp_path / "thread.jsonl"),
        },
        "codex",
        full_access=True,
        task_id="task-123",
    )
    assert "AGENT_BOARD_TASK_ID=task-123" in command
    assert str(import_file) in command
    assert "--dangerously-bypass-approvals-and-sandbox" in command


def test_session_start_hook_links_task_from_launch_environment(tmp_path, monkeypatch):
    task = work_items.create(title="new", project_path=tmp_path, provider="claude")
    monkeypatch.setenv("AGENT_BOARD_TASK_ID", task["task_id"])
    assert hook.dispatch(
        {"hook_event_name": "SessionStart", "session_id": "created-session", "cwd": str(tmp_path)},
        provider="codex",
    )
    linked = work_items.get(task["task_id"])
    assert linked["session_id"] == "created-session"
    assert linked["session_provider"] == "codex"


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
