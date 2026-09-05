from __future__ import annotations

import sqlite3

import pytest

from claude_browse.board import projects, store, work_items


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    projects.resolve_project.cache_clear()


def _project(tmp_path, session_id: str) -> dict:
    store.upsert(session_id, cwd=str(tmp_path / session_id), name=session_id)
    work_items.ensure_for_session(store.get(session_id))
    return next(
        project
        for project in work_items.list_projects()
        if project["project_key"] == f"path:{tmp_path / session_id}"
    )


def test_project_alias_folder_and_description_are_local_metadata(tmp_path):
    project = _project(tmp_path, "one")
    folder = work_items.create_folder("  Active work  ")

    updated = work_items.update_project(
        project["project_key"],
        display_name="  Widget launch  ",
        folder_id=folder["folder_id"],
        description="  Keep this local  ",
    )

    assert updated["name"] == "Widget launch"
    assert updated["display_name"] == "Widget launch"
    assert updated["folder_id"] == folder["folder_id"]
    assert updated["description"] == "Keep this local"
    described = work_items.set_project_description(
        project["project_key"], "Updated description"
    )
    assert described["display_name"] == "Widget launch"
    assert described["folder_id"] == folder["folder_id"]
    before = dict(described)
    with pytest.raises(ValueError, match="folder not found"):
        work_items.update_project(project["project_key"], folder_id="missing")
    assert work_items.list_projects() == [before]
    with pytest.raises(ValueError, match="project_key must be a string"):
        work_items.update_project(False, display_name="Nope")
    with pytest.raises(ValueError, match="display_name must be a string"):
        work_items.update_project(project["project_key"], display_name=False)
    with pytest.raises(ValueError, match="120"):
        work_items.update_project(project["project_key"], display_name="x" * 121)
    assert work_items.update_project(
        project["project_key"], display_name="", folder_id=None
    )["name"] == "one"


def test_folders_validate_and_reorder_atomically(tmp_path):
    first = work_items.create_folder("First")
    second = work_items.create_folder("Second")
    before = work_items.list_folders()

    with pytest.raises(ValueError):
        work_items.create_folder(False)
    with pytest.raises(ValueError):
        work_items.update_folder(first["folder_id"], 2)
    with pytest.raises(ValueError):
        work_items.update_folder(False, "Nope")
    with pytest.raises(ValueError):
        work_items.reorder_folders([first["folder_id"], False])
    with pytest.raises(ValueError):
        work_items.reorder_folders([first["folder_id"], "missing"])
    assert work_items.list_folders() == before

    reordered = work_items.reorder_folders([second["folder_id"], first["folder_id"]])
    assert [folder["folder_id"] for folder in reordered] == [
        second["folder_id"],
        first["folder_id"],
    ]


def test_project_preferences_survive_hook_reconciliation(tmp_path, monkeypatch):
    project = _project(tmp_path, "nested")
    folder = work_items.create_folder("Pinned")
    work_items.update_project(
        project["project_key"],
        display_name="A better name",
        folder_id=folder["folder_id"],
        description="Still mine",
    )
    monkeypatch.setattr(
        projects,
        "resolve_project",
        lambda _cwd: {
            "key": "repo:example/widget",
            "name": "widget",
            "path": str(tmp_path),
        },
    )

    assert work_items.reconcile_sessions() == 1
    reconciled = work_items.list_projects()[0]
    assert reconciled["name"] == "A better name"
    assert reconciled["display_name"] == "A better name"
    assert reconciled["folder_id"] == folder["folder_id"]
    assert reconciled["description"] == "Still mine"


def test_reconciliation_does_not_clobber_destination_preferences(tmp_path, monkeypatch):
    source = _project(tmp_path, "source")
    source_folder = work_items.create_folder("Source")
    destination_folder = work_items.create_folder("Destination")
    work_items.update_project(
        source["project_key"],
        description="Source description",
        display_name="Source alias",
        folder_id=source_folder["folder_id"],
    )
    destination = _project(tmp_path, "destination")
    with store.get_conn() as conn:
        conn.execute(
            "UPDATE work_items SET project_key = ?, project_resolved = 1 WHERE project_key = ?",
            ("repo:example/widget", destination["project_key"]),
        )
        conn.execute(
            """INSERT INTO project_settings
               (project_key, description, position, updated_at, display_name, folder_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "repo:example/widget",
                "Destination description",
                1,
                1,
                "Destination alias",
                destination_folder["folder_id"],
            ),
        )
    monkeypatch.setattr(
        projects,
        "resolve_project",
        lambda _cwd: {
            "key": "repo:example/widget",
            "name": "widget",
            "path": str(tmp_path),
        },
    )

    assert work_items.reconcile_sessions() == 1
    reconciled = work_items.list_projects()[0]
    assert reconciled["name"] == "Destination alias"
    assert reconciled["folder_id"] == destination_folder["folder_id"]
    assert reconciled["description"] == "Destination description\n\nSource description"


def test_sidebar_migration_is_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(work_items._SCHEMA)
    conn.execute(
        """CREATE TABLE project_settings (
               project_key TEXT PRIMARY KEY,
               description TEXT NOT NULL DEFAULT '',
               position INTEGER NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO project_settings VALUES ('repo:example/widget', 'Keep', 1, 2)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "_DB_PATH", db)
    monkeypatch.setattr(store, "_conn_cache", None)

    conn = work_items._conn()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(project_settings)")}
    assert {"display_name", "folder_id"} <= columns
    assert tuple(conn.execute(
        "SELECT description, display_name, folder_id FROM project_settings"
    ).fetchone()) == ("Keep", "", None)
    assert work_items.sidebar_migration_backup_path().exists()

    conn.close()
    store._conn_cache = None
    assert tuple(work_items._conn().execute(
        "SELECT description, display_name, folder_id FROM project_settings"
    ).fetchone()) == ("Keep", "", None)
