// Agent Board local UI. Vanilla JS; all persistence is browser-local or local API calls.
(function () {
  "use strict";
  var csrfToken = "",
    latestBoard = null,
    activeSid = null,
    activeSessionMeta = null,
    currentTurns = null;
  var queueMode = "all",
    groupBy = "priority",
    sortBy = "manual",
    selectedProject = null,
    draggedTask = null,
    draggedProject = null;
  var filters = {
      provider: "any",
      priority: "any",
      terminal: "any",
      due: "any",
    },
    collapsedFolders = Object.create(null);
  var boardTimer = null,
    boardSeq = 0,
    sessionsSeq = 0,
    rowMutationTails = Object.create(null),
    reorderMutationTails = Object.create(null),
    editRevisionCounters = Object.create(null),
    launchesInFlight = Object.create(null);
  var editorAction = null,
    editorAllowEmpty = false,
    activeSavedView = null,
    editClientId =
      window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID()
        : String(Date.now()) + "-" + Math.random();
  var PRIORITY_GROUPS = ["urgent", "high", "normal", "low"],
    TERMINAL_GROUPS = ["needs-input", "working", "idle", "ended", "gone"];
  var PRIORITY_LABELS = {
      urgent: "⚑ Urgent",
      high: "⚑ High",
      normal: "Normal",
      low: "Low",
    },
    TERMINAL_LABELS = {
      "needs-input": "Needs input",
      working: "Working",
      idle: "Idle",
      ended: "Ended",
      gone: "Gone",
    };
  var SAVED_VIEWS_KEY = "agent-board.saved-views.v1";
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
    node.textContent = message;
    node.className = bad ? "show error-toast" : "show";
    setTimeout(function () {
      node.className = "";
    }, 2800);
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
  function fullAccessEnabled() {
    return $("full-access").checked;
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
      filters.provider !== "any" ||
      filters.priority !== "any" ||
      filters.terminal !== "any" ||
      filters.due !== "any"
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
        (sortBy === "updated" ? "Last update" : "Due date") +
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
  function selectedProjectData() {
    return (
      latestBoard &&
      latestBoard.projects.find(function (p) {
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
    if (selectedProject && task.project_key !== selectedProject) return false;
    if (
      queueMode === "closed"
        ? !(task.work_status === "done" || task.work_status === "archived")
        : task.work_status !== "active"
    )
      return false;
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
    if (filters.terminal !== "any" && task.terminal_state !== filters.terminal)
      return false;
    if (filters.due === "today" && !isDueTodayOrOverdue(task)) return false;
    if (filters.due === "none" && task.due_date) return false;
    return true;
  }
  function visibleTasks() {
    return latestBoard ? latestBoard.tasks.filter(taskMatches) : [];
  }
  function taskGroupKey(task) {
    return groupBy === "priority"
      ? task.priority
      : groupBy === "terminal"
        ? task.terminal_state
        : "all";
  }
  function taskComparator(a, b) {
    if (sortBy === "updated")
      return (
        Number(b.last_activity_at || 0) - Number(a.last_activity_at || 0) ||
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
    queueMode = scope;
    selectedProject = null;
    activeSavedView = null;
    renderBoard(latestBoard);
  }
  function renderProjectDetail() {
    var project = selectedProjectData(),
      root = $("project-detail");
    root.hidden = !project;
    if (!project) return;
    $("project-name").textContent = projectName(project);
    $("project-path").textContent = project.path || "";
    $("project-counts").textContent =
      (project.counts || {}).active +
      " active · " +
      (project.counts || {}).today +
      " today";
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
      renderBoard(latestBoard);
    });
    select.addEventListener("dragstart", function (event) {
      draggedProject = project.project_key;
      event.dataTransfer.effectAllowed = "move";
    });
    select.addEventListener("dragend", function () {
      draggedProject = null;
    });
    row.append(select, projectMenu(project));
    return row;
  }
  function renderSidebar(data) {
    $("all-count").textContent = data.tasks.filter(function (t) {
      return t.work_status === "active";
    }).length;
    $("today-count").textContent = data.tasks.filter(function (t) {
      return t.work_status === "active" && t.in_today;
    }).length;
    $("closed-count").textContent = data.tasks.filter(function (t) {
      return t.work_status !== "active";
    }).length;
    Array.prototype.forEach.call(
      document.querySelectorAll(".work-scope,.scope-tab"),
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
        if (draggedProject) event.preventDefault();
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
    var select = el("select", "priority-select priority-" + task.priority);
    select.disabled = task.work_status !== "active";
    select.setAttribute("aria-label", "Set priority for " + task.title);
    PRIORITY_GROUPS.forEach(function (priority) {
      var option = el("option", "", PRIORITY_LABELS[priority]);
      option.value = priority;
      option.selected = task.priority === priority;
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      var next = select.value;
      saveTask(task, "priority", next)
        .then(function () {
          toast("Priority updated");
          renderBoard(latestBoard);
        })
        .catch(function (error) {
          select.value = task.priority;
          toast(error.message, true);
        });
    });
    return select;
  }
  function markDone(task) {
    var next = task.work_status === "active" ? "done" : "active";
    saveTask(task, "status", next)
      .then(function () {
        fetchBoard(true);
      })
      .catch(function (error) {
        toast(error.message, true);
      });
  }
  function openTaskDialog(task) {
    $("task-dialog").dataset.taskId = task.task_id;
    $("task-dialog").dataset.openVersion = String(
      Number($("task-dialog").dataset.openVersion || 0) + 1,
    );
    $("dialog-project").textContent = projectName(
      latestBoard.projects.find(function (p) {
        return p.project_key === task.project_key;
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
      ["active", "Active"],
      ["done", "Done"],
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
    selectSession(task.session_id);
  }
  function taskMenu(task) {
    return rowMenu(
      [
        [
          "Open details",
          function () {
            openTaskDialog(task);
          },
        ],
        [
          "Rename",
          function () {
            openEditor("Rename task", "Task name", task.title, function (name) {
              return saveTask(task, "title", name);
            });
          },
        ],
        [
          "Set due date",
          function () {
            openEditor(
              "Set due date",
              "YYYY-MM-DD (leave blank to clear)",
              task.due_date || "",
              function (date) {
                return saveTask(task, "due_date", date || null);
              },
              { allowEmpty: true, type: "date" },
            );
          },
        ],
        [
          task.work_status === "active" ? "Mark done" : "Reopen",
          function () {
            markDone(task);
          },
        ],
        [
          "Move up",
          function () {
            moveTask(task, -1);
          },
        ],
        [
          "Move down",
          function () {
            moveTask(task, 1);
          },
        ],
      ],
      "Task actions for " + task.title,
    );
  }
  function header(text, cls, sort) {
    var th = el("th", cls + (sort ? " sortable" : ""));
    th.scope = "col";
    if (sort) {
      var button = el("button", "", text);
      button.type = "button";
      button.addEventListener("click", function () {
        sortBy = sort;
        $("sort-by").value = sort;
        renderBoard(latestBoard);
      });
      th.appendChild(button);
      if (sortBy === sort)
        th.setAttribute(
          "aria-sort",
          sort === "updated" ? "descending" : "ascending",
        );
    } else th.textContent = text;
    return th;
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
    handle.draggable = !reorderLocked();
    handle.disabled = reorderLocked();
    handle.setAttribute("aria-label", "Drag " + task.title);
    done.setAttribute(
      "aria-label",
      task.work_status === "active"
        ? "Mark " + task.title + " done"
        : "Reopen " + task.title,
    );
    done.addEventListener("click", function () {
      markDone(task);
    });
    handle.addEventListener("dragstart", function (event) {
      draggedTask = task.task_id;
      event.dataTransfer.effectAllowed = "move";
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
          latestBoard.projects.find(function (p) {
            return p.project_key === task.project_key;
          }) || { name: task.project_name },
        ),
      ),
      title = el("button", "task-link", task.title || "Untitled task");
    crumb.type = title.type = "button";
    crumb.addEventListener("click", function () {
      if (!afterPendingEdits(function () {})) return;
      selectedProject = task.project_key;
      queueMode = "all";
      renderBoard(latestBoard);
    });
    title.addEventListener("click", function () {
      openTaskDialog(task);
    });
    identity.append(crumb, title);
    var due = el("td", "work-due"),
      dueValue = el(
        "span",
        "due-label" +
          (task.due_date && dueText(task.due_date) === "Overdue"
            ? " overdue"
            : ""),
        dueText(task.due_date),
      );
    dueValue.title = task.due_date || "";
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
    terminal.appendChild(
      el(
        "span",
        "runtime state-" + task.terminal_state,
        TERMINAL_LABELS[task.terminal_state] ||
          task.terminal_state ||
          "Unknown",
      ),
    );
    var agent = el("td", "work-agent");
    agent.appendChild(el("span", "agent", providerName(task.session_provider)));
    var actions = el("td", "work-actions");
    actions.appendChild(taskMenu(task));
    row.append(
      order,
      identity,
      due,
      updated,
      priority,
      terminal,
      agent,
      actions,
    );
    return row;
  }
  function reorderTask(task, destinationKey, beforeId) {
    if (reorderLocked()) return announce(reorderLockReason());
    if (task.task_id === beforeId) return;
    return queueReorder("tasks:" + task.project_key, function () {
      var currentTask = latestBoard.tasks.find(function (item) {
        return item.task_id === task.task_id;
      });
      if (!currentTask) return;
      var all = visibleTasks().filter(function (item) {
        return (
          item.project_key === task.project_key &&
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
            item.project_key === currentTask.project_key &&
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
      var payload = {
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
      return mutate("/api/tasks/reorder", "POST", payload).then(
        function (response) {
          (response.tasks || []).forEach(mergeTask);
          if (!hasProtectedWorkControls()) renderBoard(latestBoard);
        },
      );
    }).catch(function (error) {
      toast(error.message, true);
    });
  }
  function moveTask(task, direction) {
    if (reorderLocked()) return announce(reorderLockReason());
    var peers = sorted(
      visibleTasks().filter(function (item) {
        return (
          item.project_key === task.project_key &&
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
      heading = el("h3", "project-heading");
    section.dataset.groupKey = key;
    heading.append(
      el("span", "", label),
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
    headRow.append(
      header("", "column-order"),
      header("Name / Project", "column-name"),
      header("Due date", "column-due", "due"),
      header("Last update", "column-updated", "updated"),
      header("Priority", "column-priority"),
      header("Terminal state", "column-terminal"),
      header("Agent", "column-agent"),
      header("", "column-actions"),
    );
    thead.appendChild(headRow);
    sorted(tasks).forEach(function (task) {
      var row = renderTaskRow(task);
      row.addEventListener("dragover", function (event) {
        if (draggedTask) event.preventDefault();
      });
      row.addEventListener("drop", function (event) {
        event.preventDefault();
        var dragged = latestBoard.tasks.find(function (item) {
          return item.task_id === draggedTask;
        });
        if (!dragged) return;
        if (reorderLocked()) return announce(reorderLockReason());
        if (dragged.project_key !== task.project_key)
          return announce("Tasks cannot move between projects.");
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
      if (draggedTask) event.preventDefault();
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
    section.appendChild(heading);
    if (tasks.length) section.appendChild(table);
    else section.classList.add("empty-group");
    root.appendChild(section);
  }
  function renderBoard(data) {
    if (!data) return;
    latestBoard = data;
    if (selectedProject && !selectedProjectData()) selectedProject = null;
    renderSidebar(data);
    renderProjectDetail();
    updateReorderReason();
    var filterCount = Object.values(filters).filter(function (value) {
      return value !== "any";
    }).length;
    $("filter-summary").textContent = filterCount
      ? filterCount + " Filters"
      : "Filters";
    $("today-note").hidden = queueMode !== "today";
    $("work-heading").textContent = selectedProjectData()
      ? projectName(selectedProjectData())
      : queueMode === "today"
        ? "Today"
        : queueMode === "closed"
          ? "Done"
          : "All active";
    var root = $("task-groups"),
      tasks = visibleTasks();
    root.replaceChildren();
    if (!tasks.length) return empty(root, "No tasks match this view.");
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
      project: selectedProject,
      group: groupBy,
      sort: sortBy,
      filters: Object.assign({}, filters),
    };
  }
  function applySnapshot(snapshot) {
    if (projectDescriptionDirty())
      return announce(
        "Save or cancel the project description before changing views.",
      );
    queueMode =
      ["all", "today", "closed"].indexOf(snapshot.scope) >= 0
        ? snapshot.scope
        : "all";
    selectedProject =
      typeof snapshot.project === "string" ? snapshot.project : null;
    groupBy =
      ["priority", "terminal", "none"].indexOf(snapshot.group) >= 0
        ? snapshot.group
        : "priority";
    sortBy =
      ["manual", "updated", "due"].indexOf(snapshot.sort) >= 0
        ? snapshot.sort
        : "manual";
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
    };
    $("group-by").value = groupBy;
    $("sort-by").value = sortBy;
    $("filter-provider").value = filters.provider;
    $("filter-priority").value = filters.priority;
    $("filter-terminal").value = filters.terminal;
    $("filter-due").value = filters.due;
    renderBoard(latestBoard);
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
    ["claude", "codex"].forEach(function (provider) {
      var action = (meta.actions || {})[provider] || {
          label: providerName(provider),
          available: false,
          reason: "Unavailable",
        },
        button = $("thread-" + provider),
        reason = $("thread-" + provider + "-reason"),
        key = meta.session_id + ":" + provider;
      button.textContent = action.label;
      button.dataset.launchKey = key;
      button.dataset.launchAvailable = String(Boolean(action.available));
      button.disabled = !action.available || Boolean(launchesInFlight[key]);
      reason.textContent = action.available
        ? ""
        : action.reason || "Unavailable";
    });
  }
  function selectSession(sid) {
    activeSid = sid;
    activeSessionMeta = null;
    $("thread-search").value = "";
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
        ]
          .filter(Boolean)
          .join(" · ");
        $("thread-error").textContent = data.transcript_error || "";
        $("thread-error").hidden = !data.transcript_error;
        updateHistoryActions(data.meta);
        $("thread-actions").hidden = false;
        $("thread-search").hidden = false;
        document.querySelector(".thread-search-label").hidden = false;
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
    if (!currentTurns || !currentTurns.length)
      return empty(transcript, "No messages in this session.");
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
    if (!activeSessionMeta) return;
    var key = activeSessionMeta.session_id + ":" + provider;
    if (launchesInFlight[key]) return;
    launchesInFlight[key] = true;
    setLaunchBusy(key, true);
    mutate(
      "/api/sessions/" +
        encodeURIComponent(activeSessionMeta.session_id) +
        "/launch",
      "POST",
      { provider: provider, full_access: fullAccessEnabled() },
    )
      .then(function () {
        toast("Opened " + providerName(provider) + " in Terminal");
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
  Array.prototype.forEach.call(
    document.querySelectorAll(".work-scope,.scope-tab"),
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
    activeSavedView = null;
    renderBoard(latestBoard);
  });
  ["provider", "priority", "terminal", "due"].forEach(function (name) {
    $("filter-" + name).addEventListener("change", function () {
      filters[name] = this.value;
      activeSavedView = null;
      renderBoard(latestBoard);
    });
  });
  $("clear-filters").addEventListener("click", function () {
    filters = { provider: "any", priority: "any", terminal: "any", due: "any" };
    ["provider", "priority", "terminal", "due"].forEach(function (name) {
      $("filter-" + name).value = "any";
    });
    renderBoard(latestBoard);
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
      "/api/projects/" + encodeURIComponent(project.project_key),
      "PATCH",
      { description: description.value },
    )
      .then(function (response) {
        project.description = response.project.description;
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
  $("new-folder").addEventListener("click", function () {
    openEditor("New folder", "Folder name", "", function (name) {
      return mutate("/api/folders", "POST", { name: name });
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
