from __future__ import annotations

import pytest

from claude_browse.board import projects, store, work_items, workspace


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(store, "_conn_cache", None)
    projects.resolve_project.cache_clear()


def _task(tmp_path, session_id: str, *, cwd=None, name=None):
    path = cwd or tmp_path / session_id
    path.mkdir(parents=True, exist_ok=True)
    store.upsert(session_id, cwd=str(path), name=name or session_id, provider="claude")
    return work_items.ensure_for_session(store.get(session_id))


def test_snapshot_migrates_legacy_project_preferences_once(tmp_path):
    task = _task(tmp_path, "legacy")
    legacy_folder = work_items.create_folder("Legacy folder")
    work_items.update_project(
        task["project_key"],
        display_name="Renamed legacy list",
        description="Keep this description",
        folder_id=legacy_folder["folder_id"],
    )

    first = workspace.snapshot()
    second = workspace.snapshot()

    assert first == second
    assert first["spaces"] == [{"space_id": "space:general", "name": "General", "position": 1}]
    assert first["folders"] == [{
        "folder_id": legacy_folder["folder_id"], "space_id": "space:general",
        "name": "Legacy folder", "position": legacy_folder["position"],
    }]
    imported = first["lists"][0]
    assert imported["list_key"] == task["project_key"]
    assert imported["source_project_key"] == task["project_key"]
    assert imported["name"] == "Renamed legacy list"
    assert imported["description"] == "Keep this description"
    assert imported["folder_id"] == legacy_folder["folder_id"]
    assert work_items.get(task["task_id"])["project_key"] == task["project_key"]


def test_hierarchy_updates_validate_membership_and_move_children(tmp_path):
    first = workspace.create_space("First")
    second = workspace.create_space("Second")
    folder = workspace.create_folder("Work", first["space_id"])
    listed = workspace.create_list("Plan", first["space_id"], folder["folder_id"])

    moved = workspace.update_folder(folder["folder_id"], space_id=second["space_id"])
    assert moved["space_id"] == second["space_id"]
    assert workspace.context_for_list(listed["list_key"])["space_id"] == second["space_id"]
    with pytest.raises(ValueError, match="folder must belong"):
        workspace.update_list(listed["list_key"], space_id=first["space_id"])
    with pytest.raises(ValueError, match="space not found"):
        workspace.create_folder("Nope", "missing")
    with pytest.raises(ValueError, match="name must be a string"):
        workspace.create_space(False)


def test_working_directory_link_and_creation_are_strict(tmp_path):
    space = workspace.create_space("Space")
    listed = workspace.create_list("Unlinked", space["space_id"])
    parent = tmp_path / "parent"
    parent.mkdir()
    created = workspace.create_working_directory(listed["list_key"], str(parent / "new"))
    assert created["working_directory"] == str(parent / "new")
    assert (parent / "new").is_dir()
    with pytest.raises(ValueError, match="already exists"):
        workspace.create_working_directory(listed["list_key"], str(parent / "new"))
    link = parent / "link"
    link.symlink_to(parent / "new")
    with pytest.raises(ValueError, match="already exists"):
        workspace.create_working_directory(listed["list_key"], str(link))
    broken_link = parent / "broken-link"
    broken_link.symlink_to(parent / "not-created")
    with pytest.raises(ValueError, match="already exists"):
        workspace.create_working_directory(listed["list_key"], str(broken_link))
    with pytest.raises(ValueError, match="working_directory must exist"):
        workspace.update_list(listed["list_key"], working_directory=str(parent / "missing"))


def test_creating_directory_for_imported_list_stops_cwd_inheritance(tmp_path):
    source = tmp_path / "repository" / "nested"
    task = _task(tmp_path, "imported", cwd=source)
    listed = workspace.snapshot()["lists"][0]
    before_context = workspace.context_for_task(task)
    before_task = work_items.get(task["task_id"])
    parent = tmp_path / "linked-parent"
    parent.mkdir()
    expected_directory = str((parent / "new-directory").resolve())

    created = workspace.create_working_directory(listed["list_key"], str(parent / "new-directory"))
    context = workspace.context_for_task(task)
    current_task = work_items.get(task["task_id"])

    assert created["working_directory"] == expected_directory
    assert context["working_directory"] == expected_directory
    assert context["launch_revision"] != before_context["launch_revision"]
    assert {field: current_task[field] for field in ("project_key", "project_path", "session_cwd")} == {
        field: before_task[field] for field in ("project_key", "project_path", "session_cwd")
    }


def _assert_sibling_move_round_trip(kind, identifier, records):
    before = records()
    before_ids = [item[identifier] for item in before]

    boundary = workspace.move_node(kind, before_ids[0], -1)
    assert boundary[identifier] == before_ids[0]
    assert records() == before

    target = before_ids[1]
    moved = workspace.move_node(kind, target, 1)
    expected = before_ids[:]
    expected[1], expected[2] = expected[2], expected[1]
    assert moved[identifier] == target
    assert [item[identifier] for item in records()] == expected
    assert [item["position"] for item in records()] == list(range(1, len(before) + 1))

    workspace.move_node(kind, target, -1)
    assert [item[identifier] for item in records()] == before_ids
    assert [item["position"] for item in records()] == list(range(1, len(before) + 1))


def test_move_space_swaps_and_normalizes_tied_positions():
    first = workspace.create_space("First")
    second = workspace.create_space("Second")
    third = workspace.create_space("Third")
    workspace.update_space("space:general", position=1)
    workspace.update_space(first["space_id"], position=10)
    workspace.update_space(second["space_id"], position=10)
    workspace.update_space(third["space_id"], position=20)

    _assert_sibling_move_round_trip(
        "space",
        "space_id",
        lambda: workspace.snapshot()["spaces"],
    )


def test_move_folder_scopes_siblings_to_its_space():
    first_space = workspace.create_space("First")
    second_space = workspace.create_space("Second")
    first = workspace.create_folder("First", first_space["space_id"])
    second = workspace.create_folder("Second", first_space["space_id"])
    third = workspace.create_folder("Third", first_space["space_id"])
    outside = workspace.create_folder("Outside", second_space["space_id"])
    workspace.update_folder(first["folder_id"], position=10)
    workspace.update_folder(second["folder_id"], position=10)
    workspace.update_folder(third["folder_id"], position=20)
    workspace.update_folder(outside["folder_id"], position=777)
    outside_before = workspace.update_folder(outside["folder_id"])

    _assert_sibling_move_round_trip(
        "folder",
        "folder_id",
        lambda: [
            folder for folder in workspace.snapshot()["folders"]
            if folder["space_id"] == first_space["space_id"]
        ],
    )

    assert workspace.update_folder(outside["folder_id"])["position"] == outside_before["position"]


def test_move_root_list_scopes_siblings_to_space_and_root_parent():
    first_space = workspace.create_space("First")
    second_space = workspace.create_space("Second")
    first = workspace.create_list("First", first_space["space_id"])
    second = workspace.create_list("Second", first_space["space_id"])
    third = workspace.create_list("Third", first_space["space_id"])
    folder = workspace.create_folder("Folder", first_space["space_id"])
    nested = workspace.create_list("Nested", first_space["space_id"], folder["folder_id"])
    outside = workspace.create_list("Outside", second_space["space_id"])
    workspace.update_list(first["list_key"], position=10)
    workspace.update_list(second["list_key"], position=10)
    workspace.update_list(third["list_key"], position=20)
    workspace.update_list(nested["list_key"], position=444)
    workspace.update_list(outside["list_key"], position=777)
    nested_before = workspace.update_list(nested["list_key"])
    outside_before = workspace.update_list(outside["list_key"])

    _assert_sibling_move_round_trip(
        "list",
        "list_key",
        lambda: [
            listed for listed in workspace.snapshot()["lists"]
            if listed["space_id"] == first_space["space_id"] and listed["folder_id"] is None
        ],
    )

    assert workspace.update_list(nested["list_key"])["position"] == nested_before["position"]
    assert workspace.update_list(outside["list_key"])["position"] == outside_before["position"]


def test_move_folder_list_scopes_siblings_to_its_folder():
    space = workspace.create_space("Space")
    first_folder = workspace.create_folder("First folder", space["space_id"])
    second_folder = workspace.create_folder("Second folder", space["space_id"])
    first = workspace.create_list("First", space["space_id"], first_folder["folder_id"])
    second = workspace.create_list("Second", space["space_id"], first_folder["folder_id"])
    third = workspace.create_list("Third", space["space_id"], first_folder["folder_id"])
    outside = workspace.create_list("Outside", space["space_id"], second_folder["folder_id"])
    root = workspace.create_list("Root", space["space_id"])
    workspace.update_list(first["list_key"], position=10)
    workspace.update_list(second["list_key"], position=10)
    workspace.update_list(third["list_key"], position=20)
    workspace.update_list(outside["list_key"], position=777)
    workspace.update_list(root["list_key"], position=444)
    outside_before = workspace.update_list(outside["list_key"])
    root_before = workspace.update_list(root["list_key"])

    _assert_sibling_move_round_trip(
        "list",
        "list_key",
        lambda: [
            listed for listed in workspace.snapshot()["lists"]
            if listed["space_id"] == space["space_id"]
            and listed["folder_id"] == first_folder["folder_id"]
        ],
    )

    assert workspace.update_list(outside["list_key"])["position"] == outside_before["position"]
    assert workspace.update_list(root["list_key"])["position"] == root_before["position"]


@pytest.mark.parametrize(
    ("kind", "node_id", "direction", "message"),
    [
        ("unknown", "node", 1, "kind must"),
        ("space", "node", 0, "direction must"),
        ("space", "node", 2, "direction must"),
        ("space", "node", True, "direction must"),
        ("space", "node", "1", "direction must"),
        ("space", None, 1, "node_id must be a string"),
        ("space", "missing", 1, "space not found"),
    ],
)
def test_move_node_rejects_invalid_requests(kind, node_id, direction, message):
    with pytest.raises(ValueError, match=message):
        workspace.move_node(kind, node_id, direction)


def test_task_context_move_cas_and_reorder_leave_source_identity_intact(tmp_path):
    source = tmp_path / "repo" / "nested"
    first = _task(tmp_path, "one", cwd=source)
    second = _task(tmp_path, "two", cwd=tmp_path / "repo" / "other")
    imported = workspace.snapshot()["lists"][0]
    initial = workspace.context_for_task(first)
    assert initial["list_key"] == first["project_key"]
    assert initial["working_directory"] == str(source)
    assert initial["folder_status"] == "ready"

    space = workspace.create_space("Target")
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    destination = workspace.create_list(
        "Destination", space["space_id"], working_directory=str(target_dir)
    )
    moved = workspace.move_task(first["task_id"], destination["list_key"], imported["list_key"])
    assert moved["working_directory"] == str(target_dir)
    with pytest.raises(ValueError, match="current list"):
        workspace.move_task(first["task_id"], imported["list_key"], imported["list_key"])
    workspace.move_task(first["task_id"], imported["list_key"], destination["list_key"])
    assert workspace.context_for_task(first)["working_directory"] == str(source)
    assert work_items.get(first["task_id"])["project_key"] == first["project_key"]

    workspace.move_task(first["task_id"], destination["list_key"], imported["list_key"])
    workspace.move_task(second["task_id"], destination["list_key"], second["project_key"])
    reordered = workspace.reorder_tasks(
        destination["list_key"], [second["task_id"], first["task_id"]], priority="high"
    )
    assert [item["task_id"] for item in reordered] == [second["task_id"], first["task_id"]]
    assert {item["priority"] for item in reordered} == {"high"}


def test_context_order_tracks_reorder_for_moved_and_native_tasks(tmp_path):
    native = _task(tmp_path, "native")
    moved = _task(tmp_path, "moved")
    destination = workspace.snapshot()["lists"][0]
    assert destination["list_key"] == native["project_key"]
    workspace.move_task(moved["task_id"], destination["list_key"], moved["project_key"])
    before = sorted([
        workspace.context_for_task(native)["order"],
        workspace.context_for_task(moved)["order"],
    ])

    workspace.reorder_tasks(
        destination["list_key"], [moved["task_id"], native["task_id"]]
    )

    assert workspace.context_for_task(moved)["order"] == before[0]
    assert workspace.context_for_task(native)["order"] == before[1]


def test_explicitly_unlinking_an_imported_list_stops_cwd_inheritance(tmp_path):
    task = _task(tmp_path, "nested", cwd=tmp_path / "repo" / "nested")
    imported = workspace.snapshot()["lists"][0]
    assert imported["working_directory"] == task["project_path"]
    assert workspace.context_for_task(task)["working_directory"] == task["session_cwd"]

    workspace.update_list(imported["list_key"], working_directory=None)

    context = workspace.context_for_task(task)
    assert context["working_directory"] is None
    assert context["folder_status"] == "unlinked"


def test_seeded_contexts_skip_the_full_snapshot(monkeypatch, tmp_path):
    task = _task(tmp_path, "seeded")
    listed = workspace.snapshot()["lists"][0]
    monkeypatch.setattr(
        workspace,
        "snapshot",
        lambda: pytest.fail("seeded contexts must not load the full catalog"),
    )

    assert workspace.context_for_task(task, seeded=True)["list_key"] == listed["list_key"]
    assert workspace.context_for_list(listed["list_key"], seeded=True)["list_key"] == listed["list_key"]


def test_continuation_keeps_one_task_and_historical_hooks_canonical(tmp_path):
    original = _task(tmp_path, "old-session", name="Keep this task title")
    destination = tmp_path / "destination"
    destination.mkdir()
    store.upsert("new-session", cwd=str(destination), name="new", provider="codex")

    attached = work_items.attach_continuation(
        original["task_id"], store.get("new-session"), "old-session"
    )
    assert attached["task_id"] == original["task_id"]
    assert attached["session_id"] == "new-session"
    assert attached["session_provider"] == "codex"
    assert work_items.ensure_for_session(store.get("new-session"))["title"] == "Keep this task title"
    assert [entry["session_id"] for entry in work_items.get_session_history(original["task_id"])] == [
        "old-session", "new-session"
    ]
    assert work_items.get_for_session("old-session")["task_id"] == original["task_id"]
    assert work_items.ensure_for_session(store.get("old-session"))["task_id"] == original["task_id"]
    assert work_items.reconcile_sessions() == 0
    assert len(work_items.list_items(include_done=True)) == 1
    with pytest.raises(ValueError, match="stale"):
        work_items.attach_continuation(original["task_id"], store.get("new-session"), "old-session")
    with pytest.raises(ValueError, match="already linked"):
        work_items.attach_continuation(original["task_id"], store.get("old-session"), "new-session")

    other = _task(tmp_path, "other-session")
    with pytest.raises(ValueError, match="another task"):
        work_items.attach_continuation(other["task_id"], store.get("new-session"), "other-session")
