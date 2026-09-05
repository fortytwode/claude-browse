// Agent Board local UI. Vanilla JS; all persistence is browser-local or local API calls.
(function () {
  "use strict";
  var csrfToken = "",
    latestBoard = null,
    activeSid = null,
    activeSessionMeta = null,
    currentTurns = null,
    readerMode = "history";
  var queueMode = "open",
    statusFilter = "all",
    groupBy = "priority",
    sortBy = "manual",
    sortDirection = "asc",
    selectedProject = null,
    draggedTask = null,
    draggedProject = null,
    draggedColumn = null,
    columnDropTarget = null;
  var filters = {
      provider: "any",
      priority: "any",
      terminal: "any",
      due: "any",
      presence: "any",
      lastUpdate: "any",
    },
    collapsedFolders = Object.create(null);
  var boardTimer = null,
    boardSeq = 0,
    sessionsSeq = 0,
    rowMutationTails = Object.create(null),
    reorderMutationTails = Object.create(null),
    editRevisionCounters = Object.create(null),
    launchesInFlight = Object.create(null),
    toastTimer = null;
  var editorAction = null,
    editorAllowEmpty = false,
    activeSavedView = null,
    editClientId =
      window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID()
        : String(Date.now()) + "-" + Math.random();
  var PRIORITY_GROUPS = ["urgent", "high", "normal", "low"],
    TERMINAL_GROUPS = ["needs-input", "working", "idle", "ended", "gone", "unknown"];
  var TASK_ACTION_MODES = {
    continue: { actionField: "actions", keyPrefix: "task", buttonPrefix: "task", fallbackLabel: "Continue in", endpoint: "launch", unavailable: "This launch is unavailable.", success: "Launch requested — check Terminal" },
    fresh: { actionField: "start_actions", keyPrefix: "task-new", buttonPrefix: "task-fresh", fallbackLabel: "Start fresh in", endpoint: "start", unavailable: "This fresh start is unavailable.", success: "Fresh conversation requested — earlier history remains attached" },
  };
  var PRIORITY_LABELS = {
      urgent: "⚑ Urgent",
      high: "⚑ High",
      normal: "⚑ Normal",
      low: "⚑ Low",
    },
    TERMINAL_LABELS = {
      "needs-input": "Needs input",
      working: "Working",
      idle: "Idle",
      ended: "Ended",
      gone: "Gone",
      unknown: "Unknown",
    };
  var SAVED_VIEWS_KEY = "agent-board.saved-views.v1";
  // v2 changes the initial board from unfinished work to verified-open terminals.
  var VIEW_SETTINGS_KEY = "agent-board.view-settings.v2";
  var DATA_COLUMNS = ["name", "status", "due", "updated", "priority", "terminal", "agent"];
  var columnOrder = DATA_COLUMNS.slice(),
    columnWidths = Object.create(null),
    collapsedGroups = Object.create(null),
    sidebarManualOrder = false,
    sidebarWidth = 250,
    activePointerGesture = false;
  function $(id) {
    return document.getElementById(id);
  }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function request(path, options) {
    options = options || {};
    options.headers = options.headers || {};
    if (options.body) {
      options.headers["Content-Type"] = "application/json";
      options.headers["X-Agent-Board-Token"] = csrfToken;
    }
    // A stalled task edit must not hold its save queue or detail dialog open
    // indefinitely. Aborting the response does not prove the server rolled back.
    var controller =
        options.method === "PATCH" && path.indexOf("/api/tasks/") === 0
          ? new AbortController()
          : null,
      timer = controller
        ? setTimeout(function () {
            controller.abort();
          }, 15000)
        : null;
    if (controller) options.signal = controller.signal;
    return fetch(path, options)
      .then(function (res) {
        return res
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            if (!res.ok || data.error)
              throw new Error(data.error || "Request failed");
            return data;
          });
      })
      .catch(function (error) {
        if (controller && controller.signal.aborted)
          throw new Error(
            "Saving timed out. The change may have reached the server; check again before retrying",
          );
        throw error;
      })
      .finally(function () {
        if (timer !== null) clearTimeout(timer);
      });
  }
  function mutate(path, method, payload, keepalive) {
    ++boardSeq;
    return request(path, {
      method: method,
      body: JSON.stringify(payload || {}),
      keepalive: Boolean(keepalive),
    }).then(function (response) {
      ++boardSeq;
      return response;
    });
  }
  function toast(message, bad) {
    var node = $("toast");
    if (toastTimer !== null) clearTimeout(toastTimer);
    node.textContent = message;
    node.className = bad ? "show error-toast" : "show";
    toastTimer = setTimeout(function () {
      node.className = "";
      toastTimer = null;
    }, 2800);
  }
  function toastUndo(message, undo) {
    var node = $("toast"), button = el("button", "toast-undo", "Undo");
    if (toastTimer !== null) clearTimeout(toastTimer);
    node.replaceChildren(document.createTextNode ? document.createTextNode(message + " ") : el("span", "", message + " "), button);
    node.className = "show";
    button.addEventListener("click", function () {
      undo();
      if (toastTimer !== null) clearTimeout(toastTimer);
      toastTimer = null;
      node.className = "";
    });
    toastTimer = setTimeout(function () { node.className = ""; toastTimer = null; }, 7000);
  }
  function announce(message) {
    $("work-announcer").textContent = "";
    setTimeout(function () {
      $("work-announcer").textContent = message;
    }, 10);
  }
  function debounce(fn, delay) {
    var timer;
    return function () {
      var args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(null, args);
      }, delay);
    };
  }
  function empty(root, message) {
    root.replaceChildren(el("div", "empty-card", message));
  }
  function providerName(provider) {
    return provider === "codex" ? "CodeX" : "Claude";
  }
  function projectName(project) {
    return project.display_name || project.name || project.project_key;
  }
  function taskProjectKey(task) {
    return task.list_key || task.project_key;
  }
  function taskPresence(task) {
    if (task.terminal_presence === "open" || task.terminal_presence === "closed" || task.terminal_presence === "unknown") return task.terminal_presence;
    return task.terminal_open === true ? "open" : "unknown";
  }
  function terminalText(task) {
    var presence = taskPresence(task);
    return presence.charAt(0).toUpperCase() + presence.slice(1);
  }
  function displayProjects(board) {
    board = board || {};
    var workspace = board.workspace || {};
    if (!(workspace.lists || []).length)
      return (board.projects || []).map(function (project) {
        return Object.assign({}, project, {
          source_project_key: project.source_project_key || project.project_key,
        });
      });
    return workspace.lists
      .slice()
      .sort(function (a, b) {
        return Number(a.position || 0) - Number(b.position || 0) || String(a.list_key).localeCompare(String(b.list_key));
      })
      .map(function (list) {
        return {
          project_key: list.list_key,
          source_project_key: list.source_project_key || null,
          name: list.name,
          description: list.description || "",
          folder_status: list.folder_status,
          working_directory: list.working_directory || null,
          launch_revision: list.launch_revision || null,
          actions: list.actions,
          folder_id: list.folder_id || null,
          space_id: list.space_id,
          position: list.position,
        };
      });
  }
  function boardProjects() {
    return displayProjects(latestBoard);
  }
  function workspaceEnabled() {
    return Boolean(latestBoard && latestBoard.workspace && Array.isArray(latestBoard.workspace.lists));
  }
  function fullAccessEnabled() {
    return Boolean($("full-access").checked);
  }
  function setDragPayload(event, mime, identifier) {
    if (!event.dataTransfer) return;
    event.dataTransfer.effectAllowed = "move";
    if (event.dataTransfer.setData) {
      event.dataTransfer.setData(mime, identifier);
      event.dataTransfer.setData("text/plain", identifier);
    }
  }
  function allowMoveDrop(event) {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
  }
  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  function inlineFormat(value) {
    return value
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  }
  function renderTurnBody(text) {
    var codeRe = /```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g,
      parts = [],
      lastIndex = 0,
      match;
    text = String(text || "");
    while ((match = codeRe.exec(text)) !== null) {
      if (match.index > lastIndex)
        parts.push({ type: "prose", text: text.slice(lastIndex, match.index) });
      parts.push({ type: "code", text: match[2] });
      lastIndex = codeRe.lastIndex;
    }
    if (lastIndex < text.length)
      parts.push({ type: "prose", text: text.slice(lastIndex) });
    return parts
      .map(function (part) {
        if (part.type === "code")
          return "<pre><code>" + escapeHtml(part.text) + "</code></pre>";
        return part.text
          .split(/\n{2,}/)
          .filter(function (paragraph) {
            return paragraph.trim();
          })
          .map(function (paragraph) {
            return "<p>" + inlineFormat(escapeHtml(paragraph)) + "</p>";
          })
          .join("");
      })
      .join("");
  }
  function relativeTime(value) {
    if (!value) return "—";
    var seconds = Math.max(0, Date.now() / 1000 - Number(value));
    if (seconds < 60) return "now";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    return Math.floor(seconds / 86400) + "d ago";
  }
  function dueText(value) {
    if (!value) return "No date";
    var today = new Date();
    today.setHours(0, 0, 0, 0);
    var days = Math.round((new Date(value + "T00:00:00") - today) / 86400000);
    return days < 0
      ? "Overdue"
      : days === 0
        ? "Today"
        : days === 1
          ? "Tomorrow"
          : value;
  }
  function hasFilters() {
    return (
      (queueMode === "all" && statusFilter !== "all") ||
      filters.provider !== "any" ||
      filters.priority !== "any" ||
      filters.terminal !== "any" ||
      filters.due !== "any" ||
      (filters.presence || "any") !== "any" ||
      (filters.lastUpdate || "any") !== "any"
    );
  }
  function reorderLockReason() {
    if ($("work-search").value.trim())
      return "Manual reordering is disabled while searching.";
    if (hasFilters())
      return "Manual reordering is disabled while filters are applied.";
    if (sortBy !== "manual")
      return (
        "Manual reordering is disabled while sorted by " +
        ({ name: "Name", updated: "Last update", due: "Due date", priority: "Priority", terminal: "Terminal state", agent: "Agent" }[sortBy] || sortBy) +
        "."
      );
    return "Drag a handle, or use Move up and Move down from the row menu.";
  }
  function reorderLocked() {
    return Boolean(
      $("work-search").value.trim() || hasFilters() || sortBy !== "manual",
    );
  }
  function updateReorderReason() {
    $("reorder-reason").textContent = reorderLockReason();
  }
  function syncToolbarControls() {
    $("group-by").value = groupBy;
    $("sort-by").value = sortBy;
    $("filter-status").value = statusFilter;
    $("filter-provider").value = filters.provider;
    $("filter-priority").value = filters.priority;
    $("filter-terminal").value = filters.terminal;
    $("filter-due").value = filters.due;
    $("filter-presence").value = filters.presence;
    $("filter-last-update").value = filters.lastUpdate;
    $("sort-direction").textContent = sortDirection === "desc" ? "↓" : "↑";
    $("sort-direction").setAttribute("aria-label", "Sort " + (sortDirection === "desc" ? "descending" : "ascending"));
  }
  function selectedProjectData() {
    return (
      latestBoard &&
      boardProjects().find(function (p) {
        return p.project_key === selectedProject;
      })
    );
  }
  function projectDescriptionDirty() {
    var project = selectedProjectData();
    return Boolean(
      project &&
        !$("project-description-editor").hidden &&
        $("project-description").value !== (project.description || ""),
    );
  }
  function hasProtectedWorkControls() {
    return (
      projectDescriptionDirty() ||
      Object.keys(rowMutationTails).length > 0 ||
      activePointerGesture ||
      Boolean(document.querySelector(".inline-rename")) ||
      Boolean(document.querySelector("details[open], dialog[open]"))
    );
  }
  function queueReorder(key, operation) {
    var prior = reorderMutationTails[key] || Promise.resolve();
    var queued = prior.catch(function () {}).then(operation);
    var settled = queued.finally(function () {
      if (reorderMutationTails[key] === settled)
        delete reorderMutationTails[key];
    });
    reorderMutationTails[key] = settled;
    return settled;
  }
  function afterPendingEdits(action) {
    if (projectDescriptionDirty()) {
      announce("Save or cancel the project description before changing views.");
      return false;
    }
    action();
    return true;
  }
  function mergeTask(saved) {
    if (latestBoard && saved)
      latestBoard.tasks = latestBoard.tasks.map(function (task) {
        return task.task_id === saved.task_id
          ? Object.assign({}, task, saved)
          : task;
      });
  }
  function saveTask(task, field, value) {
    var key = task.task_id,
      revisionKey = key + ":" + field,
      revision = (editRevisionCounters[revisionKey] || 0) + 1;
    editRevisionCounters[revisionKey] = revision;
    var payload = { _edit_client: editClientId, _edit_revision: revision };
    payload[field] = value;
    var prior = rowMutationTails[key] || Promise.resolve();
    var op = prior
      .catch(function () {})
      .then(function () {
        return mutate(
          "/api/tasks/" + encodeURIComponent(key),
          "PATCH",
          payload,
        );
      });
    var settled = op
      .then(function (response) {
        mergeTask(response.task);
        return response.task;
      })
      .finally(function () {
        if (rowMutationTails[key] === settled) delete rowMutationTails[key];
      });
    rowMutationTails[key] = settled;
    return settled;
  }
  function setLaunchBusy(launchKey, busy) {
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-launch-key]"),
      function (button) {
        if (button.dataset.launchKey !== launchKey) return;
        button.disabled = busy || button.dataset.launchAvailable !== "true";
        if (busy) button.setAttribute("aria-busy", "true");
        else button.removeAttribute("aria-busy");
      },
    );
  }
  function isDueTodayOrOverdue(task) {
    var today = new Date();
    var day =
      today.getFullYear() +
      "-" +
      String(today.getMonth() + 1).padStart(2, "0") +
      "-" +
      String(today.getDate()).padStart(2, "0");
    return Boolean(task.due_date && task.due_date <= day);
  }
  function taskMatches(task) {
    if (selectedProject && taskProjectKey(task) !== selectedProject) return false;
    var status = task.work_status === "done" ? "completed" : task.work_status;
    if (queueMode === "all" && statusFilter !== "all" && status !== statusFilter)
      return false;
    if (queueMode === "open" && taskPresence(task) !== "open") return false;
    if (queueMode === "today" && !task.in_today) return false;
    var query = $("work-search").value.trim().toLowerCase();
    if (
      query &&
      [
        task.title,
        task.summary,
        task.project_name,
        task.session_provider,
        task.session_id,
      ]
        .join(" ")
        .toLowerCase()
        .indexOf(query) < 0
    )
      return false;
    if (
      filters.provider !== "any" &&
      task.session_provider !== filters.provider
    )
      return false;
    if (filters.priority !== "any" && task.priority !== filters.priority)
      return false;
    if (filters.terminal !== "any" && (task.terminal_runtime_state || task.terminal_state) !== filters.terminal)
      return false;
    if ((filters.presence || "any") !== "any" && taskPresence(task) !== filters.presence)
      return false;
    if (filters.due === "today" && !isDueTodayOrOverdue(task)) return false;
    if (filters.due === "none" && task.due_date) return false;
    if ((filters.lastUpdate || "any") !== "any") {
      var windows = { "24h": 86400, "7d": 604800, "30d": 2592000 };
      if (!task.last_activity_at || Number(task.last_activity_at) < Date.now() / 1000 - windows[filters.lastUpdate]) return false;
    }
    return true;
  }
  function visibleTasks() {
    return latestBoard ? latestBoard.tasks.filter(taskMatches) : [];
  }
  function taskGroupKey(task) {
    return groupBy === "priority"
      ? task.priority
      : groupBy === "terminal"
        ? (task.terminal_runtime_state || task.terminal_state || "unknown")
        : "all";
  }
  function taskComparator(a, b) {
    var priorityRank = { urgent: 0, high: 1, normal: 2, low: 3 };
    var terminalRank = { "needs-input": 0, working: 1, idle: 2, ended: 3, gone: 4, unknown: 5 };
    var comparison;
    if (sortBy === "name") comparison = String(a.title || "").localeCompare(String(b.title || ""));
    else if (sortBy === "priority") comparison = (priorityRank[a.priority] === undefined ? 99 : priorityRank[a.priority]) - (priorityRank[b.priority] === undefined ? 99 : priorityRank[b.priority]);
    else if (sortBy === "terminal") comparison = (terminalRank[a.terminal_runtime_state || a.terminal_state] === undefined ? 99 : terminalRank[a.terminal_runtime_state || a.terminal_state]) - (terminalRank[b.terminal_runtime_state || b.terminal_state] === undefined ? 99 : terminalRank[b.terminal_runtime_state || b.terminal_state]);
    else if (sortBy === "agent") comparison = String(a.session_provider || "").localeCompare(String(b.session_provider || ""));
    else if (sortBy === "updated") comparison = Number(a.last_activity_at || 0) - Number(b.last_activity_at || 0);
    else if (sortBy === "due") comparison = String(a.due_date || "9999-12-31").localeCompare(String(b.due_date || "9999-12-31"));
    if (comparison) return sortDirection === "desc" ? -comparison : comparison;
    if (sortBy === "updated")
      return (
        (Number(a.last_activity_at || 0) - Number(b.last_activity_at || 0)) * (sortDirection === "desc" ? -1 : 1) ||
        String(a.task_id).localeCompare(String(b.task_id))
      );
    if (sortBy === "due")
      return (
        String(a.due_date || "9999-12-31").localeCompare(
          String(b.due_date || "9999-12-31"),
        ) || String(a.task_id).localeCompare(String(b.task_id))
      );
    return (
      Number(a.order !== undefined ? a.order : a.position) -
        Number(b.order !== undefined ? b.order : b.position) ||
      String(a.task_id).localeCompare(String(b.task_id))
    );
  }
  function sorted(tasks) {
    return tasks.slice().sort(taskComparator);
  }
  function selectScope(scope) {
    if (!afterPendingEdits(function () {})) return;
    if (["open", "all", "today"].indexOf(scope) < 0) scope = "open";
    queueMode = scope;
    activeSavedView = null;
    restoreViewSettings();
    renderBoard(latestBoard);
  }
  function selectStatus(status) {
    statusFilter = ["active", "completed", "archived", "all"].indexOf(status) >= 0 ? status : "active";
    activeSavedView = null;
    renderBoard(latestBoard);
  }
  function renderProjectDetail() {
    var project = selectedProjectData(),
      root = $("project-detail");
    root.hidden = !project;
    if (!project) return;
    $("project-name").textContent = projectName(project);
    $("project-path").textContent = project.working_directory
      ? project.working_directory
      : project.folder_status === "missing"
        ? "Working folder is missing — choose a valid destination before launch."
        : "Unlinked — plan first, then link or create a working folder.";
    var projectTasks = (latestBoard.tasks || []).filter(function (task) { return taskProjectKey(task) === project.project_key; });
    $("project-counts").textContent =
      ((project.counts || {}).active === undefined ? projectTasks.filter(function (task) { return task.work_status === "active"; }).length : (project.counts || {}).active) +
      " active · " +
      ((project.counts || {}).today === undefined ? projectTasks.filter(function (task) { return task.in_today && task.work_status === "active"; }).length : (project.counts || {}).today) +
      " today";
    $("list-launch-actions").hidden = !workspaceEnabled();
    updateListActions(project);
    $("project-description-preview").textContent =
      project.description || "No project description yet.";
    if (
      !projectDescriptionDirty() &&
      document.activeElement !== $("project-description")
    )
      $("project-description").value = project.description || "";
    $("description-limit").textContent =
      $("project-description").value.length + " / 10000";
    var inherited = project.inherited_descriptions || [],
      inheritedRoot = $("project-inherited"),
      inheritedList = $("project-inherited-list");
    inheritedRoot.hidden = inherited.length === 0;
    inheritedList.replaceChildren();
    inherited.forEach(function (note) {
      inheritedList.append(
        el(
          "div",
          "inherited-note",
          (note.source_key || "Project") + ": " + note.description,
        ),
      );
    });
  }
  function menuButton(label, handler) {
    var button = el("button", "", label);
    button.type = "button";
    button.addEventListener("click", handler);
    return button;
  }
  function rowMenu(items, label) {
    var details = el("details", "row-menu"),
      summary = el("summary", "icon-button ellipsis", "⋯"),
      panel = el("div", "row-menu-panel");
    summary.setAttribute("aria-label", label || "More actions");
    items.forEach(function (item) {
      panel.appendChild(
        menuButton(item[0], function () {
          details.open = false;
          item[1]();
        }),
      );
    });
    details.append(summary, panel);
    return details;
  }
  function openEditor(title, label, initial, action, options) {
    options = options || {};
    editorAllowEmpty = Boolean(options.allowEmpty);
    editorAction = action;
    $("editor-title").textContent = title;
    $("editor-label").textContent = label;
    $("editor-input").type = options.type || "text";
    $("editor-input").value = initial || "";
    $("editor-error").hidden = true;
    $("editor-dialog").showModal();
    setTimeout(function () {
      $("editor-input").focus();
    }, 0);
  }
  function projectMenu(project) {
    var folders = (latestBoard.folders || []).filter(function (folder) {
      return folder.folder_id !== project.folder_id;
    });
    var items = [
      [
        "Rename display name",
        function () {
          openEditor(
            "Rename project",
            "Display name (leave blank to use repository name)",
            projectName(project),
            function (name) {
              return mutate(
                "/api/projects/" + encodeURIComponent(project.project_key),
                "PATCH",
                { display_name: name },
              );
            },
            { allowEmpty: true },
          );
        },
      ],
      [
        "Move to workspace",
        function () {
          moveProjectToFolder(project, null);
        },
      ],
    ];
    folders.forEach(function (folder) {
      items.push([
        "Move to " + folder.name,
        function () {
          moveProjectToFolder(project, folder.folder_id);
        },
      ]);
    });
    items.push(
      [
        "Move up",
        function () {
          moveProject(project.project_key, -1);
        },
      ],
      [
        "Move down",
        function () {
          moveProject(project.project_key, 1);
        },
      ],
    );
    return rowMenu(items, "Project actions for " + projectName(project));
  }
  function folderMenu(folder) {
    return rowMenu(
      [
        [
          "Rename folder",
          function () {
            openEditor(
              "Rename folder",
              "Folder name",
              folder.name,
              function (name) {
                return mutate(
                  "/api/folders/" + encodeURIComponent(folder.folder_id),
                  "PATCH",
                  { name: name },
                );
              },
            );
          },
        ],
        [
          "Move up",
          function () {
            moveFolder(folder.folder_id, -1);
          },
        ],
        [
          "Move down",
          function () {
            moveFolder(folder.folder_id, 1);
          },
        ],
      ],
      "Folder actions for " + folder.name,
    );
  }
  function workspacePosition(kind, item, direction) {
    var id = kind === "space" ? item.space_id : kind === "folder" ? item.folder_id : item.list_key;
    sidebarManualOrder = true;
    storeViewSettings();
    return queueReorder("workspace-node:" + kind + ":" + id, function () {
      return mutate("/api/workspace/reorder", "POST", {
        kind: kind,
        node_id: id,
        direction: direction,
      }).then(function () {
        return fetchBoard(true);
      });
    }).catch(function (error) { toast(error.message, true); });
  }
  function workspaceMenu(kind, item) {
    var id = kind === "space" ? item.space_id : kind === "folder" ? item.folder_id : item.list_key,
      label = item.name,
      path = "/api/workspace/" + (kind === "space" ? "spaces/" : kind === "folder" ? "folders/" : "lists/") + encodeURIComponent(id),
      items = [["Rename", function () {
        openEditor("Rename " + kind, kind + " name", label, function (name) {
          return mutate(path, "PATCH", { name: name });
        });
      }]];
    if (kind === "space") {
      items.push(["New Folder", function () {
        openEditor("New Folder", "Folder name", "", function (name) {
          return mutate("/api/workspace/folders", "POST", { name: name, space_id: item.space_id });
        });
      }], ["New List", function () { openListEditor(item.space_id, null); }]);
    }
    if (kind === "folder") {
      (latestBoard.workspace.spaces || []).forEach(function (space) {
        if (space.space_id !== item.space_id) items.push(["Move to " + space.name, function () { return mutate(path, "PATCH", { space_id: space.space_id }); }]);
      });
      items.push(["New List", function () { openListEditor(item.space_id, item.folder_id); }]);
    }
    if (kind === "list") {
      (latestBoard.workspace.spaces || []).forEach(function (space) {
        items.push(["Move to " + space.name + " root", function () { return moveListToSpaceRoot(item.list_key, space); }]);
      });
      (latestBoard.workspace.folders || []).forEach(function (folder) {
        if (folder.folder_id !== item.folder_id) items.push(["Move to " + folder.name, function () { return moveListToFolder(item.list_key, folder); }]);
      });
      items.push(["Link existing folder", function () {
        openEditor("Link existing working folder", "Existing absolute folder path", item.working_directory || "", function (pathValue) {
          return mutate(path, "PATCH", { working_directory: pathValue });
        });
      }], ["Create working folder…", function () {
        openEditor("Create working folder", "New absolute folder path", "", function (pathValue) {
          return mutate(path + "/directory", "POST", { path: pathValue });
        });
      }], ["Keep unlinked (plan first)", function () {
        return mutate(path, "PATCH", { working_directory: null });
      }]);
    }
    items.push(["Move up", function () { workspacePosition(kind, item, -1); }], ["Move down", function () { workspacePosition(kind, item, 1); }]);
    return rowMenu(items, kind + " actions for " + label);
  }
  function openListEditor(spaceId, folderId) {
    openEditor("New List", "List name", "", function (name) {
      return mutate("/api/workspace/lists", "POST", { name: name, space_id: spaceId, folder_id: folderId });
    });
  }
  function renderProjectNode(project) {
    var row = el("div", "project-row"),
      select = el(
        "button",
        "project-select" +
          (selectedProject === project.project_key ? " active" : ""),
      );
    select.type = "button";
    select.draggable = true;
    select.append(
      el("span", "project-name", projectName(project)),
      el("span", "nav-count", String((project.counts || {}).active || 0)),
    );
    select.addEventListener("click", function () {
      if (!afterPendingEdits(function () {})) return;
      selectedProject = project.project_key;
      queueMode = "all";
      activeSavedView = null;
      restoreViewSettings();
      renderBoard(latestBoard);
    });
    select.addEventListener("dragstart", function (event) {
      draggedProject = project.project_key;
      setDragPayload(event, "application/x-agent-board-list", project.project_key);
    });
    select.addEventListener("dragend", function () {
      draggedProject = null;
    });
    row.append(select, projectMenu(project));
    return row;
  }
  function renderSidebar(data) {
    var sidebarSort = $("sidebar-sort");
    if (sidebarSort) sidebarSort.value = sidebarManualOrder ? "manual" : "open";
    Array.prototype.forEach.call(
      document.querySelectorAll(".scope-tab"),
      function (node) {
        var active = node.dataset.scope === queueMode && !selectedProject;
        node.classList.toggle("active", active);
        node.setAttribute(
          node.classList.contains("scope-tab")
            ? "aria-selected"
            : "aria-pressed",
          String(active),
        );
      },
    );
    var root = $("folder-list");
    root.replaceChildren();
    if (data.workspace && Array.isArray(data.workspace.spaces)) {
      var workspace = data.workspace;
      workspace.spaces.slice().sort(function (a, b) { return Number(a.position || 0) - Number(b.position || 0); }).forEach(function (space) {
        var spaceBlock = el("div", "space-block"), spaceRow = el("div", "space-row"), spaceSelect = el("button", "space-select"), spaceId = "space:" + space.space_id;
        spaceSelect.type = "button";
        spaceSelect.append(el("span", "tree-icon space-icon", "◆"), el("span", "project-name", space.name), sidebarCount(space));
        spaceSelect.addEventListener("click", function () { collapsedFolders[spaceId] = !collapsedFolders[spaceId]; renderSidebar(data); });
        spaceRow.append(spaceSelect, workspaceMenu("space", space)); spaceBlock.appendChild(spaceRow);
        spaceBlock.addEventListener("dragover", function (event) { if (draggedProject) allowMoveDrop(event); });
        spaceBlock.addEventListener("drop", function (event) {
          event.preventDefault();
          if (draggedProject) moveListToSpaceRoot(draggedProject, space);
        });
        if (!collapsedFolders[spaceId]) {
          var folders = workspace.folders.filter(function (folder) { return folder.space_id === space.space_id; });
          folders.forEach(function (folder) {
            var folderBlock = el("div", "tree-folder-block"), folderRow = el("div", "folder-row"), folderSelect = el("button", "folder-select"), folderId = "folder:" + folder.folder_id;
            folderSelect.type = "button";
            folderSelect.append(el("span", "tree-icon folder-icon", "▱"), el("span", "folder-caret", collapsedFolders[folderId] ? "›" : "⌄"), el("span", "project-name", folder.name), sidebarCount(folder));
            folderSelect.addEventListener("click", function () { collapsedFolders[folderId] = !collapsedFolders[folderId]; renderSidebar(data); });
            folderRow.append(folderSelect, workspaceMenu("folder", folder)); folderBlock.appendChild(folderRow);
            if (!collapsedFolders[folderId]) appendWorkspaceLists(folderBlock, workspace, space.space_id, folder.folder_id);
            folderBlock.addEventListener("dragover", function (event) { if (draggedProject) allowMoveDrop(event); });
            folderBlock.addEventListener("drop", function (event) { event.preventDefault(); event.stopPropagation(); moveListToFolder(draggedProject, folder); });
            spaceBlock.appendChild(folderBlock);
          });
          appendWorkspaceLists(spaceBlock, workspace, space.space_id, null);
        }
        root.appendChild(spaceBlock);
      });
      renderSavedViews();
      return;
    }
    var folders = data.folders || [];
    function appendProjects(parent, folderId) {
      var holder = el("div", "folder-projects");
      data.projects
        .filter(function (project) {
          return (project.folder_id || null) === folderId;
        })
        .forEach(function (project) {
          holder.appendChild(renderProjectNode(project));
        });
      parent.appendChild(holder);
    }
    folders.forEach(function (folder) {
      var block = el("div", "folder-block"),
        row = el("div", "folder-row"),
        select = el("button", "folder-select"),
        caret = el(
          "span",
          "folder-caret",
          collapsedFolders[folder.folder_id] ? "›" : "⌄",
        );
      select.type = "button";
      select.append(caret, el("span", "project-name", folder.name));
      select.addEventListener("click", function () {
        collapsedFolders[folder.folder_id] =
          !collapsedFolders[folder.folder_id];
        renderSidebar(data);
      });
      row.append(select, folderMenu(folder));
      block.appendChild(row);
      block.addEventListener("dragover", function (event) {
        if (draggedProject) allowMoveDrop(event);
      });
      block.addEventListener("drop", function (event) {
        event.preventDefault();
        var project = data.projects.find(function (p) {
          return p.project_key === draggedProject;
        });
        if (project) moveProjectToFolder(project, folder.folder_id);
      });
      if (!collapsedFolders[folder.folder_id])
        appendProjects(block, folder.folder_id);
      root.appendChild(block);
    });
    var unfiled = el("div", "folder-projects");
    data.projects
      .filter(function (project) {
        return !project.folder_id;
      })
      .forEach(function (project) {
        unfiled.appendChild(renderProjectNode(project));
      });
    if (unfiled.children.length) {
      var label = el("div", "sidebar-section-title", "Workspace projects");
      label.style.marginTop = "10px";
      root.append(label, unfiled);
    }
    renderSavedViews();
  }
  function sidebarCounts(item) {
    var counts = item.counts;
    if (counts && typeof counts.total === "number") return counts;
    var key = item.list_key || item.project_key;
    var matching = key ? (latestBoard.tasks || []).filter(function (task) { return taskProjectKey(task) === key; }) : [];
    return { total: matching.length, open_terminal: matching.filter(function (task) { return taskPresence(task) === "open"; }).length };
  }
  function sidebarCount(item) {
    var counts = sidebarCounts(item);
    return el("span", "nav-count", String(counts.open_terminal || 0) + "/" + String(counts.total || 0));
  }
  function appendWorkspaceLists(parent, workspace, spaceId, folderId) {
    var holder = el("div", "folder-projects workspace-lists");
    workspace.lists.filter(function (list) { return list.space_id === spaceId && (list.folder_id || null) === folderId; }).sort(function (a, b) {
      var aCounts = sidebarCounts(a), bCounts = sidebarCounts(b);
      return sidebarManualOrder ? Number(a.position || 0) - Number(b.position || 0) : Number(bCounts.open_terminal || 0) - Number(aCounts.open_terminal || 0) || String(a.name).localeCompare(String(b.name));
    }).forEach(function (list) {
      var row = el("div", "project-row list-row"), select = el("button", "project-select" + (selectedProject === list.list_key ? " active" : ""));
      select.type = "button"; select.draggable = true;
      select.append(el("span", "tree-icon list-icon", "☷"), el("span", "project-name", list.name), sidebarCount(list));
      select.title = list.name;
      select.addEventListener("click", function () { if (!afterPendingEdits(function () {})) return; selectedProject = list.list_key; queueMode = "all"; activeSavedView = null; restoreViewSettings(); renderBoard(latestBoard); });
      select.addEventListener("dragstart", function (event) { draggedProject = list.list_key; setDragPayload(event, "application/x-agent-board-list", list.list_key); });
      select.addEventListener("dragend", function () { draggedProject = null; });
      row.append(select, workspaceMenu("list", list));
      row.addEventListener("dragover", function (event) { if (draggedTask || draggedProject) allowMoveDrop(event); });
      row.addEventListener("drop", function (event) {
        event.preventDefault();
        event.stopPropagation();
        if (draggedProject && draggedProject !== list.list_key) {
          var rect = row.getBoundingClientRect && row.getBoundingClientRect();
          return placeWorkspaceNode("list", draggedProject, list.list_key, rect && event.clientY > rect.top + rect.height / 2 ? "after" : "before");
        }
        var task = latestBoard.tasks.find(function (candidate) { return candidate.task_id === draggedTask; });
        if (task) moveTaskToList(task, list.list_key);
      });
      holder.appendChild(row);
    });
    if (holder.children.length) parent.appendChild(holder);
  }
  function placeWorkspaceNode(kind, nodeId, targetId, placement) {
    if (!nodeId || !targetId || nodeId === targetId) return Promise.resolve();
    sidebarManualOrder = true; storeViewSettings();
    return queueReorder("workspace-node:" + kind + ":" + nodeId, function () {
      return mutate("/api/workspace/reorder", "POST", { kind: kind, node_id: nodeId, target_id: targetId, placement: placement }).then(function () { return fetchBoard(true); });
    }).then(function () { toast("List order saved (Manual)"); }).catch(function (error) { toast(error.message, true); });
  }
  function moveListToFolder(listKey, folder) {
    if (!listKey || !workspaceEnabled()) return;
    return queueReorder("workspace-list-parent:" + listKey, function () {
      return mutate("/api/workspace/lists/" + encodeURIComponent(listKey), "PATCH", { space_id: folder.space_id, folder_id: folder.folder_id })
        .then(function () { return fetchBoard(true); });
    })
      .then(function () { toast("List moved"); })
      .catch(function (error) { toast(error.message, true); });
  }
  function moveListToSpaceRoot(listKey, space) {
    if (!listKey || !workspaceEnabled()) return;
    return queueReorder("workspace-list-parent:" + listKey, function () {
      return mutate("/api/workspace/lists/" + encodeURIComponent(listKey), "PATCH", { space_id: space.space_id, folder_id: null })
        .then(function () { return fetchBoard(true); });
    })
      .then(function () { toast("List moved"); })
      .catch(function (error) { toast(error.message, true); });
  }
  function moveProjectToFolder(project, folderId) {
    mutate(
      "/api/projects/" + encodeURIComponent(project.project_key),
      "PATCH",
      { folder_id: folderId },
    )
      .then(function () {
        return fetchBoard(true);
      })
      .then(function () {
        toast("Project moved");
      })
      .catch(function (error) {
        toast(error.message, true);
      });
  }
  function moveProject(projectKey, direction) {
    return queueReorder("projects", function () {
      var project = latestBoard.projects.find(function (p) {
        return p.project_key === projectKey;
      });
      if (!project) return;
      var peers = latestBoard.projects.filter(function (p) {
        return (p.folder_id || null) === (project.folder_id || null);
      });
      var index = peers.findIndex(function (p) {
          return p.project_key === projectKey;
        }),
        target = index + direction;
      if (target < 0 || target >= peers.length)
        return announce(
          "Project is already at the " + (direction < 0 ? "top." : "bottom."),
        );
      var otherKey = peers[target].project_key;
      var ids = latestBoard.projects.map(function (p) {
        return p.project_key === projectKey
          ? otherKey
          : p.project_key === otherKey
            ? projectKey
            : p.project_key;
      });
      return mutate("/api/projects/reorder", "POST", {
        project_keys: ids,
      }).then(function (response) {
        var byKey = Object.fromEntries(
          latestBoard.projects.map(function (p) {
            return [p.project_key, p];
          }),
        );
        latestBoard.projects = response.projects.map(function (p) {
          return Object.assign({}, byKey[p.project_key], p, {
            counts: (byKey[p.project_key] || p).counts,
          });
        });
        if (!projectDescriptionDirty()) renderBoard(latestBoard);
      });
    }).catch(function (error) {
      toast(error.message, true);
    });
  }
  function moveFolder(folderId, direction) {
    return queueReorder("folders", function () {
      var ids = (latestBoard.folders || []).map(function (f) {
          return f.folder_id;
        }),
        index = ids.indexOf(folderId),
        target = index + direction;
      if (target < 0 || target >= ids.length)
        return announce(
          "Folder is already at the " + (direction < 0 ? "top." : "bottom."),
        );
      var swap = ids[target];
      ids[index] = swap;
      ids[target] = folderId;
      return mutate("/api/folders/reorder", "POST", { folder_ids: ids })
        .then(function (response) {
          latestBoard.folders = response.folders;
          if (!projectDescriptionDirty()) renderBoard(latestBoard);
        })
        .catch(function (error) {
          toast(error.message, true);
        });
    });
  }
  function prioritySelect(task) {
    var menu = el("details", "priority-menu priority-" + task.priority),
      summary = el("summary", "priority-flag", PRIORITY_LABELS[task.priority]),
      panel = el("div", "priority-options"),
      options = [];
    summary.setAttribute("aria-label", "Set priority for " + task.title);
    panel.setAttribute("role", "radiogroup");
    panel.setAttribute("aria-label", "Priority for " + task.title);
    if (task.work_status !== "active") summary.setAttribute("aria-disabled", "true");
    PRIORITY_GROUPS.forEach(function (priority) {
      var option = el("button", "priority-option priority-" + priority, PRIORITY_LABELS[priority]);
      option.type = "button";
      option.setAttribute("role", "radio");
      option.setAttribute("aria-checked", String(task.priority === priority));
      option.tabIndex = task.priority === priority ? 0 : -1;
      option.addEventListener("click", function () {
        if (task.work_status !== "active" || priority === task.priority) { menu.open = false; return; }
        menu.open = false;
        saveTask(task, "priority", priority)
          .then(function () { toast("Priority updated"); renderBoard(latestBoard); })
          .catch(function (error) { toast(error.message, true); });
      });
      option.addEventListener("keydown", function (event) {
        var index = options.indexOf(option), next;
        if (event.key === "Escape") { menu.open = false; summary.focus(); return; }
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); option.click(); return; }
        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
        event.preventDefault();
        next = options[(index + (event.key === "ArrowDown" ? 1 : options.length - 1)) % options.length];
        next.focus();
      });
      options.push(option); panel.appendChild(option);
    });
    summary.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault(); menu.open = true;
        options[task.priority ? PRIORITY_GROUPS.indexOf(task.priority) : 0].focus();
      }
    });
    menu.append(summary, panel);
    return menu;
  }
  function markDone(task) {
    var next = task.work_status === "done" ? "active" : "done";
    saveTask(task, "status", next)
      .then(function () {
        fetchBoard(true);
      })
      .catch(function (error) {
        toast(error.message, true);
      });
  }
  function taskStatusSelect(task) {
    var select = el("select", "task-status");
    select.setAttribute("aria-label", "Task status for " + task.title);
    [
      ["active", "To do"],
      ["done", "Done"],
      ["archived", "Archived"],
    ].forEach(function (pair) {
      var option = el("option", "", pair[1]);
      option.value = pair[0];
      option.selected = task.work_status === pair[0];
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      select.disabled = true;
      saveTask(task, "status", select.value)
        .then(function () { fetchBoard(true); })
        .catch(function (error) {
          select.value = task.work_status;
          toast(error.message, true);
        })
        .finally(function () { select.disabled = false; });
    });
    return select;
  }
  function inlineRename(task, identity, crumb, title, pencil) {
    var input = el("input", "inline-rename");
    input.value = task.title || ""; input.maxLength = 500; input.setAttribute("aria-label", "Rename " + (task.title || "task"));
    identity.replaceChildren(crumb, input);
    input.focus(); input.select();
    function cancel() { identity.replaceChildren(crumb, title, pencil); pencil.focus(); }
    function save() {
      var next = input.value.trim();
      if (!next || next === task.title) return cancel();
      input.disabled = true;
      saveTask(task, "title", next).then(function (saved) {
        Object.assign(task, saved); title.textContent = task.title; cancel(); toast("Task renamed");
      }).catch(function (error) { input.disabled = false; input.setAttribute("aria-invalid", "true"); toast("Could not rename: " + error.message, true); });
    }
    input.addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); save(); } if (event.key === "Escape") { event.preventDefault(); cancel(); } });
    input.addEventListener("blur", function () { if (!input.disabled) cancel(); });
  }
  function openTaskDialog(task) {
    $("task-dialog").dataset.taskId = task.task_id;
    $("task-dialog").dataset.openVersion = String(
      Number($("task-dialog").dataset.openVersion || 0) + 1,
    );
    $("dialog-project").textContent = projectName(
      boardProjects().find(function (p) {
        return p.project_key === taskProjectKey(task);
      }) || { name: task.project_name },
    );
    $("dialog-task-title").textContent = task.title || "Untitled task";
    var fields = $("dialog-task-fields");
    fields.replaceChildren();
    var title = el("input", "");
    title.value = task.title || "";
    title.maxLength = 500;
    var due = el("input", "");
    due.type = "date";
    due.value = task.due_date || "";
    var status = el("select", "");
    [
      ["active", "To do"],
      ["done", "Completed"],
      ["archived", "Archived"],
    ].forEach(function (pair) {
      var opt = el("option", "", pair[1]);
      opt.value = pair[0];
      opt.selected = task.work_status === pair[0];
      status.appendChild(opt);
    });
    var error = el("p", "field-error");
    error.hidden = true;
    function field(label, control) {
      var wrapper = el("label", "", label);
      wrapper.appendChild(control);
      fields.appendChild(wrapper);
    }
    field("Name", title);
    field("Due date", due);
    field("Work status", status);
    fields.appendChild(error);
    $("task-destination").textContent = task.working_directory
      ? "Next launch destination: " + task.working_directory + (task.folder_status === "ready" ? "" : " (" + task.folder_status + ")")
      : "Next launch destination: unlinked — choose a working folder first.";
    function dialogSave(name, value, control) {
      control.removeAttribute("aria-invalid");
      error.hidden = true;
      saveTask(task, name, value)
        .then(function (saved) {
          Object.assign(task, saved);
          $("dialog-task-title").textContent = task.title;
        })
        .catch(function (reason) {
          control.setAttribute("aria-invalid", "true");
          error.textContent =
            "Could not save: " +
            reason.message +
            ". Your edit is still here; change it to retry.";
          error.hidden = false;
        });
    }
    title.addEventListener("change", function () {
      dialogSave("title", title.value, title);
    });
    due.addEventListener("change", function () {
      dialogSave("due_date", due.value || null, due);
    });
    status.addEventListener("change", function () {
      dialogSave("status", status.value, status);
    });
    var viewer = $("viewer");
    $("conversation-host").appendChild(viewer);
    $("dialog-access").appendChild($("full-access-label"));
    $("task-dialog").showModal();
    readerMode = "task";
    updateTaskActions(task);
    updateFreshTaskActions(task);
    loadTaskHistory(task);
    selectSession(task.session_id);
  }
  function loadTaskHistory(task) {
    var picker = $("task-history-picker"), select = $("task-history-select"), dialog = $("task-dialog"), taskId = task.task_id, openVersion = dialog.dataset.openVersion;
    picker.hidden = true; select.replaceChildren();
    request("/api/tasks/" + encodeURIComponent(task.task_id) + "/history")
      .then(function (data) {
        if (!dialog.open || dialog.dataset.taskId !== taskId || dialog.dataset.openVersion !== openVersion) return;
        var sessions = data.sessions || [];
        if (!sessions.length) return;
        sessions.forEach(function (session) {
          var option = el("option", "", (session.provider || "Agent") + " · " + (session.cwd || "unknown folder"));
          option.value = session.session_id; option.selected = session.session_id === task.session_id; select.appendChild(option);
        });
        picker.hidden = false;
        select.onchange = function () { selectSession(select.value); };
      })
      .catch(function (error) { toast("Could not load conversation history: " + error.message, true); });
  }
  function launchAction(actions, provider, fallbackLabel) {
    return (actions || {})[provider] || {
      label: fallbackLabel + " " + providerName(provider),
      available: false,
      reason: "Unavailable",
    };
  }
  function updateTaskActions(task) {
    updateTaskActionButtons(task, TASK_ACTION_MODES.continue);
  }
  function updateFreshTaskActions(task) {
    updateTaskActionButtons(task, TASK_ACTION_MODES.fresh);
  }
  function updateTaskActionButtons(task, mode) {
    ["claude", "codex"].forEach(function (provider) {
      var action = launchAction(task[mode.actionField], provider, mode.fallbackLabel), key = mode.keyPrefix + ":" + task.task_id + ":" + provider, button = $(mode.buttonPrefix + "-" + provider), reason = $(mode.buttonPrefix + "-" + provider + "-reason");
      button.textContent = action.label;
      button.dataset.launchKey = key;
      button.dataset.launchAvailable = String(Boolean(action.available));
      button.disabled = !action.available || Boolean(launchesInFlight[key]);
      button.title = action.available ? "" : action.reason || "Unavailable";
      reason.textContent = action.available ? "" : action.reason || "Unavailable";
      reason.hidden = mode === TASK_ACTION_MODES.continue &&
        task.session_id === activeSid && !$("thread-error").hidden &&
        /transcript/i.test(action.reason || "");
    });
  }
  function updateListActions(project) {
    ["claude", "codex"].forEach(function (provider) {
      var action = launchAction(project.actions, provider, "Start"), key = "list:" + project.project_key + ":" + provider, button = $("list-" + provider), reason = $("list-" + provider + "-reason");
      button.textContent = action.label;
      button.dataset.launchKey = key;
      button.dataset.launchAvailable = String(Boolean(action.available));
      button.disabled = !action.available || Boolean(launchesInFlight[key]);
      button.title = action.available ? "Destination: " + (project.working_directory || "") + (fullAccessEnabled() ? " · full access" : " · permissions") : action.reason || "Unavailable";
      reason.textContent = action.available ? "" : action.reason || "Unavailable";
    });
  }
  function launchTask(task, provider) {
    return launchTaskAction(task, provider, TASK_ACTION_MODES.continue);
  }
  function continueOrFocusTask(task, provider) {
    var isCurrentOpenSession = taskPresence(task) === "open" && task.session_provider === provider,
      key = "focus:" + task.task_id + ":" + provider;
    if (!isCurrentOpenSession) return launchTask(task, provider);
    if (launchesInFlight[key]) return Promise.resolve();
    launchesInFlight[key] = true;
    return mutate("/api/tasks/" + encodeURIComponent(task.task_id) + "/focus", "POST", {})
      .then(function (result) {
        if (result.focused) {
          toast("Opened the existing " + providerName(provider) + " terminal");
          return undefined;
        }
        toast((result.reason || "That terminal is no longer available.") + " Continuing in a new Terminal.");
        return launchTask(task, provider);
      })
      .catch(function () {
        // A board server from before this feature, or an OS automation denial,
        // must never prevent an otherwise valid continuation.
        toast("Could not focus that terminal. Continuing in a new Terminal.");
        return launchTask(task, provider);
      })
      .finally(function () { delete launchesInFlight[key]; });
  }
  function startFreshTask(task, provider) {
    return launchTaskAction(task, provider, TASK_ACTION_MODES.fresh);
  }
  function launchTaskAction(task, provider, mode) {
    var action = launchAction(task[mode.actionField], provider, mode.fallbackLabel), key = mode.keyPrefix + ":" + task.task_id + ":" + provider;
    if (!action.available) return toast(action.reason || mode.unavailable, true);
    if (launchesInFlight[key]) return Promise.resolve();
    launchesInFlight[key] = true;
    updateTaskActionButtons(task, mode);
    return mutate("/api/tasks/" + encodeURIComponent(task.task_id) + "/" + mode.endpoint, "POST", { provider: provider, full_access: fullAccessEnabled(), launch_revision: task.launch_revision })
      .then(function () { toast(mode.success); })
      .catch(function (error) { toast(error.message, true); })
      .finally(function () { delete launchesInFlight[key]; updateTaskActionButtons(task, mode); });
  }
  function gridLaunchAction(task, provider) {
    var continuation = launchAction(task.actions, provider, "Continue in"),
      fresh = launchAction(task.start_actions, provider, "Start"),
      useContinuation = continuation.available,
      action = useContinuation ? continuation : fresh,
      presence = taskPresence(task),
      label = useContinuation
        ? (presence === "closed" ? "Restart " : "Continue in ") + providerName(provider)
        : "Start " + providerName(provider),
      button = el("button", "grid-launch " + provider, label);
    button.type = "button";
    button.disabled = !action.available;
    button.title = !action.available
      ? action.reason || "Unavailable"
      : presence === "open" && task.session_provider === provider
        ? "Opens the existing Terminal window for this task. If it changed, safely continues in a new Terminal."
        : useContinuation
          ? "Continue this task in a new Terminal launch."
          : "Start a new conversation; earlier task history remains attached.";
    button.addEventListener("click", function () {
      if (useContinuation) continueOrFocusTask(task, provider);
      else startFreshTask(task, provider);
    });
    return button;
  }
  function launchList(project, provider) {
    var action = launchAction(project.actions, provider, "Start"), key = "list:" + project.project_key + ":" + provider;
    if (!action.available) return toast(action.reason || "This launch is unavailable.", true);
    if (launchesInFlight[key]) return Promise.resolve();
    launchesInFlight[key] = true;
    updateListActions(project);
    return mutate("/api/workspace/lists/" + encodeURIComponent(project.project_key) + "/launch", "POST", { provider: provider, full_access: fullAccessEnabled(), launch_revision: project.launch_revision })
      .then(function () { toast("Launch requested — check Terminal"); })
      .catch(function (error) { toast(error.message, true); })
      .finally(function () { delete launchesInFlight[key]; updateListActions(project); });
  }
  function header(text, cls, sort, column, groupId) {
    var th = el("th", cls + (sort ? " sortable" : ""));
    th.scope = "col";
    if (column && columnWidths[column]) th.style.width = columnWidths[column] + "px";
    if (sort) {
      var button = el("button", "", text);
      button.type = "button";
      button.addEventListener("click", function () {
        sortDirection = sortBy === sort ? (sortDirection === "asc" ? "desc" : "asc") : (sort === "updated" ? "desc" : "asc");
        sortBy = sort;
        $("sort-by").value = sort;
        storeViewSettings();
        renderBoard(latestBoard);
      });
      th.appendChild(button);
      if (sortBy === sort)
        th.setAttribute(
          "aria-sort",
          sortDirection === "desc" ? "descending" : "ascending",
        );
    } else th.textContent = text;
    if (column) {
      th.draggable = true;
      th.addEventListener("dragstart", function (event) { draggedColumn = column; setDragPayload(event, "application/x-agent-board-column", column); });
      th.addEventListener("dragend", function () { clearColumnDropTarget(); draggedColumn = null; });
      th.addEventListener("dragover", function (event) { if (draggedColumn && draggedColumn !== column) { allowMoveDrop(event); setColumnDropTarget(th); } });
      th.addEventListener("dragleave", function () { if (columnDropTarget === th) clearColumnDropTarget(); });
      th.addEventListener("drop", function (event) { event.preventDefault(); clearColumnDropTarget(); moveColumn(draggedColumn, column); });
      var left = el("button", "column-arrow", "‹"), right = el("button", "column-arrow", "›"), resize = el("button", "column-resize", ""), index = columnOrder.indexOf(column);
      left.type = right.type = resize.type = "button";
      resize.id = "column-resize-" + (groupId || "table") + "-" + column;
      left.disabled = index === 0; right.disabled = index === columnOrder.length - 1;
      left.setAttribute("aria-label", "Move " + text + " left"); right.setAttribute("aria-label", "Move " + text + " right"); resize.setAttribute("aria-label", "Resize " + text); resize.title = "Drag to resize " + text;
      left.addEventListener("click", function () { moveColumn(column, columnOrder[Math.max(0, columnOrder.indexOf(column) - 1)]); });
      right.addEventListener("click", function () { moveColumn(column, columnOrder[Math.min(columnOrder.length - 1, columnOrder.indexOf(column) + 1)]); });
      resize.addEventListener("keydown", function (event) { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); setColumnWidth(column, renderedColumnWidth(th, column) + (event.key === "ArrowLeft" ? -12 : 12)); storeViewSettings(); renderBoardWithFocus(resize.id); } });
      resize.addEventListener("pointerdown", function (event) { event.preventDefault(); event.stopPropagation(); beginColumnResize(event, column); });
      th.append(left, right, resize);
    }
    return th;
  }
  function setColumnDropTarget(target) {
    clearColumnDropTarget();
    columnDropTarget = target;
    target.className += " column-drop-target";
  }
  function clearColumnDropTarget() {
    if (!columnDropTarget) return;
    columnDropTarget.className = columnDropTarget.className.replace(/\s*column-drop-target/g, "");
    columnDropTarget = null;
  }
  function moveColumn(column, before) {
    var from = columnOrder.indexOf(column), to = columnOrder.indexOf(before);
    if (from < 0 || to < 0 || from === to) return;
    columnOrder.splice(from, 1); columnOrder.splice(to, 0, column);
    storeViewSettings(); renderBoard(latestBoard);
  }
  function setColumnWidth(column, width) {
    columnWidths[column] = Math.max(72, Math.min(600, Number(width) || 130));
  }
  function renderedColumnWidth(header, column) {
    var rect = header && typeof header.getBoundingClientRect === "function" ? header.getBoundingClientRect() : null;
    return rect && rect.width ? rect.width : columnWidths[column] || 130;
  }
  function beginColumnResize(event, column) {
    if (activePointerGesture) return;
    var header = event.currentTarget.parentElement, startX = event.clientX, start = renderedColumnWidth(header, column), resizeId = event.currentTarget.id, finished = false;
    activePointerGesture = true;
    function move(next) {
      setColumnWidth(column, start + next.clientX - startX);
      header.style.width = columnWidths[column] + "px";
    }
    function end() { if (finished) return; finished = true; activePointerGesture = false; storeViewSettings(); window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", end); window.removeEventListener("pointercancel", end); window.removeEventListener("blur", end); renderBoardWithFocus(resizeId); }
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", end); window.addEventListener("pointercancel", end); window.addEventListener("blur", end);
  }
  function beginSidebarResize(event) {
    if (activePointerGesture) return;
    var startX = event.clientX, start = sidebarWidth, finished = false;
    activePointerGesture = true;
    function move(next) {
      sidebarWidth = Math.max(190, Math.min(480, start + next.clientX - startX));
      if (document.documentElement && document.documentElement.style) document.documentElement.style.setProperty("--sidebar-width", sidebarWidth + "px");
    }
    function end() { if (finished) return; finished = true; activePointerGesture = false; storeViewSettings(); window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", end); window.removeEventListener("pointercancel", end); window.removeEventListener("blur", end); }
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", end); window.addEventListener("pointercancel", end); window.addEventListener("blur", end);
  }
  function renderTaskRow(task) {
    var row = el("tr", "work-row");
    row.dataset.taskId = task.task_id;
    var order = el("td", "work-order"),
      controls = el("div", "order-controls"),
      handle = el("button", "drag-handle", "⋮⋮"),
      done = el(
        "button",
        "done-circle" + (task.work_status !== "active" ? " done" : ""),
        task.work_status !== "active" ? "✓" : "",
      );
    handle.type = done.type = "button";
    handle.draggable = true;
    handle.disabled = false;
    handle.setAttribute("aria-label", "Drag " + task.title);
    done.setAttribute(
      "aria-label",
      task.work_status === "done"
        ? "Mark " + task.title + " to do"
        : "Mark " + task.title + " done",
    );
    done.addEventListener("click", function () {
      markDone(task);
    });
    handle.addEventListener("dragstart", function (event) {
      draggedTask = task.task_id;
      setDragPayload(event, "application/x-agent-board-task", task.task_id);
    });
    handle.addEventListener("dragend", function () {
      draggedTask = null;
    });
    controls.append(handle, done);
    order.appendChild(controls);
    var identity = el("td", "work-identity"),
      crumb = el(
        "button",
        "task-breadcrumb",
        projectName(
          boardProjects().find(function (p) {
            return p.project_key === taskProjectKey(task);
          }) || { name: task.project_name },
        ),
      ),
      title = el("button", "task-link", task.title || "Untitled task"), pencil = el("button", "rename-pencil", "✎");
    crumb.type = title.type = "button";
    crumb.addEventListener("click", function () {
      if (!afterPendingEdits(function () {})) return;
      selectedProject = taskProjectKey(task);
      queueMode = "all";
      renderBoard(latestBoard);
    });
    title.setAttribute("aria-label", "Open details for " + task.title);
    title.title = "Open details";
    title.addEventListener("click", function () {
      openTaskDialog(task);
    });
    pencil.type = "button"; pencil.setAttribute("aria-label", "Rename " + task.title);
    pencil.addEventListener("click", function () { inlineRename(task, identity, crumb, title, pencil); });
    identity.append(crumb, title, pencil);
    var status = el("td", "work-status");
    status.appendChild(taskStatusSelect(task));
    var due = el("td", "work-due"), dueValue = el("input", "due-input");
    dueValue.type = "date";
    dueValue.value = task.due_date || "";
    dueValue.title = task.due_date ? "Due " + dueText(task.due_date) : "Set due date";
    dueValue.setAttribute("aria-label", "Due date for " + task.title);
    dueValue.addEventListener("change", function () {
      dueValue.disabled = true;
      saveTask(task, "due_date", dueValue.value || null)
        .then(function () { fetchBoard(true); })
        .catch(function (error) { dueValue.value = task.due_date || ""; toast(error.message, true); })
        .finally(function () { dueValue.disabled = false; });
    });
    due.appendChild(dueValue);
    var updated = el("td", "work-updated"),
      updateValue = el("span", "updated", relativeTime(task.last_activity_at));
    updateValue.title = task.last_activity_at
      ? new Date(Number(task.last_activity_at) * 1000).toLocaleString()
      : "No update recorded";
    updated.appendChild(updateValue);
    var priority = el("td", "work-priority");
    priority.appendChild(prioritySelect(task));
    var terminal = el("td", "work-terminal");
    var presence = taskPresence(task), terminalValue = el("span", "runtime presence-" + presence, terminalText(task));
    terminal.appendChild(terminalValue);
    if (presence === "open") {
      var runtime = task.terminal_runtime_state || task.terminal_state;
      if (runtime && runtime !== "gone") terminal.appendChild(el("span", "runtime-secondary", TERMINAL_LABELS[runtime] || runtime));
    }
    var agent = el("td", "work-agent");
    agent.appendChild(el("span", "agent", providerName(task.session_provider)));
    var actions = el("td", "work-actions");
    actions.append(gridLaunchAction(task, "claude"), gridLaunchAction(task, "codex"));
    var cells = { name: identity, status: status, due: due, updated: updated, priority: priority, terminal: terminal, agent: agent };
    row.appendChild(order);
    columnOrder.forEach(function (column) { row.appendChild(cells[column]); });
    row.appendChild(actions);
    return row;
  }
  function reorderTask(task, destinationKey, beforeId) {
    if (reorderLocked()) return announce(reorderLockReason());
    if (task.task_id === beforeId) return;
    return queueReorder("tasks:" + taskProjectKey(task), function () {
      var currentTask = latestBoard.tasks.find(function (item) {
        return item.task_id === task.task_id;
      });
      if (!currentTask) return;
      var all = visibleTasks().filter(function (item) {
        return (
          taskProjectKey(item) === taskProjectKey(task) &&
          item.work_status === currentTask.work_status &&
          taskGroupKey(item) === destinationKey &&
          item.task_id !== task.task_id
        );
      });
      all = sorted(all);
      var at = all.findIndex(function (item) {
        return item.task_id === beforeId;
      });
      all.splice(at < 0 ? all.length : at, 0, currentTask);
      var previous = sorted(
        visibleTasks().filter(function (item) {
          return (
            taskProjectKey(item) === taskProjectKey(currentTask) &&
            item.work_status === currentTask.work_status &&
            taskGroupKey(item) === destinationKey
          );
        }),
      );
      if (
        previous.length === all.length &&
        previous.every(function (item, index) {
          return item.task_id === all[index].task_id;
        })
      )
        return;
      var payload = workspaceEnabled() ? {
        list_key: taskProjectKey(task),
        task_ids: all.map(function (item) { return item.task_id; }),
      } : {
        project_key: task.project_key,
        task_ids: all.map(function (item) {
          return item.task_id;
        }),
      };
      if (
        groupBy === "priority" &&
        currentTask.work_status === "active" &&
        currentTask.priority !== destinationKey
      )
        payload.priority = destinationKey;
      return mutate(workspaceEnabled() ? "/api/workspace/tasks/reorder" : "/api/tasks/reorder", "POST", payload).then(
        function (response) {
          (response.tasks || []).forEach(mergeTask);
          if (!hasProtectedWorkControls()) renderBoard(latestBoard);
        },
      );
    }).catch(function (error) {
      toast(error.message, true);
    });
  }
  function moveTaskToList(task, listKey) {
    var expected = taskProjectKey(task);
    if (!listKey || listKey === expected) return Promise.resolve(function () {});
    return mutate("/api/workspace/tasks/" + encodeURIComponent(task.task_id) + "/move", "POST", {
      list_key: listKey,
      expected_list_key: expected,
    }).then(function (response) {
      var context = response.context || {}, moved = Object.assign({}, task, context, { list_key: context.list_key || listKey, project_key: task.project_key });
      mergeTask(moved);
      if (!hasProtectedWorkControls()) renderBoard(latestBoard);
      var undone = false;
      var undo = function () {
        if (undone) return Promise.resolve();
        undone = true;
        return mutate("/api/workspace/tasks/" + encodeURIComponent(task.task_id) + "/move", "POST", {
          list_key: expected,
          expected_list_key: context.list_key || listKey,
        }).then(function (undoResponse) {
          mergeTask(Object.assign({}, moved, undoResponse.context || {}, { list_key: expected }));
          if (!hasProtectedWorkControls()) renderBoard(latestBoard);
          toast("Task move undone");
        }).catch(function (error) { toast("Could not undo task move: " + error.message, true); });
      };
      toastUndo("Task moved to " + (context.list_name || listKey) + ".", undo);
      return undo;
    }).catch(function (error) { toast(error.message, true); throw error; });
  }
  function moveTask(task, direction) {
    if (reorderLocked()) return announce(reorderLockReason());
    var peers = sorted(
      visibleTasks().filter(function (item) {
        return (
          taskProjectKey(item) === taskProjectKey(task) &&
          item.work_status === task.work_status &&
          taskGroupKey(item) === taskGroupKey(task)
        );
      }),
    );
    var index = peers.findIndex(function (item) {
        return item.task_id === task.task_id;
      }),
      target = index + direction;
    if (target < 0 || target >= peers.length)
      return announce(
        "Task is already at the " + (direction < 0 ? "top." : "bottom."),
      );
    reorderTask(
      task,
      taskGroupKey(task),
      direction < 0 ? peers[target].task_id : (peers[target + 1] || {}).task_id,
    );
  }
  function appendGroup(root, key, label, tasks) {
    var section = el("section", "project-group"),
      heading = el("h3", "project-heading"), groupId = "group-" + (selectedProject || queueMode) + "-" + groupBy + "-" + key,
      toggle = el("button", "group-toggle", "⌄"), content = el("div", "group-content");
    section.dataset.groupKey = key;
    toggle.type = "button"; toggle.id = "group-toggle-" + groupId; toggle.setAttribute("aria-controls", groupId); toggle.setAttribute("aria-expanded", String(!collapsedGroups[groupId])); toggle.setAttribute("aria-label", "Toggle " + label);
    toggle.addEventListener("click", function () { collapsedGroups[groupId] = !collapsedGroups[groupId]; storeViewSettings(); renderBoardWithFocus(toggle.id); });
    heading.append(
      toggle,
      el("span", groupBy === "priority" ? "priority-heading priority-" + key : "", label),
      el(
        "span",
        "count",
        tasks.length + (tasks.length === 1 ? " task" : " tasks"),
      ),
    );
    var table = el("table", "work-table"),
      thead = document.createElement("thead"),
      headRow = document.createElement("tr"),
      body = document.createElement("tbody");
    headRow.appendChild(header("", "column-order"));
    var definitions = { name: ["Name / List", "column-name", "name"], status: ["Task status", "column-status"], due: ["Due date", "column-due", "due"], updated: ["Last update", "column-updated", "updated"], priority: ["Priority", "column-priority", "priority"], terminal: ["Terminal state", "column-terminal", "terminal"], agent: ["Agent", "column-agent", "agent"] };
    columnOrder.forEach(function (column) { var definition = definitions[column]; headRow.appendChild(header(definition[0], definition[1], definition[2], column, groupId)); });
    headRow.appendChild(header("Actions", "column-actions"));
    thead.appendChild(headRow);
    sorted(tasks).forEach(function (task) {
      var row = renderTaskRow(task);
      row.addEventListener("dragover", function (event) {
        if (draggedTask) allowMoveDrop(event);
      });
      row.addEventListener("drop", function (event) {
        event.preventDefault();
        var dragged = latestBoard.tasks.find(function (item) {
          return item.task_id === draggedTask;
        });
        if (!dragged) return;
        if (reorderLocked()) return announce(reorderLockReason());
        if (taskProjectKey(dragged) !== taskProjectKey(task))
          return workspaceEnabled() ? moveTaskToList(dragged, taskProjectKey(task)) : announce("Tasks cannot move between projects.");
        if (dragged.work_status !== task.work_status)
          return announce(
            "Done and archived rows keep separate work status while reordering.",
          );
        if (groupBy === "priority" && key !== dragged.priority) {
          if (queueMode === "closed")
            return announce("Closed rows cannot change priority by dragging.");
          return reorderTask(dragged, key, task.task_id);
        }
        if (key !== taskGroupKey(dragged))
          return announce(
            "Terminal state is runtime truth; tasks cannot move between terminal groups.",
          );
        reorderTask(dragged, key, task.task_id);
      });
      body.appendChild(row);
    });
    section.addEventListener("dragover", function (event) {
      if (draggedTask) allowMoveDrop(event);
    });
    section.addEventListener("drop", function (event) {
      if (event.target.closest("tr")) return;
      event.preventDefault();
      var dragged = latestBoard.tasks.find(function (item) {
        return item.task_id === draggedTask;
      });
      if (!dragged) return;
      if (reorderLocked()) return announce(reorderLockReason());
      if (groupBy === "priority" && key !== dragged.priority) {
        if (queueMode === "closed")
          return announce("Closed rows cannot change priority by dragging.");
        return reorderTask(dragged, key, null);
      }
      if (key === taskGroupKey(dragged)) reorderTask(dragged, key, null);
    });
    table.append(thead, body);
    content.id = groupId; content.hidden = Boolean(collapsedGroups[groupId]);
    section.appendChild(heading);
    if (tasks.length) content.appendChild(table);
    else section.classList.add("empty-group");
    if (!tasks.length) content.appendChild(el("p", "compact-drop-target", "Drop a task here"));
    section.appendChild(content);
    root.appendChild(section);
  }
  function renderBoard(data) {
    if (!data) return;
    latestBoard = data;
    if (document.documentElement && document.documentElement.style) document.documentElement.style.setProperty("--sidebar-width", sidebarWidth + "px");
    if (selectedProject && !selectedProjectData()) selectedProject = null;
    renderSidebar(data);
    renderProjectDetail();
    syncToolbarControls();
    updateReorderReason();
    var filterCount = Object.values(filters).filter(function (value) {
      return value !== "any";
    }).length;
    $("filter-summary").textContent = filterCount
      ? filterCount + " Filters"
      : "Filters";
    $("filter-summary").setAttribute("data-active", filterCount ? "true" : "false");
    $("today-note").hidden = queueMode !== "today";
    $("work-heading").textContent = selectedProjectData()
      ? projectName(selectedProjectData())
      : queueMode === "today"
        ? "Today"
        : queueMode === "open"
        ? "Open terminals on this Mac"
        : statusFilter === "completed"
          ? "Completed"
          : statusFilter === "archived"
            ? "Archived"
            : statusFilter === "all"
              ? "All work"
              : "All threads";
    var unknown = (latestBoard.tasks || []).filter(function (task) { return taskPresence(task) === "unknown"; }).length;
    $("unknown-count").textContent = unknown ? "(" + unknown + " unknown)" : "";
    $("presence-note").hidden = !unknown;
    $("presence-note").textContent = unknown ? "This Mac could not verify " + unknown + " saved thread" + (unknown === 1 ? "." : "s.") + " Find them in All threads." : "";
    var root = $("task-groups"),
      tasks = visibleTasks();
    root.replaceChildren();
    if (!tasks.length) return empty(root, queueMode === "open" ? "No verified open terminals. Choose All threads to review saved work." : "No tasks match this view.");
    if (groupBy === "none") return appendGroup(root, "all", "Tasks", tasks);
    var keys = groupBy === "priority" ? PRIORITY_GROUPS : TERMINAL_GROUPS,
      labels = groupBy === "priority" ? PRIORITY_LABELS : TERMINAL_LABELS;
    keys.forEach(function (key) {
      var matches = tasks.filter(function (task) {
        return taskGroupKey(task) === key;
      });
      if (matches.length || groupBy === "priority") {
        appendGroup(root, key, labels[key], matches);
      }
    });
  }
  function renderBoardWithFocus(id) {
    renderBoard(latestBoard);
    var control = $(id);
    if (control && typeof control.focus === "function") control.focus();
  }
  function fetchBoard(force) {
    if (!force && hasProtectedWorkControls()) return Promise.resolve();
    var seq = ++boardSeq;
    return request("/api/board")
      .then(function (data) {
        if (seq === boardSeq && !hasProtectedWorkControls()) {
          $("board-error").hidden = true;
          renderBoard(data);
        }
      })
      .catch(function (error) {
        if (seq === boardSeq) {
          $("board-error").textContent = error.message;
          $("board-error").hidden = false;
        }
      });
  }
  function scheduleBoardPoll() {
    boardTimer = setTimeout(function () {
      fetchBoard().finally(scheduleBoardPoll);
    }, 10000);
  }
  function validSavedViews(value) {
    return Array.isArray(value)
      ? value
          .filter(function (item) {
            return (
              item &&
              typeof item.name === "string" &&
              item.name.trim() &&
              item.snapshot &&
              typeof item.snapshot === "object"
            );
          })
          .map(function (item) {
            return { name: item.name.slice(0, 80), snapshot: item.snapshot };
          })
      : [];
  }
  function savedViews() {
    try {
      return validSavedViews(
        JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || "[]"),
      );
    } catch (_error) {
      return [];
    }
  }
  function storeSavedViews(views) {
    try {
      localStorage.setItem(
        SAVED_VIEWS_KEY,
        JSON.stringify(validSavedViews(views)),
      );
      return true;
    } catch (_error) {
      toast("Saved views are unavailable in this browser.", true);
      return false;
    }
  }
  function snapshotView() {
    return {
      scope: queueMode,
      status: statusFilter,
      project: selectedProject,
      group: groupBy,
      sort: sortBy,
      sortDirection: sortDirection,
      columns: columnOrder.slice(),
      columnWidths: Object.assign({}, columnWidths),
      collapsedGroups: Object.assign({}, collapsedGroups),
      filters: Object.assign({}, filters),
    };
  }
  function normalizedColumns(columns) {
    if (!Array.isArray(columns) || new Set(columns).size !== columns.length || !columns.every(function (column) { return DATA_COLUMNS.indexOf(column) >= 0 || column === "name" || column === "due" || column === "updated" || column === "priority" || column === "terminal" || column === "agent"; })) return DATA_COLUMNS.slice();
    var result = columns.filter(function (column) { return DATA_COLUMNS.indexOf(column) >= 0; });
    if (result.indexOf("status") < 0) result.splice(Math.max(0, result.indexOf("name") + 1), 0, "status");
    return result.length === DATA_COLUMNS.length && new Set(result).size === DATA_COLUMNS.length ? result : DATA_COLUMNS.slice();
  }
  function applySnapshot(snapshot) {
    if (projectDescriptionDirty())
      return announce(
        "Save or cancel the project description before changing views.",
      );
    queueMode = snapshot.scope === "closed" || snapshot.scope === "completed" || snapshot.scope === "archived" || snapshot.scope === "all-status" ? "all" : ["open", "all", "today"].indexOf(snapshot.scope) >= 0 ? snapshot.scope : "open";
    statusFilter = snapshot.scope === "closed" || snapshot.scope === "completed"
      ? "completed"
      : snapshot.scope === "archived"
        ? "archived"
        : snapshot.scope === "all-status"
          ? "all"
          : ["active", "completed", "archived", "all"].indexOf(snapshot.status) >= 0
            ? snapshot.status
            : "active";
    selectedProject =
      typeof snapshot.project === "string" ? snapshot.project : null;
    groupBy =
      ["priority", "terminal", "none"].indexOf(snapshot.group) >= 0
        ? snapshot.group
        : "priority";
    sortBy =
      ["manual", "name", "updated", "due", "priority", "terminal", "agent"].indexOf(snapshot.sort) >= 0
        ? snapshot.sort
        : "manual";
    sortDirection = snapshot.sortDirection === "desc" ? "desc" : "asc";
    columnOrder = normalizedColumns(snapshot.columns);
    columnWidths = Object.create(null);
    if (snapshot.columnWidths && typeof snapshot.columnWidths === "object") Object.keys(snapshot.columnWidths).forEach(function (column) { if (DATA_COLUMNS.indexOf(column) >= 0) setColumnWidth(column, snapshot.columnWidths[column]); });
    collapsedGroups = snapshot.collapsedGroups && typeof snapshot.collapsedGroups === "object" ? Object.assign(Object.create(null), snapshot.collapsedGroups) : Object.create(null);
    var savedFilters = snapshot.filters || {};
    filters = {
      provider:
        ["any", "claude", "codex"].indexOf(savedFilters.provider) >= 0
          ? savedFilters.provider
          : "any",
      priority:
        ["any", "urgent", "high", "normal", "low"].indexOf(
          savedFilters.priority,
        ) >= 0
          ? savedFilters.priority
          : "any",
      terminal:
        ["any"].concat(TERMINAL_GROUPS).indexOf(savedFilters.terminal) >= 0
          ? savedFilters.terminal
          : "any",
      due:
        ["any", "today", "none"].indexOf(savedFilters.due) >= 0
          ? savedFilters.due
          : "any",
      presence: ["any", "open", "closed", "unknown"].indexOf(savedFilters.presence) >= 0 ? savedFilters.presence : "any",
      lastUpdate: ["any", "24h", "7d", "30d"].indexOf(savedFilters.lastUpdate) >= 0 ? savedFilters.lastUpdate : "any",
    };
    renderBoard(latestBoard);
  }
  function storeViewSettings() {
    try {
      var saved = JSON.parse(localStorage.getItem(VIEW_SETTINGS_KEY) || "{}"), views = saved.views || {};
      views[(selectedProject || queueMode) + ""] = { sort: sortBy, sortDirection: sortDirection, columns: columnOrder, widths: columnWidths, collapsedGroups: collapsedGroups };
      localStorage.setItem(VIEW_SETTINGS_KEY, JSON.stringify({ views: views, sidebarWidth: sidebarWidth, sidebarManualOrder: sidebarManualOrder }));
    } catch (_error) {}
  }
  function restoreViewSettings() {
    columnOrder = DATA_COLUMNS.slice();
    columnWidths = Object.create(null);
    collapsedGroups = Object.create(null);
    sortBy = "manual";
    sortDirection = "asc";
    sidebarWidth = 250;
    sidebarManualOrder = false;
    try {
      var saved = JSON.parse(localStorage.getItem(VIEW_SETTINGS_KEY) || "{}");
      if (!saved || typeof saved !== "object" || Array.isArray(saved)) return;
      var settings = (saved.views || {})[(selectedProject || queueMode) + ""] || {};
      if (!settings || typeof settings !== "object" || Array.isArray(settings)) settings = {};
      if (["manual", "name", "updated", "due", "priority", "terminal", "agent"].indexOf(settings.sort) >= 0) sortBy = settings.sort;
      sortDirection = settings.sortDirection === "desc" ? "desc" : "asc";
      columnOrder = normalizedColumns(settings.columns);
      if (settings.widths && typeof settings.widths === "object") Object.keys(settings.widths).forEach(function (column) { if (DATA_COLUMNS.indexOf(column) >= 0) setColumnWidth(column, settings.widths[column]); });
      collapsedGroups = settings.collapsedGroups && typeof settings.collapsedGroups === "object" ? Object.assign(Object.create(null), settings.collapsedGroups) : Object.create(null);
      sidebarWidth = Math.max(190, Math.min(480, Number(saved.sidebarWidth === undefined ? settings.sidebarWidth : saved.sidebarWidth) || 250));
      sidebarManualOrder = Boolean(saved.sidebarManualOrder === undefined ? settings.sidebarManualOrder : saved.sidebarManualOrder);
    } catch (_error) {}
  }
  function renderSavedViews() {
    var root = $("saved-view-list");
    root.replaceChildren();
    savedViews().forEach(function (view, index) {
      var row = el("div", "project-row"),
        button = el(
          "button",
          "saved-view" + (activeSavedView === index ? " active" : ""),
          view.name,
        );
      button.type = "button";
      button.addEventListener("click", function () {
        activeSavedView = index;
        applySnapshot(view.snapshot);
      });
      row.append(
        button,
        rowMenu([
          [
            "Rename",
            function () {
              openEditor(
                "Rename saved view",
                "View name",
                view.name,
                function (name) {
                  var views = savedViews();
                  views[index].name = name;
                  storeSavedViews(views);
                },
              );
            },
          ],
          [
            "Delete",
            function () {
              var views = savedViews();
              views.splice(index, 1);
              activeSavedView = null;
              storeSavedViews(views);
              renderSavedViews();
            },
          ],
        ]),
      );
      root.appendChild(row);
    });
  }
  function fetchSessions() {
    var params = new URLSearchParams(),
      query = $("search-input").value.trim();
    if (query) params.set("q", query);
    if ($("here-toggle").checked) params.set("here", "1");
    var seq = ++sessionsSeq;
    request("/api/sessions?" + params)
      .then(function (data) {
        if (seq === sessionsSeq) renderSessionList(data.sessions || []);
      })
      .catch(function (error) {
        if (seq === sessionsSeq) empty($("session-list"), error.message);
      });
  }
  function renderSessionList(sessions) {
    var list = $("session-list");
    list.replaceChildren();
    if (!sessions.length) return empty(list, "No sessions found.");
    sessions.forEach(function (session) {
      var row = el(
          "button",
          "session-row" + (session.session_id === activeSid ? " active" : ""),
        ),
        top = el("span", "session-row-top");
      row.type = "button";
      top.append(
        el("strong", "", session.folder || "?"),
        el("span", "", (session.when || "") + " · " + session.provider_name),
      );
      row.append(top, el("span", "session-row-title", session.title));
      row.addEventListener("click", function () {
        selectSession(session.session_id);
      });
      list.appendChild(row);
    });
  }
  function updateHistoryActions(meta) {
    var taskLaunch = meta.task_launch,
      actions = taskLaunch ? taskLaunch.actions : meta.actions,
      isHistorical = Boolean(taskLaunch && taskLaunch.session_id !== meta.session_id);
    ["claude", "codex"].forEach(function (provider) {
      var action = (actions || {})[provider] || {
          label: providerName(provider),
          available: false,
          reason: "Unavailable",
        },
        button = $("thread-" + provider),
        reason = $("thread-" + provider + "-reason"),
        key = taskLaunch ? "task:" + taskLaunch.task_id + ":" + provider : meta.session_id + ":" + provider;
      button.textContent = action.label + (isHistorical ? " (current task)" : "");
      button.dataset.launchKey = key;
      button.dataset.launchAvailable = String(Boolean(action.available));
      button.disabled = !action.available || Boolean(launchesInFlight[key]);
      button.title = action.available ? "" : action.reason || "Unavailable";
      reason.textContent = action.available
        ? ""
        : action.reason || "Unavailable";
      reason.hidden = !isHistorical && !$("thread-error").hidden &&
        /transcript/i.test(action.reason || "");
    });
  }
  function selectSession(sid) {
    activeSid = sid;
    activeSessionMeta = null;
    $("thread-search").value = "";
    $("thread-search-count").textContent = "";
    $("thread-actions").hidden = true;
    $("thread-error").hidden = true;
    $("viewer-title").textContent = "Loading…";
    $("viewer-meta").textContent = "";
    $("transcript").replaceChildren();
    request("/api/session/" + encodeURIComponent(sid))
      .then(function (data) {
        if (sid !== activeSid) return;
        activeSessionMeta = data.meta;
        currentTurns = data.turns || [];
        $("viewer-title").textContent = data.meta.title;
        $("viewer-meta").textContent = [
          data.meta.folder,
          data.meta.cwd,
          data.meta.provider_name,
          data.meta.msg_count + " messages",
          data.meta.task_launch ? "Next launch: " + (data.meta.task_launch.working_directory || "unlinked") : "",
        ]
          .filter(Boolean)
          .join(" · ");
        $("thread-error").textContent = data.transcript_error || "";
        $("thread-error").hidden = !data.transcript_error;
        if (readerMode === "history") {
          updateHistoryActions(data.meta);
          $("thread-actions").hidden = false;
        } else {
          $("thread-actions").hidden = true;
          var task = latestBoard && latestBoard.tasks.find(function (item) { return item.task_id === $("task-dialog").dataset.taskId; });
          if (task) updateTaskActions(task);
        }
        var missingTranscript = Boolean(data.transcript_error && !(data.turns || []).length);
        $("thread-search").hidden = missingTranscript;
        document.querySelector(".thread-search-label").hidden = missingTranscript;
        renderTranscript("");
      })
      .catch(function (error) {
        if (sid === activeSid) {
          $("viewer-title").textContent = "Could not load session";
          $("viewer-meta").textContent = error.message;
        }
      });
  }
  function renderTranscript(query) {
    var transcript = $("transcript");
    transcript.replaceChildren();
    if (!currentTurns || !currentTurns.length) {
      if (!$('thread-error').hidden) return;
      return empty(transcript, "No messages in this session.");
    }
    var lower = query.trim().toLowerCase(),
      count = 0;
    currentTurns.forEach(function (turn) {
      var match = !lower || String(turn.text).toLowerCase().indexOf(lower) >= 0;
      if (match) count += 1;
      var wrapper = el(
          "div",
          "turn role-" +
            (turn.role === "user" ? "user" : "assistant") +
            (match ? "" : " thread-search-hidden"),
        ),
        body = el("div", "turn-body");
      body.innerHTML = renderTurnBody(turn.text);
      wrapper.append(
        el("div", "turn-role", turn.role === "user" ? "User" : "Assistant"),
        body,
      );
      transcript.appendChild(wrapper);
    });
    $("thread-search-count").textContent = lower
      ? count + " matching turn" + (count === 1 ? "" : "s")
      : "";
  }
  function launchActiveThread(provider) {
    if (!activeSessionMeta) return Promise.resolve();
    var taskLaunch = activeSessionMeta.task_launch,
      key = taskLaunch ? "task:" + taskLaunch.task_id + ":" + provider : activeSessionMeta.session_id + ":" + provider,
      path = taskLaunch
        ? "/api/tasks/" + encodeURIComponent(taskLaunch.task_id) + "/launch"
        : "/api/sessions/" + encodeURIComponent(activeSessionMeta.session_id) + "/launch",
      payload = taskLaunch
        ? { provider: provider, full_access: fullAccessEnabled(), launch_revision: taskLaunch.launch_revision }
        : { provider: provider, full_access: fullAccessEnabled() };
    if (launchesInFlight[key]) return Promise.resolve();
    launchesInFlight[key] = true;
    setLaunchBusy(key, true);
    return mutate(path, "POST", payload)
      .then(function () {
        toast("Launch requested — check Terminal");
      })
      .catch(function (error) {
        toast(error.message, true);
      })
      .finally(function () {
        delete launchesInFlight[key];
        setLaunchBusy(key, false);
      });
  }
  function activateTab(tab) {
    var board = tab.dataset.view === "board";
    $("board-view").hidden = !board;
    $("history-view").hidden = board;
    Array.prototype.forEach.call(
      document.querySelectorAll(".tab"),
      function (item) {
        var active = item === tab;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      },
    );
    if (board) fetchBoard();
    else fetchSessions();
  }
  Array.prototype.forEach.call(
    document.querySelectorAll(".tab"),
    function (tab) {
      tab.addEventListener("click", function () {
        activateTab(tab);
      });
    },
  );
  if (typeof document.addEventListener === "function") document.addEventListener("click", function (event) {
    if (event.target && typeof event.target.closest === "function" && event.target.closest(".priority-menu")) return;
    Array.prototype.forEach.call(document.querySelectorAll(".priority-menu[open]"), function (menu) { menu.open = false; });
  });
  Array.prototype.forEach.call(
    document.querySelectorAll(".scope-tab"),
    function (button) {
      button.addEventListener("click", function () {
        selectScope(button.dataset.scope);
      });
    },
  );
  $("group-by").addEventListener("change", function () {
    groupBy = this.value;
    activeSavedView = null;
    renderBoard(latestBoard);
  });
  $("sort-by").addEventListener("change", function () {
    sortBy = this.value;
    sortDirection = sortBy === "updated" ? "desc" : "asc";
    storeViewSettings();
    activeSavedView = null;
    renderBoard(latestBoard);
  });
  $("filter-status").addEventListener("change", function () {
    selectStatus(this.value);
  });
  ["provider", "priority", "terminal", "due", "presence", "last-update"].forEach(function (name) {
    $("filter-" + name).addEventListener("change", function () {
      filters[name === "last-update" ? "lastUpdate" : name] = this.value;
      activeSavedView = null;
      renderBoard(latestBoard);
    });
  });
  $("clear-filters").addEventListener("click", function () {
    filters = { provider: "any", priority: "any", terminal: "any", due: "any", presence: "any", lastUpdate: "any" };
    ["provider", "priority", "terminal", "due", "presence", "last-update"].forEach(function (name) {
      $("filter-" + name).value = "any";
    });
    renderBoard(latestBoard);
  });
  $("sort-direction").addEventListener("click", function () {
    sortDirection = sortDirection === "asc" ? "desc" : "asc";
    storeViewSettings(); renderBoard(latestBoard);
  });
  if ($("sidebar-sort")) $("sidebar-sort").addEventListener("change", function () {
    sidebarManualOrder = this.value === "manual";
    storeViewSettings();
    renderBoard(latestBoard);
  });
  $("sidebar-resize").addEventListener("pointerdown", beginSidebarResize);
  $("sidebar-resize").addEventListener("keydown", function (event) {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      sidebarWidth = Math.max(190, Math.min(480, sidebarWidth + (event.key === "ArrowLeft" ? -12 : 12)));
      storeViewSettings(); renderBoard(latestBoard);
    }
  });
  $("work-search").addEventListener(
    "input",
    debounce(function () {
      renderBoard(latestBoard);
    }, 100),
  );
  $("edit-project-description").addEventListener("click", function () {
    $("project-description-editor").hidden = false;
    $("project-description").focus();
  });
  $("project-description").addEventListener("input", function () {
    $("description-limit").textContent = this.value.length + " / 10000";
  });
  $("cancel-description").addEventListener("click", function () {
    var project = selectedProjectData();
    $("project-description").value = project ? project.description || "" : "";
    $("project-description-editor").hidden = true;
    $("description-error").hidden = true;
  });
  $("save-description").addEventListener("click", function () {
    var project = selectedProjectData();
    if (!project || this.disabled) return;
    var description = $("project-description");
    $("save-description").disabled =
      $("cancel-description").disabled =
      description.disabled =
        true;
    mutate(
      workspaceEnabled() ? "/api/workspace/lists/" + encodeURIComponent(project.project_key) : "/api/projects/" + encodeURIComponent(project.project_key),
      "PATCH",
      { description: description.value },
    )
      .then(function (response) {
        project.description = (response.list || response.project).description;
        $("project-description-editor").hidden = true;
        $("description-error").hidden = true;
        renderProjectDetail();
        toast("Project description saved");
      })
      .catch(function (error) {
        $("description-error").textContent = error.message;
        $("description-error").hidden = false;
      })
      .finally(function () {
        $("save-description").disabled =
          $("cancel-description").disabled =
          description.disabled =
            false;
      });
  });
  $("new-space").addEventListener("click", function () {
    openEditor("New Space", "Space name", "", function (name) {
      return mutate("/api/workspace/spaces", "POST", { name: name });
    });
  });
  $("save-view").addEventListener("click", function () {
    openEditor("Save view", "View name", "", function (name) {
      var views = savedViews();
      views.push({ name: name, snapshot: snapshotView() });
      storeSavedViews(views);
    });
  });
  $("editor-form").addEventListener("submit", function (event) {
    event.preventDefault();
    if (!editorAction) return;
    var name = $("editor-input").value.trim();
    if (!name && !editorAllowEmpty) {
      $("editor-error").textContent = "A name is required.";
      $("editor-error").hidden = false;
      return;
    }
    $("editor-submit").disabled = true;
    Promise.resolve(editorAction(name))
      .then(function () {
        $("editor-dialog").close();
        editorAction = null;
        return fetchBoard(true);
      })
      .catch(function (error) {
        $("editor-error").textContent = error.message;
        $("editor-error").hidden = false;
      })
      .finally(function () {
        $("editor-submit").disabled = false;
      });
  });
  $("editor-cancel").addEventListener("click", function () {
    if ($("editor-submit").disabled) return;
    editorAction = null;
    $("editor-dialog").close();
  });
  $("close-task-dialog").addEventListener("click", function () {
    closeTaskDialog();
  });
  function closeTaskDialog() {
    if (document.activeElement && document.activeElement.blur)
      document.activeElement.blur();
    var dialog = $("task-dialog"),
      version = dialog.dataset.openVersion,
      pending = rowMutationTails[dialog.dataset.taskId];
    Promise.allSettled(pending ? [pending] : []).then(function () {
      if (!dialog.open || dialog.dataset.openVersion !== version) return;
      if ($("dialog-task-fields").querySelector('[aria-invalid="true"]')) {
        toast(
          "An edit did not save. Correct it before closing, or use Discard unsaved edits.",
          true,
        );
        $("discard-task-edits").hidden = false;
        return;
      }
      $("task-dialog").close();
    });
  }
  $("task-dialog").addEventListener("cancel", function (event) {
    event.preventDefault();
    closeTaskDialog();
  });
  $("discard-task-edits").addEventListener("click", function () {
    $("task-dialog").close();
  });
  $("task-dialog").addEventListener("close", function () {
    readerMode = "history";
    $("history-view").appendChild($("viewer"));
    $("topbar").appendChild($("full-access-label"));
    $("discard-task-edits").hidden = true;
    renderBoard(latestBoard);
    fetchBoard();
  });
  $("search-input").addEventListener("input", debounce(fetchSessions, 250));
  $("here-toggle").addEventListener("change", fetchSessions);
  $("thread-search").addEventListener(
    "input",
    debounce(function () {
      renderTranscript($("thread-search").value);
    }, 120),
  );
  $("thread-claude").addEventListener("click", function () {
    launchActiveThread("claude");
  });
  $("thread-codex").addEventListener("click", function () {
    launchActiveThread("codex");
  });
  $("list-claude").addEventListener("click", function () { var project = selectedProjectData(); if (project) launchList(project, "claude"); });
  $("list-codex").addEventListener("click", function () { var project = selectedProjectData(); if (project) launchList(project, "codex"); });
  $("task-claude").addEventListener("click", function () { var task = latestBoard && latestBoard.tasks.find(function (item) { return item.task_id === $("task-dialog").dataset.taskId; }); if (task) continueOrFocusTask(task, "claude"); });
  $("task-codex").addEventListener("click", function () { var task = latestBoard && latestBoard.tasks.find(function (item) { return item.task_id === $("task-dialog").dataset.taskId; }); if (task) continueOrFocusTask(task, "codex"); });
  $("task-fresh-claude").addEventListener("click", function () { var task = latestBoard && latestBoard.tasks.find(function (item) { return item.task_id === $("task-dialog").dataset.taskId; }); if (task) startFreshTask(task, "claude"); });
  $("task-fresh-codex").addEventListener("click", function () { var task = latestBoard && latestBoard.tasks.find(function (item) { return item.task_id === $("task-dialog").dataset.taskId; }); if (task) startFreshTask(task, "codex"); });
  restoreViewSettings();
  request("/api/meta")
    .then(function (meta) {
      csrfToken = meta.csrf_token;
      if (meta.here_only_forced) {
        $("here-toggle").checked = true;
        $("here-toggle").disabled = true;
      }
      return fetchBoard();
    })
    .then(scheduleBoardPoll)
    .catch(function (error) {
      toast(error.message, true);
    });
  window.addEventListener("pagehide", function () {
    if (boardTimer) clearTimeout(boardTimer);
  });
  window.addEventListener("beforeunload", function (event) {
    if (
      projectDescriptionDirty() ||
      Object.keys(rowMutationTails).length ||
      document.querySelector("dialog[open]")
    ) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
})();
