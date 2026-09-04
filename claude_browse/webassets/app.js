// Agent Board local web app. Vanilla JS, no build step and no network assets.
(function () {
  "use strict";
  var csrfToken = "", launchProject = null, activeSid = null, activeSessionMeta = null, queueMode = "today", latestBoard = null;
  var currentTurns = null, sessionsSeq = 0, boardTimer = null;
  function $(id) { return document.getElementById(id); }
  function request(path, options) {
    options = options || {}; options.headers = options.headers || {};
    if (options.body) { options.headers["Content-Type"] = "application/json"; options.headers["X-Agent-Board-Token"] = csrfToken; }
    return fetch(path, options).then(function (res) { return res.json().then(function (data) { if (!res.ok || data.error) throw new Error(data.error || "Request failed"); return data; }); });
  }
  function mutate(path, method, payload) { return request(path, { method: method, body: JSON.stringify(payload || {}) }); }
  function toast(message, isError) { var el = $("toast"); el.textContent = message; el.className = isError ? "show error-toast" : "show"; setTimeout(function () { el.className = ""; }, 2800); }
  function escapeHtml(s) { return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function inlineFormat(escaped) { return escaped.replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>"); }
  function renderTurnBody(text) {
    var codeRe = /```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, parts = [], lastIndex = 0, match;
    while ((match = codeRe.exec(text)) !== null) { if (match.index > lastIndex) parts.push({ type: "prose", text: text.slice(lastIndex, match.index) }); parts.push({ type: "code", text: match[2] }); lastIndex = codeRe.lastIndex; }
    if (lastIndex < text.length) parts.push({ type: "prose", text: text.slice(lastIndex) });
    return parts.map(function (part) { if (part.type === "code") return "<pre><code>" + escapeHtml(part.text) + "</code></pre>"; return part.text.split(/\n{2,}/).filter(function (p) { return p.trim(); }).map(function (p) { return "<p>" + inlineFormat(escapeHtml(p)) + "</p>"; }).join(""); }).join("");
  }
  function debounce(fn, wait) { var timer = null; return function () { clearTimeout(timer); timer = setTimeout(fn, wait); }; }
  function relativeTime(timestamp) { if (!timestamp) return ""; var seconds = Math.max(0, Date.now() / 1000 - Number(timestamp)); if (seconds < 60) return "now"; if (seconds < 3600) return Math.floor(seconds / 60) + "m ago"; if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago"; return Math.floor(seconds / 86400) + "d ago"; }
  function localToday() { var now = new Date(); return now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0") + "-" + String(now.getDate()).padStart(2, "0"); }
  function dueLabel(value) { if (!value) return "No date"; var today = new Date(); today.setHours(0, 0, 0, 0); var due = new Date(value + "T00:00:00"), days = Math.round((due - today) / 86400000); if (days < 0) return "Overdue · " + value; if (days === 0) return "Today"; if (days === 1) return "Tomorrow"; return value; }
  function launchButton(task, provider, titleInput) {
    var button = document.createElement("button"), isResume = task.session_id && task.session_provider === provider;
    button.className = "launch " + provider; button.textContent = (isResume ? "Resume " : "Start ") + (provider === "claude" ? "Claude" : "Codex") + " · full access"; button.disabled = !task.project_available; button.title = task.project_available ? "Opens a new Terminal window with permission checks bypassed" : "Project folder is not available on this Mac";
    button.addEventListener("click", function () { button.disabled = true; mutate("/api/tasks/" + encodeURIComponent(task.task_id) + "/launch", "POST", { provider: provider, full_access: true, title: titleInput.value }).then(function () { toast("Opened " + (provider === "claude" ? "Claude" : "Codex") + " in Terminal"); }).catch(function (err) { toast(err.message, true); }).finally(function () { button.disabled = !task.project_available; }); });
    return button;
  }
  function saveTask(taskId, changes, refresh) { return mutate("/api/tasks/" + encodeURIComponent(taskId), "PATCH", changes).then(function () { if (refresh) return fetchBoard(true); }).catch(function (err) { toast(err.message, true); throw err; }); }
  function renderTask(task) {
    var card = document.createElement("article"); card.className = "task-card";
    var main = document.createElement("div"); main.className = "task-main";
    var title = document.createElement("input"); title.className = "task-title"; title.value = task.title; title.maxLength = 500; title.setAttribute("aria-label", "Task name"); title.addEventListener("input", debounce(function () { saveTask(task.task_id, { title: title.value }, false); }, 450));
    var details = document.createElement("div"); details.className = "task-details";
    var breadcrumb = document.createElement("span"); breadcrumb.className = "task-project"; breadcrumb.textContent = task.project_name;
    var due = document.createElement("input"); due.type = "date"; due.value = task.due_date || ""; due.title = dueLabel(task.due_date); due.addEventListener("change", function () { saveTask(task.task_id, { due_date: due.value || null }, true); });
    var status = document.createElement("select"); [["todo", "To do"], ["waiting", "Waiting"], ["done", "Done"]].forEach(function (choice) { var option = document.createElement("option"); option.value = choice[0]; option.textContent = choice[1]; option.selected = task.status === choice[0]; status.appendChild(option); }); status.addEventListener("change", function () { saveTask(task.task_id, { status: status.value }, true); });
    var runtime = document.createElement("span"); runtime.className = "runtime state-" + task.runtime_state; runtime.textContent = task.runtime_state.replace("-", " ");
    var updated = document.createElement("span"); updated.className = "updated"; updated.textContent = "Updated " + relativeTime(task.updated_at);
    details.appendChild(breadcrumb); details.appendChild(due); details.appendChild(status); details.appendChild(runtime); details.appendChild(updated); main.appendChild(title); main.appendChild(details);
    var actions = document.createElement("div"); actions.className = "task-actions"; actions.appendChild(launchButton(task, "claude", title)); actions.appendChild(launchButton(task, "codex", title));
    var copy = document.createElement("button"); copy.textContent = "Copy safe command"; copy.addEventListener("click", function () { navigator.clipboard.writeText(task.safe_command).then(function () { toast("Safe command copied"); }); }); actions.appendChild(copy);
    card.appendChild(main); card.appendChild(actions); return card;
  }
  function appendGroup(root, name, tasks) {
    var section = document.createElement("section"); section.className = "project-group"; var heading = document.createElement("div"); heading.className = "project-heading"; heading.innerHTML = '<div><span class="repo-mark">⌘</span><strong></strong></div><span class="count"></span>'; heading.querySelector("strong").textContent = name; heading.querySelector(".count").textContent = tasks.length + (tasks.length === 1 ? " task" : " tasks"); section.appendChild(heading); tasks.forEach(function (task) { section.appendChild(renderTask(task)); }); root.appendChild(section);
  }
  function renderBoard(data) {
    latestBoard = data;
    $("attention-count").textContent = data.attention.length; $("task-count").textContent = data.tasks.length;
    var attention = $("attention-list"); attention.innerHTML = ""; if (!data.attention.length) attention.innerHTML = '<div class="empty-card">Nothing is waiting on you.</div>';
    data.attention.forEach(function (item) {
      var card = document.createElement("article"); card.className = "attention-card"; var copy = document.createElement("div"); copy.innerHTML = '<div class="attention-title"></div><div class="muted"></div>'; copy.firstChild.textContent = item.title; copy.lastChild.textContent = item.project.name + " · " + item.provider + " · " + (item.unattended ? "finished, not reviewed" : item.state) + " · " + relativeTime(item.updated_at);
      var actions = document.createElement("div"); actions.className = "attention-actions"; if (!item.queued_task_id) { var queue = document.createElement("button"); queue.className = "primary"; queue.textContent = "Add to queue"; queue.addEventListener("click", function () { createFromSession(item.session_id, item.title); }); actions.appendChild(queue); }
      var ack = document.createElement("button"); ack.textContent = "Acknowledge"; ack.addEventListener("click", function () { mutate("/api/sessions/" + encodeURIComponent(item.session_id) + "/ack", "POST", {}).then(fetchBoard).catch(function (err) { toast(err.message, true); }); }); actions.appendChild(ack); card.appendChild(copy); card.appendChild(actions); attention.appendChild(card);
    });
    var root = $("task-groups"); root.innerHTML = "";
    if (queueMode === "today") {
      var today = localToday(), overdue = [], dueToday = [];
      data.tasks.forEach(function (task) { if (task.due_date && task.due_date < today) overdue.push(task); else if (task.due_date === today) dueToday.push(task); });
      if (overdue.length) appendGroup(root, "Overdue", overdue); if (dueToday.length) appendGroup(root, "Today", dueToday);
      if (!overdue.length && !dueToday.length) root.innerHTML = '<div class="empty-card">Nothing due today. Open “All by project” to plan future work.</div>';
    } else {
      var groups = {}; data.tasks.forEach(function (task) { (groups[task.project_key] = groups[task.project_key] || { name: task.project_name, tasks: [] }).tasks.push(task); });
      if (!data.tasks.length) root.innerHTML = '<div class="empty-card">No queued work yet. Add a task or save a thread from History.</div>';
      Object.keys(groups).forEach(function (key) { appendGroup(root, groups[key].name, groups[key].tasks); });
    }
  }
  function fetchBoard(force) { if (!force && $("board-view").contains(document.activeElement) && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return Promise.resolve(); return request("/api/board").then(function (data) { $("board-error").hidden = true; renderBoard(data); }).catch(function (err) { $("board-error").hidden = false; $("board-error").textContent = err.message; }); }
  function createFromSession(sessionId, suggestedTitle) { var title = window.prompt("Task name", suggestedTitle || ""); if (!title) return; mutate("/api/tasks", "POST", { title: title, session_id: sessionId }).then(function () { toast("Thread added to work queue"); fetchBoard(); if (activeSessionMeta && activeSessionMeta.session_id === sessionId) { activeSessionMeta.queued_task_id = true; $("queue-thread").hidden = true; } }).catch(function (err) { toast(err.message, true); }); }
  function fetchSessions() { var params = new URLSearchParams(), q = $("search-input").value.trim(); if (q) params.set("q", q); if ($("here-toggle").checked) params.set("here", "1"); var seq = ++sessionsSeq; request("/api/sessions?" + params.toString()).then(function (data) { if (seq === sessionsSeq) renderSessionList(data.sessions || []); }).catch(function (err) { if (seq === sessionsSeq) $("session-list").innerHTML = '<div class="session-row-empty">' + escapeHtml(err.message) + "</div>"; }); }
  function renderSessionList(sessions) {
    var list = $("session-list"); list.innerHTML = ""; if (!sessions.length) { list.innerHTML = '<div class="session-row-empty">No sessions found.</div>'; return; }
    sessions.forEach(function (s) { var row = document.createElement("button"); row.className = "session-row" + (s.session_id === activeSid ? " active" : ""); row.dataset.sid = s.session_id; row.innerHTML = '<span class="session-row-top"><strong></strong><span></span></span><span class="session-row-title"></span>'; row.querySelector("strong").textContent = s.folder || "?"; row.querySelector(".session-row-top span").textContent = (s.when || "") + " · " + s.provider_name; row.querySelector(".session-row-title").textContent = s.title; row.addEventListener("click", function () { selectSession(s.session_id); }); list.appendChild(row); });
  }
  function selectSession(sid) {
    activeSid = sid; activeSessionMeta = null; Array.prototype.forEach.call($("session-list").querySelectorAll(".session-row"), function (row) { row.classList.toggle("active", row.dataset.sid === sid); }); $("thread-search").value = ""; $("thread-search").hidden = true; $("thread-actions").hidden = true; $("viewer-title").textContent = "Loading…"; $("viewer-meta").textContent = ""; $("transcript").innerHTML = "";
    request("/api/session/" + encodeURIComponent(sid)).then(function (data) { if (sid !== activeSid) return; activeSessionMeta = data.meta; currentTurns = data.turns || []; $("viewer-title").textContent = data.meta.title; $("viewer-meta").textContent = data.meta.folder + " · " + data.meta.cwd + " · " + data.meta.provider_name + " · " + data.meta.msg_count + " messages"; $("thread-actions").hidden = false; $("queue-thread").hidden = Boolean(data.meta.queued_task_id); $("thread-search").hidden = false; renderTranscript(""); }).catch(function (err) { if (sid === activeSid) { $("viewer-title").textContent = "Could not load session"; $("viewer-meta").textContent = err.message; } });
  }
  function renderTranscript(query) {
    if (!currentTurns || !currentTurns.length) { $("transcript").innerHTML = '<div id="transcript-empty">No messages in this session.</div>'; return; }
    var lower = query.trim().toLowerCase(), count = 0, first = null; $("transcript").innerHTML = "";
    currentTurns.forEach(function (turn) { var matches = !lower || turn.text.toLowerCase().indexOf(lower) !== -1; if (matches) count++; var el = document.createElement("div"); el.className = "turn role-" + (turn.role === "user" ? "user" : "assistant") + (matches ? "" : " thread-search-hidden"); el.innerHTML = '<div class="turn-role"></div><div class="turn-body"></div>'; el.firstChild.textContent = turn.role === "user" ? "User" : "Assistant"; el.lastChild.innerHTML = renderTurnBody(turn.text); $("transcript").appendChild(el); if (matches && lower && !first) first = el; });
    $("thread-search-count").textContent = lower ? count + " matching turn" + (count === 1 ? "" : "s") : ""; if (first) first.scrollIntoView({ block: "center" }); else if (!lower) $("transcript").scrollTop = $("transcript").scrollHeight;
  }
  Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) { tab.addEventListener("click", function () { var board = tab.dataset.view === "board"; $("board-view").hidden = !board; $("history-view").hidden = board; Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (item) { item.classList.toggle("active", item === tab); }); if (!board) fetchSessions(); }); });
  Array.prototype.forEach.call(document.querySelectorAll(".queue-mode"), function (button) { button.addEventListener("click", function () { queueMode = button.dataset.mode; Array.prototype.forEach.call(document.querySelectorAll(".queue-mode"), function (item) { item.classList.toggle("active", item === button); }); if (latestBoard) renderBoard(latestBoard); }); });
  $("show-add-task").addEventListener("click", function () { $("add-task-form").hidden = false; $("new-title").focus(); }); $("cancel-add").addEventListener("click", function () { $("add-task-form").hidden = true; });
  $("add-task-form").addEventListener("submit", function (event) { event.preventDefault(); mutate("/api/tasks", "POST", { title: $("new-title").value, project_path: $("new-project").value, due_date: $("new-due").value || null, provider: $("new-provider").value, notes: $("new-notes").value }).then(function () { event.target.reset(); $("new-project").value = launchProject.path; event.target.hidden = true; toast("Task added"); fetchBoard(); }).catch(function (err) { toast(err.message, true); }); });
  $("queue-thread").addEventListener("click", function () { if (activeSessionMeta) createFromSession(activeSessionMeta.session_id, activeSessionMeta.title); }); $("search-input").addEventListener("input", debounce(fetchSessions, 250)); $("here-toggle").addEventListener("change", fetchSessions); $("thread-search").addEventListener("input", debounce(function () { renderTranscript($("thread-search").value); }, 150));
  function launchActiveThread(provider) { if (!activeSessionMeta) return; mutate("/api/sessions/" + encodeURIComponent(activeSessionMeta.session_id) + "/launch", "POST", { provider: provider, full_access: true }).then(function () { toast("Opened " + (provider === "claude" ? "Claude" : "Codex") + " in Terminal"); }).catch(function (err) { toast(err.message, true); }); }
  $("thread-claude").addEventListener("click", function () { launchActiveThread("claude"); }); $("thread-codex").addEventListener("click", function () { launchActiveThread("codex"); });
  request("/api/meta").then(function (meta) { csrfToken = meta.csrf_token; launchProject = meta.launch_project; $("new-project").value = launchProject.path; if (meta.here_only_forced) { $("here-toggle").checked = true; $("here-toggle").disabled = true; } return fetchBoard(); }).then(function () { boardTimer = setInterval(fetchBoard, 10000); }).catch(function (err) { toast(err.message, true); }); window.addEventListener("beforeunload", function () { if (boardTimer) clearInterval(boardTimer); });
})();
