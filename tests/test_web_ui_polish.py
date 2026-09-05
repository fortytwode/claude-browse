"""Focused static evidence for the ClickUp-canonical local board surface."""

from pathlib import Path

ASSETS = Path(__file__).parents[1] / "claude_browse" / "webassets"


def test_clickup_board_has_contextual_rows_and_dialog_conversation():
    html = (ASSETS / "index.html").read_text()
    js = (ASSETS / "app.js").read_text()

    for element_id in (
        "folder-list",
        "saved-view-list",
        "task-dialog",
        "conversation-host",
        "filter-provider",
        "filter-priority",
        "filter-terminal",
        "filter-due",
        "sort-by",
    ):
        assert f'id="{element_id}"' in html

    assert "Last update" in js
    assert "Open details" in js
    assert '$("conversation-host").appendChild(viewer)' in js
    assert '$("history-view").appendChild($("viewer"))' in js
    assert "transcript_error" in js
    assert "No date" in js


def test_manual_reorder_is_safe_while_priority_edits_stay_patch_based():
    js = (ASSETS / "app.js").read_text()

    assert "function queueReorder" in js
    assert 'queueReorder("tasks:" + task.project_key' in js
    assert "visibleTasks().filter" in js
    assert "Manual reordering is disabled while filters are applied." in js
    assert 'saveTask(task, "priority", next)' in js
    assert "Closed rows cannot change priority by dragging." in js
    assert "Terminal state is runtime truth" in js


def test_saved_views_are_versioned_browser_local_and_defensively_read():
    js = (ASSETS / "app.js").read_text()

    assert 'SAVED_VIEWS_KEY = "agent-board.saved-views.v1"' in js
    assert "localStorage.getItem" in js
    assert "localStorage.setItem" in js
    assert "validSavedViews" in js
    assert "Saved views are unavailable in this browser." in js
