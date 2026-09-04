// Agent Board local web app. Vanilla JS, no build step and no network assets.
(function () {
  "use strict";
  var csrfToken = "", activeSid = null, activeSessionMeta = null;
  var queueMode = "active", latestBoard = null, currentTurns = null;
  var sessionsSeq = 0, boardTimer = null, editSequence = 0;
  var rowMutationTails = Object.create(null), editStates = [];

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
    var focused = Boolean(active && $("board-view").contains(active) && active.matches(".work-edit"));
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
  function headerCell(textValue, className) { var cell = element("th", className, textValue); cell.scope = "col"; return cell; }
  function renderTaskRow(task) {
    var row = element("tr", "work-row");
    var identity = element("td", "work-identity"), title = element("input", "task-title work-edit");
    title.value = task.title || ""; title.maxLength = 500; title.setAttribute("aria-label", "Name for " + (task.title || task.session_id));
    identity.append(title, registerEdit(task, "title", title, function () { return title.value; }, 450), element("div", "task-project", task.project_name || "Unknown project"), element("div", "session-id", task.session_id));
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
    row.append(identity, dueCell, statusCell, terminalCell, activityCell, actionCell); return row;
  }
  function appendGroup(root, name, tasks) {
    var section = element("section", "project-group"), heading = element("h4", "project-heading");
    heading.append(element("span", "project-name", name), element("span", "count", tasks.length + (tasks.length === 1 ? " thread" : " threads")));
    var table = element("table", "work-table"), caption = element("caption", "sr-only", name + " terminal threads");
    var head = document.createElement("thead"), headRow = document.createElement("tr"), body = document.createElement("tbody");
    headRow.append(headerCell("Name / Project", "column-name"), headerCell("Due date", "column-due"), headerCell("Work status", "column-status"), headerCell("Terminal state", "column-terminal"), headerCell("Agent / activity", "column-activity"), headerCell("Actions", "column-actions"));
    head.appendChild(headRow); tasks.forEach(function (task) { body.appendChild(renderTaskRow(task)); });
    table.append(caption, head, body); section.append(heading, table); root.appendChild(section);
  }
  function workSearchMatches(task) {
    var query = $("work-search").value.trim().toLowerCase(); if (!query) return true;
    var haystack = [task.title, task.project_name, task.session_provider, task.session_id].join(" ").toLowerCase();
    return haystack.indexOf(query) !== -1;
  }
  function renderBoard(data) {
    latestBoard = data; editStates = [];
    $("task-count").textContent = data.tasks.length + (data.tasks.length === 1 ? " thread" : " threads");
    var root = $("task-groups"); root.replaceChildren();
    var visible = data.tasks.filter(function (task) {
      if (!workSearchMatches(task)) return false;
      if (queueMode === "done") return task.work_status === "done" || task.work_status === "archived";
      if (task.work_status !== "active") return false;
      return queueMode !== "today" || task.in_today;
    });
    if (!visible.length) { emptyMessage(root, "No threads match this view."); return; }
    if (queueMode === "projects") {
      var groups = Object.create(null), order = [];
      visible.forEach(function (task) { if (!groups[task.project_key]) { groups[task.project_key] = []; order.push(task.project_key); } groups[task.project_key].push(task); });
      order.forEach(function (key) { appendGroup(root, groups[key][0].project_name, groups[key]); });
    } else {
      appendGroup(root, queueMode === "active" ? "Active" : queueMode === "today" ? "Today" : "Done & Archived", visible);
    }
  }
  function fetchBoard() {
    if (hasProtectedWorkControls()) return Promise.resolve();
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
  Array.prototype.forEach.call(document.querySelectorAll(".queue-mode"), function (button) {
    button.addEventListener("click", function () {
      flushPendingEdits(false).then(function () {
        queueMode = button.dataset.mode;
        Array.prototype.forEach.call(document.querySelectorAll(".queue-mode"), function (item) {
          var selected = item === button; item.classList.toggle("active", selected); item.setAttribute("aria-pressed", String(selected));
        });
        if (latestBoard) renderBoard(latestBoard);
      });
    });
  });
  $("work-search").addEventListener("input", debounce(function () { if (latestBoard && !hasProtectedWorkControls()) renderBoard(latestBoard); }, 120));
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
