// Agent Board local web app. Vanilla JS, no build step and no network assets.
(function () {
  "use strict";
  var csrfToken = "", activeSid = null, activeSessionMeta = null;
  var queueMode = "all", groupBy = "priority", selectedProject = null;
  var latestBoard = null, currentTurns = null, draggedTask = null, draggedProject = null;
  var sessionsSeq = 0, boardTimer = null, editSequence = 0;
  var rowMutationTails = Object.create(null), editStates = [];
  var PRIORITY_GROUPS = ["urgent", "high", "normal", "low"];
  var TERMINAL_GROUPS = ["needs-input", "working", "idle", "ended", "gone"];
  var PRIORITY_LABELS = { urgent: "Urgent", high: "High", normal: "Normal", low: "Low" };
  var TERMINAL_LABELS = { "needs-input": "Needs input", working: "Working", idle: "Idle", ended: "Ended", gone: "Gone" };

  function $(id) { return document.getElementById(id); }
  function element(tag, className, textValue) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (textValue !== undefined) node.textContent = textValue;
    return node;
  }
  function request(path, options) {
    options = options || {}; options.headers = options.headers || {};
    if (options.body) {
      options.headers["Content-Type"] = "application/json";
      options.headers["X-Agent-Board-Token"] = csrfToken;
    }
    return fetch(path, options).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok || data.error) throw new Error(data.error || "Request failed");
        return data;
      });
    });
  }
  function mutate(path, method, payload, keepalive) {
    return request(path, { method: method, body: JSON.stringify(payload || {}), keepalive: Boolean(keepalive) });
  }
  function toast(message, isError) {
    var node = $("toast"); node.textContent = message;
    node.className = isError ? "show error-toast" : "show";
    setTimeout(function () { node.className = ""; }, 2800);
  }
  function announce(message) { $("work-announcer").textContent = ""; setTimeout(function () { $("work-announcer").textContent = message; }, 10); }
  function emptyMessage(root, message) { root.replaceChildren(element("div", "empty-card", message)); }
  function debounce(fn, wait) {
    var timer = null;
    return function () { var args = arguments; clearTimeout(timer); timer = setTimeout(function () { fn.apply(null, args); }, wait); };
  }
  function escapeHtml(value) { return String(value || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function inlineFormat(value) { return value.replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>"); }
  function renderTurnBody(text) {
    var codeRe = /```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, parts = [], lastIndex = 0, match;
    while ((match = codeRe.exec(text)) !== null) {
      if (match.index > lastIndex) parts.push({ type: "prose", text: text.slice(lastIndex, match.index) });
      parts.push({ type: "code", text: match[2] }); lastIndex = codeRe.lastIndex;
    }
    if (lastIndex < text.length) parts.push({ type: "prose", text: text.slice(lastIndex) });
    return parts.map(function (part) {
      if (part.type === "code") return "<pre><code>" + escapeHtml(part.text) + "</code></pre>";
      return part.text.split(/\n{2,}/).filter(function (paragraph) { return paragraph.trim(); }).map(function (paragraph) { return "<p>" + inlineFormat(escapeHtml(paragraph)) + "</p>"; }).join("");
    }).join("");
  }
  function relativeTime(timestamp) {
    if (!timestamp) return "Unknown";
    var seconds = Math.max(0, Date.now() / 1000 - Number(timestamp));
    if (seconds < 60) return "now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    return Math.floor(seconds / 86400) + "d ago";
  }
  function dueLabel(value) {
    if (!value) return "No due date";
    var today = new Date(); today.setHours(0, 0, 0, 0);
    var due = new Date(value + "T00:00:00"), days = Math.round((due - today) / 86400000);
    if (days < 0) return "Overdue: " + value;
    if (days === 0) return "Due today";
    if (days === 1) return "Due tomorrow";
    return "Due " + value;
  }
  function providerName(provider) { return provider === "codex" ? "CodeX" : "Claude"; }
  function fullAccessEnabled() { return $("full-access").checked; }

  function hasProtectedWorkControls() {
    var active = document.activeElement;
    var focused = Boolean(active && $("board-view").contains(active) && active.matches(".work-edit, #project-description"));
    return focused || editStates.some(function (state) { return Boolean(state.timer || state.inFlight || state.failed); });
  }
  function mergeSavedTask(saved) {
    if (!latestBoard || !saved) return;
    latestBoard.tasks = latestBoard.tasks.map(function (task) { return task.task_id === saved.task_id ? Object.assign({}, task, saved) : task; });
  }
  function setEditError(state, error) {
    state.failed = true;
    state.control.setAttribute("aria-invalid", "true");
    state.error.hidden = false;
    state.error.textContent = "Could not save: " + error.message + ". Edit again to retry.";
    toast(error.message, true);
  }
  function clearEditError(state) {
    state.failed = false; state.control.removeAttribute("aria-invalid");
    state.error.hidden = true; state.error.textContent = "";
  }
  function commitEdit(state, keepalive) {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    var revision = state.revision, payload = {}; payload[state.field] = state.readValue();
    state.inFlight = true; state.control.setAttribute("aria-busy", "true");
    var prior = rowMutationTails[state.taskId] || Promise.resolve();
    var operation = prior.catch(function () {}).then(function () {
      return mutate("/api/tasks/" + encodeURIComponent(state.taskId), "PATCH", payload, keepalive);
    });
    rowMutationTails[state.taskId] = operation;
    operation.then(function (response) {
      mergeSavedTask(response.task); if (state.revision === revision) clearEditError(state);
    }).catch(function (error) {
      if (state.revision === revision) setEditError(state, error);
    }).finally(function () {
      if (state.revision === revision) { state.inFlight = false; state.control.removeAttribute("aria-busy"); }
    });
    return operation.catch(function () {});
  }
  function scheduleEdit(state, delay) {
    state.revision += 1; if (state.timer) clearTimeout(state.timer);
    state.control.setAttribute("aria-busy", "true");
    state.timer = setTimeout(function () { commitEdit(state, false); }, delay);
  }
  function registerEdit(task, field, control, readValue, delay) {
    var error = element("span", "field-error"), errorId = "edit-error-" + (++editSequence);
    error.id = errorId; error.hidden = true; error.setAttribute("role", "alert"); error.setAttribute("aria-live", "assertive");
    control.setAttribute("aria-describedby", errorId);
    var state = { taskId: task.task_id, field: field, control: control, error: error, readValue: readValue, timer: null, inFlight: false, failed: false, revision: 0 };
    editStates.push(state);
    control.addEventListener(field === "title" ? "input" : "change", function () { scheduleEdit(state, delay); });
    control.addEventListener("blur", function () { if (!hasProtectedWorkControls()) fetchBoard(); });
    return error;
  }
  function flushPendingEdits(keepalive) {
    return Promise.all(editStates.filter(function (state) { return Boolean(state.timer); }).map(function (state) { return commitEdit(state, keepalive); }));
  }

  function launchChoice(task, provider) {
    var choice = element("div", "launch-choice"), button = element("button", "launch " + provider);
    var reason = element("span", "disabled-reason"), action = task.actions[provider], reasonId = "launch-reason-" + (++editSequence);
    reason.id = reasonId; button.type = "button"; button.textContent = action.label; button.disabled = !action.available;
    if (!action.available) { reason.textContent = action.reason || "Unavailable"; button.setAttribute("aria-describedby", reasonId); }
    button.addEventListener("click", function () {
      button.disabled = true;
      mutate("/api/tasks/" + encodeURIComponent(task.task_id) + "/launch", "POST", { provider: provider, full_access: fullAccessEnabled() }).then(function () {
        toast("Opened " + providerName(provider) + " in Terminal");
      }).catch(function (error) { toast(error.message, true); }).finally(function () { button.disabled = !action.available; });
    });
    choice.append(button, reason); return choice;
  }
  function restoreFocus(token) {
    if (!token) return;
    var control = Array.prototype.find.call(document.querySelectorAll("[data-focus-key]"), function (item) { return item.dataset.focusKey === token; });
    if (control) control.focus();
  }
  function reorderLocked() { return Boolean($("work-search").value.trim()); }
  function updateReorderReason() {
    var reason = reorderLocked() ? "Reordering is disabled while searching." : "Drag a handle, or use Move up and Move down.";
    $("reorder-reason").textContent = reason;
  }
  function taskGroupKey(task) { return groupBy === "priority" ? task.priority : task.terminal_state; }
  function visibleTasks() {
    if (!latestBoard) return [];
    return latestBoard.tasks.filter(function (task) {
      if (!workSearchMatches(task)) return false;
      if (selectedProject && task.project_key !== selectedProject) return false;
      if (queueMode === "closed") return task.work_status === "done" || task.work_status === "archived";
      if (task.work_status !== "active") return false;
      return queueMode !== "today" || task.in_today;
    });
  }
  function sortByOrder(tasks) { return tasks.slice().sort(function (a, b) { return Number(a.order) - Number(b.order) || String(a.task_id).localeCompare(String(b.task_id)); }); }
  function mergeReorderedTasks(tasks) { tasks.forEach(mergeSavedTask); }
  function refreshAfterMutation(focus, successMessage) {
    return fetchBoard(true).then(function () { restoreFocus(focus); if (successMessage) announce(successMessage); });
  }
  function taskReorderPayload(task, destinationKey, beforeTaskId) {
    var groupTasks = sortByOrder(visibleTasks().filter(function (item) {
      return item.task_id !== task.task_id && item.project_key === task.project_key && item.work_status === task.work_status && taskGroupKey(item) === destinationKey;
    }));
    var beforeIndex = groupTasks.findIndex(function (item) { return item.task_id === beforeTaskId; });
    if (beforeIndex < 0) beforeIndex = groupTasks.length;
    groupTasks.splice(beforeIndex, 0, task);
    var payload = { project_key: task.project_key, task_ids: groupTasks.map(function (item) { return item.task_id; }) };
    if (groupBy === "priority" && queueMode !== "closed") payload.priority = destinationKey;
    return payload;
  }
  function performTaskReorder(taskId, destinationKey, beforeTaskId, focus) {
    if (reorderLocked()) { announce("Reordering is disabled while searching."); restoreFocus(focus); return; }
    var task = latestBoard.tasks.find(function (item) { return item.task_id === taskId; });
    if (!task) return;
    var beforeTask = beforeTaskId && latestBoard.tasks.find(function (item) { return item.task_id === beforeTaskId; });
    if (beforeTask && beforeTask.project_key !== task.project_key) { announce("Tasks cannot move between projects."); restoreFocus(focus); return; }
    if (beforeTask && beforeTask.work_status !== task.work_status) { announce("Done and archived rows keep their planning status while reordering."); restoreFocus(focus); return; }
    if (selectedProject && task.project_key !== selectedProject) { announce("Tasks cannot move between projects."); restoreFocus(focus); return; }
    if (queueMode === "closed" && task.work_status === "active") { announce("Done and archived work can reorder only in the closed view."); restoreFocus(focus); return; }
    if (queueMode === "closed" && groupBy === "priority" && destinationKey !== task.priority) { announce("Closed rows cannot change priority by dragging."); restoreFocus(focus); return; }
    if (groupBy === "terminal" && destinationKey !== task.terminal_state) {
      announce("Terminal state is runtime truth; tasks cannot move between terminal groups."); restoreFocus(focus); return;
    }
    var snapshot = latestBoard;
    var payload = taskReorderPayload(task, destinationKey, beforeTaskId);
    mutate("/api/tasks/reorder", "POST", payload).then(function (response) {
      mergeReorderedTasks(response.tasks || []);
      return refreshAfterMutation(focus, "Moved " + (task.title || "thread") + ".");
    }).catch(function (error) {
      latestBoard = snapshot; renderBoard(snapshot); restoreFocus(focus);
      announce("Move failed. " + error.message); toast(error.message, true);
    });
  }
  function moveTask(task, direction, focus) {
    if (reorderLocked()) { announce("Reordering is disabled while searching."); return; }
    var peers = sortByOrder(visibleTasks().filter(function (item) { return item.project_key === task.project_key && item.work_status === task.work_status && taskGroupKey(item) === taskGroupKey(task); }));
    var index = peers.findIndex(function (item) { return item.task_id === task.task_id; });
    var swap = index + direction;
    if (index < 0 || swap < 0 || swap >= peers.length) { announce("Thread is already at the " + (direction < 0 ? "top" : "bottom") + " of its group."); return; }
    var before = direction < 0 ? peers[swap].task_id : (peers[swap + 1] || {}).task_id;
    performTaskReorder(task.task_id, taskGroupKey(task), before, focus);
  }
  function performPriorityChange(task, next, focus) {
    if (reorderLocked()) { announce("Reordering is disabled while searching."); renderBoard(latestBoard); restoreFocus(focus); return; }
    var snapshot = latestBoard, destination = sortByOrder(visibleTasks().filter(function (item) { return item.task_id !== task.task_id && item.project_key === task.project_key && item.work_status === "active" && item.priority === next; }));
    destination.push(task);
    mutate("/api/tasks/reorder", "POST", { project_key: task.project_key, task_ids: destination.map(function (item) { return item.task_id; }), priority: next }).then(function (response) {
      mergeReorderedTasks(response.tasks || []); return refreshAfterMutation(focus, "Set priority " + PRIORITY_LABELS[next] + ".");
    }).catch(function (error) { latestBoard = snapshot; renderBoard(snapshot); restoreFocus(focus); announce("Priority change failed. " + error.message); toast(error.message, true); });
  }
  function prioritySelect(task) {
    var select = element("select", "priority-select work-edit");
    select.setAttribute("aria-label", "Set priority for " + task.title); select.dataset.focusKey = "priority-" + task.task_id;
    select.disabled = reorderLocked() || task.work_status !== "active";
    PRIORITY_GROUPS.forEach(function (priority) { var option = element("option", "", PRIORITY_LABELS[priority]); option.value = priority; option.selected = task.priority === priority; select.appendChild(option); });
    select.addEventListener("change", function () {
      var focus = select.dataset.focusKey, next = select.value;
      if (task.work_status !== "active") {
        select.value = task.priority; announce("Closed rows cannot change priority."); return;
      }
      performPriorityChange(task, next, focus);
    });
    return select;
  }
  function headerCell(textValue, className) { var cell = element("th", className, textValue); cell.scope = "col"; return cell; }
  function renderTaskRow(task) {
    var row = element("tr", "work-row"); row.dataset.taskId = task.task_id; row.dataset.projectKey = task.project_key; row.dataset.groupKey = taskGroupKey(task);
    var orderCell = element("td", "work-order"), orderControls = element("div", "order-controls");
    var handle = element("button", "drag-handle", "⋮⋮"); handle.type = "button"; handle.draggable = !reorderLocked(); handle.disabled = reorderLocked(); handle.setAttribute("aria-label", "Drag " + task.title); handle.dataset.focusKey = "drag-" + task.task_id;
    var up = element("button", "move-button", "↑"), down = element("button", "move-button", "↓");
    up.type = down.type = "button"; up.title = "Move up"; down.title = "Move down"; up.setAttribute("aria-label", "Move up " + task.title); down.setAttribute("aria-label", "Move down " + task.title); up.dataset.focusKey = "up-" + task.task_id; down.dataset.focusKey = "down-" + task.task_id; up.disabled = down.disabled = reorderLocked();
    handle.addEventListener("dragstart", function (event) { draggedTask = task.task_id; event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", task.task_id); });
    handle.addEventListener("dragend", function () { draggedTask = null; });
    up.addEventListener("click", function () { moveTask(task, -1, up.dataset.focusKey); }); down.addEventListener("click", function () { moveTask(task, 1, down.dataset.focusKey); });
    orderControls.append(handle, up, down); orderCell.appendChild(orderControls);
    var identity = element("td", "work-identity"), title = element("input", "task-title work-edit");
    title.value = task.title || ""; title.maxLength = 500; title.setAttribute("aria-label", "Name for " + (task.title || task.session_id));
    identity.append(title, registerEdit(task, "title", title, function () { return title.value; }, 450), element("div", "task-summary", task.summary), element("div", "task-project", task.project_name || "Unknown project"), element("div", "session-id", task.session_id));
    var priorityCell = element("td", "work-priority"); priorityCell.append(prioritySelect(task), element("span", "sr-only", "Set priority"));
    var dueCell = element("td", "work-due"), due = element("input", "work-edit");
    due.type = "date"; due.value = task.due_date || ""; due.title = dueLabel(task.due_date); due.setAttribute("aria-label", "Due date for " + task.title);
    dueCell.append(due, registerEdit(task, "due_date", due, function () { return due.value || null; }, 0));
    var statusCell = element("td", "work-status"), status = element("select", "work-edit");
    [["active", "Active"], ["done", "Done"], ["archived", "Archived"]].forEach(function (choice) {
      var option = element("option", "", choice[1]); option.value = choice[0]; option.selected = task.work_status === choice[0]; status.appendChild(option);
    });
    status.setAttribute("aria-label", "Work status for " + task.title);
    statusCell.append(status, registerEdit(task, "status", status, function () { return status.value; }, 0));
    var terminalCell = element("td", "work-terminal");
    terminalCell.appendChild(element("span", "runtime state-" + task.terminal_state, String(task.terminal_state || "gone").replace("-", " ")));
    var activityCell = element("td", "work-activity");
    activityCell.append(element("strong", "provider", providerName(task.session_provider)), element("span", "updated", relativeTime(task.last_activity_at)));
    var actionCell = element("td", "work-actions"), actions = element("div", "task-actions");
    actions.append(launchChoice(task, "claude"), launchChoice(task, "codex")); actionCell.appendChild(actions);
    row.append(orderCell, identity, priorityCell, dueCell, statusCell, terminalCell, activityCell, actionCell); return row;
  }
  function appendGroup(root, key, name, tasks) {
    var section = element("section", "project-group"), heading = element("h4", "project-heading"); section.dataset.groupKey = key;
    heading.append(element("span", "project-name", name), element("span", "count", tasks.length + (tasks.length === 1 ? " thread" : " threads")));
    var table = element("table", "work-table"), caption = element("caption", "sr-only", name + " terminal threads");
    var head = document.createElement("thead"), headRow = document.createElement("tr"), body = document.createElement("tbody");
    headRow.append(headerCell("Order", "column-order"), headerCell("Name / Project", "column-name"), headerCell("Priority", "column-priority"), headerCell("Due date", "column-due"), headerCell("Work status", "column-status"), headerCell("Terminal state", "column-terminal"), headerCell("Agent / activity", "column-activity"), headerCell("Actions", "column-actions"));
    head.appendChild(headRow); sortByOrder(tasks).forEach(function (task) { var row = renderTaskRow(task); row.addEventListener("dragover", function (event) { if (draggedTask && !reorderLocked()) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } }); row.addEventListener("drop", function (event) { event.preventDefault(); var focus = "drag-" + draggedTask; performTaskReorder(draggedTask, key, task.task_id, focus); }); body.appendChild(row); });
    section.addEventListener("dragover", function (event) { if (draggedTask && !reorderLocked()) event.preventDefault(); }); section.addEventListener("drop", function (event) { if (event.target.closest("tr")) return; event.preventDefault(); performTaskReorder(draggedTask, key, null, "drag-" + draggedTask); });
    table.append(caption, head, body); section.append(heading, table); root.appendChild(section);
  }
  function workSearchMatches(task) {
    var query = $("work-search").value.trim().toLowerCase(); if (!query) return true;
    var haystack = [task.title, task.summary, task.project_name, task.session_provider, task.session_id].join(" ").toLowerCase();
    return haystack.indexOf(query) !== -1;
  }
  function renderProjectSidebar(data) {
    $("all-count").textContent = data.tasks.filter(function (task) { return task.work_status === "active"; }).length;
    $("today-count").textContent = data.tasks.filter(function (task) { return task.work_status === "active" && task.in_today; }).length;
    $("closed-count").textContent = data.tasks.filter(function (task) { return task.work_status !== "active"; }).length;
    var list = $("project-list"); list.replaceChildren();
    data.projects.forEach(function (project) {
      var row = element("div", "project-nav-row" + (selectedProject === project.project_key ? " active" : "")); row.dataset.projectKey = project.project_key;
      var drag = element("button", "project-drag", "⋮⋮"); drag.type = "button"; drag.draggable = !reorderLocked(); drag.disabled = reorderLocked(); drag.setAttribute("aria-label", "Drag project " + project.name); drag.dataset.focusKey = "project-drag-" + project.project_key; drag.addEventListener("dragstart", function (event) { draggedProject = project.project_key; event.dataTransfer.setData("text/plain", project.project_key); }); drag.addEventListener("dragend", function () { draggedProject = null; });
      var select = element("button", "project-select"); select.type = "button"; select.setAttribute("aria-pressed", String(selectedProject === project.project_key)); select.append(element("span", "project-nav-name", project.name), element("span", "nav-count", String(project.counts.active))); select.addEventListener("click", function () { selectedProject = project.project_key; queueMode = "all"; renderBoard(latestBoard); });
      var projectMoves = element("span", "project-moves"), up = element("button", "project-move", "↑"), down = element("button", "project-move", "↓"); up.type = down.type = "button"; up.title = "Move project up"; down.title = "Move project down"; up.setAttribute("aria-label", "Move project up " + project.name); down.setAttribute("aria-label", "Move project down " + project.name); up.dataset.focusKey = "project-up-" + project.project_key; down.dataset.focusKey = "project-down-" + project.project_key; up.disabled = down.disabled = reorderLocked(); up.addEventListener("click", function () { moveProject(project.project_key, -1, up.dataset.focusKey); }); down.addEventListener("click", function () { moveProject(project.project_key, 1, down.dataset.focusKey); }); projectMoves.append(up, down);
      row.addEventListener("dragover", function (event) { if (draggedProject && !reorderLocked()) event.preventDefault(); }); row.addEventListener("drop", function (event) { event.preventDefault(); reorderProject(draggedProject, project.project_key, "project-drag-" + draggedProject); });
      row.append(drag, select, projectMoves); list.appendChild(row);
    });
  }
  function moveProject(projectKey, direction, focus) {
    var keys = latestBoard.projects.map(function (project) { return project.project_key; }), index = keys.indexOf(projectKey), swap = index + direction;
    if (swap < 0 || swap >= keys.length) { announce("Project is already at the " + (direction < 0 ? "top." : "bottom.")); return; }
    var before = direction < 0 ? keys[swap] : keys[swap + 1]; reorderProject(projectKey, before, focus);
  }
  function reorderProject(projectKey, beforeKey, focus) {
    if (!projectKey || reorderLocked()) { announce("Reordering is disabled while searching."); return; }
    var snapshot = latestBoard.projects.slice(), keys = snapshot.map(function (project) { return project.project_key; }).filter(function (key) { return key !== projectKey; });
    var index = keys.indexOf(beforeKey); keys.splice(index < 0 ? keys.length : index, 0, projectKey);
    mutate("/api/projects/reorder", "POST", { project_keys: keys }).then(function () { return fetchBoard(true); }).then(function () { restoreFocus(focus); announce("Project order saved."); }).catch(function (error) { latestBoard.projects = snapshot; renderProjectSidebar(latestBoard); restoreFocus(focus); announce("Project move failed. " + error.message); toast(error.message, true); });
  }
  function selectedProjectData() { return latestBoard && latestBoard.projects.find(function (project) { return project.project_key === selectedProject; }); }
  function renderProjectDetail() {
    var project = selectedProjectData(), detail = $("project-detail"); detail.hidden = !project; if (!project) return;
    $("project-name").textContent = project.name; $("project-path").textContent = project.path;
    $("project-counts").textContent = project.counts.active + " active · " + project.counts.today + " today · " + project.counts.needs_input + " needs input";
    var description = $("project-description"); if (document.activeElement !== description && !description.getAttribute("aria-invalid")) description.value = project.description || "";
    $("description-limit").textContent = description.value.length + " / 1000";
  }
  function renderBoard(data) {
    latestBoard = data; editStates = [];
    $("task-count").textContent = data.tasks.length + (data.tasks.length === 1 ? " thread" : " threads");
    if (selectedProject && !selectedProjectData()) selectedProject = null;
    renderProjectSidebar(data); renderProjectDetail(); updateReorderReason();
    Array.prototype.forEach.call(document.querySelectorAll(".work-scope"), function (button) { var active = button.dataset.scope === queueMode; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
    var heading = queueMode === "all" ? "All active" : queueMode === "today" ? "Today" : "Done & Archived"; if (selectedProjectData()) heading = selectedProjectData().name;
    $("work-heading").textContent = heading;
    var root = $("task-groups"); root.replaceChildren();
    var visible = visibleTasks(); $("visible-count").textContent = visible.length + (visible.length === 1 ? " thread" : " threads");
    if (!visible.length) { emptyMessage(root, "No threads match this view."); return; }
    var keys = groupBy === "priority" ? PRIORITY_GROUPS : TERMINAL_GROUPS, labels = groupBy === "priority" ? PRIORITY_LABELS : TERMINAL_LABELS;
    keys.forEach(function (key) { var tasks = visible.filter(function (task) { return taskGroupKey(task) === key; }); appendGroup(root, key, labels[key], tasks); });
  }
  function fetchBoard(force) {
    if (!force && hasProtectedWorkControls()) return Promise.resolve();
    return request("/api/board").then(function (data) { $("board-error").hidden = true; renderBoard(data); }).catch(function (error) { $("board-error").hidden = false; $("board-error").textContent = error.message; });
  }

  function fetchSessions() {
    var params = new URLSearchParams(), query = $("search-input").value.trim();
    if (query) params.set("q", query);
    if ($("here-toggle").checked) params.set("here", "1");
    var seq = ++sessionsSeq;
    request("/api/sessions?" + params.toString()).then(function (data) {
      if (seq === sessionsSeq) renderSessionList(data.sessions || []);
    }).catch(function (error) { if (seq === sessionsSeq) emptyMessage($("session-list"), error.message); });
  }
  function renderSessionList(sessions) {
    var list = $("session-list"); list.replaceChildren();
    if (!sessions.length) { emptyMessage(list, "No sessions found."); return; }
    sessions.forEach(function (session) {
      var row = element("button", "session-row" + (session.session_id === activeSid ? " active" : ""));
      var top = element("span", "session-row-top");
      top.append(element("strong", "", session.folder || "?"), element("span", "", (session.when || "") + " · " + session.provider_name));
      row.append(top, element("span", "session-row-title", session.title)); row.dataset.sid = session.session_id;
      row.addEventListener("click", function () { selectSession(session.session_id); }); list.appendChild(row);
    });
  }
  function updateHistoryActions(meta) {
    ["claude", "codex"].forEach(function (provider) {
      var action = meta.actions[provider], button = $("thread-" + provider), reason = $("thread-" + provider + "-reason");
      button.textContent = action.label; button.disabled = !action.available;
      reason.textContent = action.available ? "" : (action.reason || "Unavailable");
      if (action.available) button.removeAttribute("aria-describedby"); else button.setAttribute("aria-describedby", reason.id);
    });
  }
  function selectSession(sid) {
    activeSid = sid; activeSessionMeta = null;
    Array.prototype.forEach.call($("session-list").querySelectorAll(".session-row"), function (row) { row.classList.toggle("active", row.dataset.sid === sid); });
    $("thread-search").value = ""; $("thread-search").hidden = true; document.querySelector(".thread-search-label").hidden = true;
    $("thread-actions").hidden = true; $("viewer-title").textContent = "Loading…"; $("viewer-meta").textContent = ""; $("transcript").replaceChildren();
    request("/api/session/" + encodeURIComponent(sid)).then(function (data) {
      if (sid !== activeSid) return;
      activeSessionMeta = data.meta; currentTurns = data.turns || [];
      $("viewer-title").textContent = data.meta.title;
      $("viewer-meta").textContent = data.meta.folder + " · " + data.meta.cwd + " · " + data.meta.provider_name + " · " + data.meta.msg_count + " messages";
      updateHistoryActions(data.meta); $("thread-actions").hidden = false; $("thread-search").hidden = false;
      document.querySelector(".thread-search-label").hidden = false; renderTranscript("");
    }).catch(function (error) {
      if (sid === activeSid) { $("viewer-title").textContent = "Could not load session"; $("viewer-meta").textContent = error.message; }
    });
  }
  function renderTranscript(query) {
    var transcript = $("transcript"); transcript.replaceChildren();
    if (!currentTurns || !currentTurns.length) { emptyMessage(transcript, "No messages in this session."); return; }
    var lower = query.trim().toLowerCase(), count = 0, first = null;
    currentTurns.forEach(function (turn) {
      var matches = !lower || turn.text.toLowerCase().indexOf(lower) !== -1;
      if (matches) count += 1;
      var wrapper = element("div", "turn role-" + (turn.role === "user" ? "user" : "assistant") + (matches ? "" : " thread-search-hidden"));
      var body = element("div", "turn-body"); body.innerHTML = renderTurnBody(turn.text);
      wrapper.append(element("div", "turn-role", turn.role === "user" ? "User" : "Assistant"), body); transcript.appendChild(wrapper);
      if (matches && lower && !first) first = wrapper;
    });
    $("thread-search-count").textContent = lower ? count + " matching turn" + (count === 1 ? "" : "s") : "";
    if (first) first.scrollIntoView({ block: "center" }); else if (!lower) transcript.scrollTop = transcript.scrollHeight;
  }
  function activateTab(tab) {
    var board = tab.dataset.view === "board"; $("board-view").hidden = !board; $("history-view").hidden = board;
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (item) {
      var selected = item === tab; item.classList.toggle("active", selected); item.setAttribute("aria-pressed", String(selected));
    });
    if (board) fetchBoard(); else fetchSessions();
  }
  Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
    tab.addEventListener("click", function () { flushPendingEdits(false).then(function () { activateTab(tab); }); });
  });
  Array.prototype.forEach.call(document.querySelectorAll(".work-scope"), function (button) {
    button.addEventListener("click", function () {
      flushPendingEdits(false).then(function () {
        queueMode = button.dataset.scope; selectedProject = null;
        if (latestBoard) renderBoard(latestBoard);
      });
    });
  });
  $("group-by").addEventListener("change", function () { groupBy = $("group-by").value; if (latestBoard) renderBoard(latestBoard); });
  $("work-search").addEventListener("input", debounce(function () { if (latestBoard && !hasProtectedWorkControls()) renderBoard(latestBoard); }, 120));
  $("project-description").addEventListener("input", function () { $("description-limit").textContent = $("project-description").value.length + " / 1000"; });
  $("cancel-description").addEventListener("click", function () { var project = selectedProjectData(); if (!project) return; $("project-description").value = project.description || ""; $("project-description").removeAttribute("aria-invalid"); $("description-error").hidden = true; renderProjectDetail(); });
  $("save-description").addEventListener("click", function () {
    var project = selectedProjectData(), description = $("project-description"), value = description.value; if (!project) return;
    var button = $("save-description"); button.disabled = true; description.setAttribute("aria-busy", "true");
    mutate("/api/projects/" + encodeURIComponent(project.project_key), "PATCH", { description: value }).then(function (response) {
      project.description = response.project.description; description.removeAttribute("aria-invalid"); $("description-error").hidden = true; announce("Project description saved."); toast("Project description saved");
    }).catch(function (error) { description.setAttribute("aria-invalid", "true"); $("description-error").textContent = "Could not save: " + error.message; $("description-error").hidden = false; description.focus(); announce("Description save failed. Your text is retained."); toast(error.message, true); }).finally(function () { button.disabled = false; description.removeAttribute("aria-busy"); });
  });
  $("search-input").addEventListener("input", debounce(fetchSessions, 250));
  $("here-toggle").addEventListener("change", fetchSessions);
  $("thread-search").addEventListener("input", debounce(function () { renderTranscript($("thread-search").value); }, 150));
  function launchActiveThread(provider) {
    if (!activeSessionMeta) return;
    mutate("/api/sessions/" + encodeURIComponent(activeSessionMeta.session_id) + "/launch", "POST", { provider: provider, full_access: fullAccessEnabled() }).then(function () {
      toast("Opened " + providerName(provider) + " in Terminal");
    }).catch(function (error) { toast(error.message, true); });
  }
  $("thread-claude").addEventListener("click", function () { launchActiveThread("claude"); });
  $("thread-codex").addEventListener("click", function () { launchActiveThread("codex"); });
  request("/api/meta").then(function (meta) {
    csrfToken = meta.csrf_token;
    if (meta.here_only_forced) { $("here-toggle").checked = true; $("here-toggle").disabled = true; }
    return fetchBoard();
  }).then(function () { boardTimer = setInterval(fetchBoard, 10000); }).catch(function (error) { toast(error.message, true); });
  window.addEventListener("beforeunload", function () { if (boardTimer) clearInterval(boardTimer); flushPendingEdits(true); });
})();
