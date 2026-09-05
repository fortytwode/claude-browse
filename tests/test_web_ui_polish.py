"""Focused static evidence for the ClickUp-canonical local board surface."""

from html.parser import HTMLParser
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
        "filter-presence",
        "filter-last-update",
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
    assert 'queueReorder("tasks:" + taskProjectKey(task)' in js
    assert "visibleTasks().filter" in js
    assert "Manual reordering is disabled while filters are applied." in js
    assert 'saveTask(task, "priority", priority)' in js
    assert "Closed rows cannot change priority by dragging." in js
    assert "Terminal state is runtime truth" in js
    assert 'placement: placement' in js


def test_saved_views_are_versioned_browser_local_and_defensively_read():
    js = (ASSETS / "app.js").read_text()

    assert 'SAVED_VIEWS_KEY = "agent-board.saved-views.v1"' in js
    assert "localStorage.getItem" in js
    assert "localStorage.setItem" in js
    assert "validSavedViews" in js
    assert "Saved views are unavailable in this browser." in js


def test_workspace_navigation_and_launch_controls_are_concrete_not_legacy_aliases():
    html = (ASSETS / "index.html").read_text()
    js = (ASSETS / "app.js").read_text()
    css = (ASSETS / "app.css").read_text()

    for element_id in (
        "new-space",
        "list-claude",
        "list-codex",
        "task-history-select",
        "task-claude",
        "task-codex",
    ):
        assert f'id="{element_id}"' in html

    assert '"/api/workspace/tasks/" + encodeURIComponent(task.task_id) + "/move"' in js
    assert "expected_list_key" in js
    assert '"/api/tasks/" + encodeURIComponent(task.task_id) + "/history"' in js
    assert "launch_revision" in js
    assert "workspaceMenu(\"space\"" in js
    assert "workspaceMenu(\"folder\"" in js
    assert "workspaceMenu(\"list\"" in js
    assert ".space-icon" in css
    assert ".folder-icon" in css
    assert ".list-icon" in css


def test_work_tabs_close_before_toolbar_and_status_is_a_single_toolbar_filter():
    html = (ASSETS / "index.html").read_text()

    class DivParents(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.parents = {}

        def handle_starttag(self, tag, attrs):
            if tag not in {"div", "section"}:
                return
            values = dict(attrs)
            marker = values.get("id") or values.get("class")
            self.parents[marker] = self.stack[-1] if self.stack else None
            self.stack.append(marker)

        def handle_endtag(self, tag):
            if tag in {"div", "section"}:
                self.stack.pop()

    parser = DivParents()
    parser.feed(html)
    assert parser.parents["board-toolbar"] == "work-main"
    assert 'class="work-scopes"' not in html
    assert 'id="filter-status"' in html
    assert 'data-scope="open"' in html
    assert 'data-scope="all"' in html
    assert 'data-scope="today"' in html


def test_undo_toast_is_interactive_only_while_visible():
    css = (ASSETS / "app.css").read_text()

    assert "#toast.show" in css
    assert "pointer-events: auto" in css


def test_project_detail_stacks_actions_before_the_sidebar_narrows_title_space():
    css = (ASSETS / "app.css").read_text()
    narrow = css.split("@media (max-width: 900px)", 1)[1].split("@media (max-width: 680px)", 1)[0]

    assert ".project-detail" in narrow
    assert "grid-template-columns: minmax(0, 1fr)" in narrow
    assert ".project-detail-actions" in narrow
    assert "flex-wrap: wrap" in narrow
    assert "#list-launch-actions" in narrow


def test_workspace_tree_keeps_folder_labels_compact_and_list_counts_trailing():
    css = (ASSETS / "app.css").read_text()

    assert ".folder-select {\n  justify-content: flex-start;" in css
    assert ".project-select .project-name {\n  margin-right: auto;" in css


def test_open_terminal_surface_uses_presence_and_fresh_start_contracts():
    html = (ASSETS / "index.html").read_text()
    js = (ASSETS / "app.js").read_text()
    css = (ASSETS / "app.css").read_text()

    assert "function taskPresence(task)" in js
    assert 'queueMode = "open"' in js
    assert 'endpoint: "start"' in js
    assert '"/api/tasks/" + encodeURIComponent(task.task_id) + "/" + mode.endpoint' in js
    assert 'actionField: "start_actions"' in js
    assert 'id="task-fresh-claude"' in html
    assert 'id="task-fresh-codex"' in html
    assert ".presence-open" in css
    assert ".priority-options" in css
