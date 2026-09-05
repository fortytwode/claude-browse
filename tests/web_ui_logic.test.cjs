"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const APP_PATH = path.resolve(__dirname, "../claude_browse/webassets/app.js");
const INITIALIZATION_MARKER =
  'Array.prototype.forEach.call(\n    document.querySelectorAll(".tab"),';

class FakeElement {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.hidden = false;
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.style = {};
    this.classList = {
      add: (name) => {
        if (!this.className.split(/\s+/).includes(name))
          this.className = (this.className + " " + name).trim();
      },
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, enabled) => {
        if (enabled) this.classList.add(name);
      },
    };
  }

  append(...children) {
    children.forEach((child) => this.appendChild(child));
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  dispatch(type, event = {}) {
    for (const listener of this.listeners[type] || []) listener.call(this, event);
  }

  setAttribute() {}
  removeAttribute() {}
  focus() {}
  showModal() {}
  close() {}
}

function localDate(instant, year, monthIndex, day) {
  return class LocalDate extends Date {
    constructor(...args) {
      super(...(args.length ? args : [instant]));
    }

    getFullYear() {
      return year;
    }
    getMonth() {
      return monthIndex;
    }
    getDate() {
      return day;
    }
    toLocaleDateString() {
      return `${year}-${String(monthIndex + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    }
  };
}

function loadApp({ DateImpl = Date, fetchImpl, initialize = false, setTimeoutImpl, clearTimeoutImpl } = {}) {
  const source = fs.readFileSync(APP_PATH, "utf8");
  const markerIndex = source.lastIndexOf(INITIALIZATION_MARKER);
  assert.notEqual(markerIndex, -1, "app.js initialization marker must remain present");

  const elements = new Map();
  const elementFor = (id) => {
    if (!elements.has(id)) elements.set(id, new FakeElement());
    return elements.get(id);
  };
  const document = {
    activeElement: null,
    createElement: (tag) => new FakeElement(tag),
    getElementById: elementFor,
    querySelector: (selector) => selector === ".thread-search-label" ? elementFor("thread-search-label") : null,
    querySelectorAll: () => [],
  };
  const context = {
    Date: DateImpl,
    AbortController,
    Promise,
    console,
    document,
    fetch: fetchImpl || (() => new Promise(() => {})),
    localStorage: { getItem: () => null, setItem() {} },
    setTimeout: setTimeoutImpl || (() => 0),
    clearTimeout: clearTimeoutImpl || (() => {}),
    window: {
      addEventListener() {},
      crypto: { randomUUID: () => "test-client" },
    },
  };
  const testExports = `
globalThis.testApi = {
  fetchBoard,
  renderBoard,
  syncToolbarControls,
  isDueTodayOrOverdue,
  renderTaskRow,
  renderTurnBody,
  moveProject,
  workspacePosition,
  workspaceMenu,
  moveListToFolder,
  moveListToSpaceRoot,
  displayProjects,
  moveTaskToList,
  launchTask,
  loadTaskHistory,
  updateTaskActions,
  selectSession,
  updateHistoryActions,
  launchActiveThread,
  toast,
  toastUndo,
  selectStatus,
  taskComparator,
  snapshotView,
  applySnapshot,
  openEditor,
  reorderTask,
  saveTask,
  taskMatches,
  getState: function () {
    return { latestBoard: latestBoard, queueMode: queueMode, statusFilter: statusFilter, selectedProject: selectedProject, groupBy: groupBy, sortBy: sortBy, sortDirection: sortDirection, columns: columnOrder.slice(), activeSid: activeSid };
  },
  setState: function (next) {
    if (Object.prototype.hasOwnProperty.call(next, "latestBoard")) latestBoard = next.latestBoard;
    if (Object.prototype.hasOwnProperty.call(next, "queueMode")) queueMode = next.queueMode;
    if (Object.prototype.hasOwnProperty.call(next, "statusFilter")) statusFilter = next.statusFilter;
    if (Object.prototype.hasOwnProperty.call(next, "selectedProject")) selectedProject = next.selectedProject;
    if (Object.prototype.hasOwnProperty.call(next, "groupBy")) groupBy = next.groupBy;
    if (Object.prototype.hasOwnProperty.call(next, "sortBy")) sortBy = next.sortBy;
    if (Object.prototype.hasOwnProperty.call(next, "sortDirection")) sortDirection = next.sortDirection;
    if (Object.prototype.hasOwnProperty.call(next, "columns")) columnOrder = next.columns;
    if (Object.prototype.hasOwnProperty.call(next, "filters")) filters = next.filters;
    if (Object.prototype.hasOwnProperty.call(next, "readerMode")) readerMode = next.readerMode;
  }
};
})();`;
  const prefix = initialize ? source.slice(0, source.lastIndexOf("})();")) : source.slice(0, markerIndex);
  vm.runInNewContext(prefix + testExports, context, {
    filename: APP_PATH,
  });
  return { api: context.testApi, elementFor };
}

function activeTask(overrides = {}) {
  return {
    task_id: "task-1",
    project_key: "project-1",
    project_name: "Project one",
    work_status: "active",
    priority: "normal",
    terminal_state: "idle",
    order: 0,
    title: "Task one",
    ...overrides,
  };
}

function findByClass(node, className) {
  if (node.classList && node.classList.contains(className)) return node;
  for (const child of node.children || []) {
    const found = findByClass(child, className);
    if (found) return found;
  }
  return null;
}

test("renderTurnBody escapes fenced code while retaining prose formatting", () => {
  const { api } = loadApp();

  assert.equal(
    api.renderTurnBody("Before **bold**\n\n```html\n<b>unsafe</b>\n```\n\nAfter"),
    "<p>Before <strong>bold</strong></p><pre><code>&lt;b&gt;unsafe&lt;/b&gt;\n</code></pre><p>After</p>",
  );
});

test("due checks use the user's local calendar day rather than the UTC day", () => {
  const india = loadApp({
    DateImpl: localDate("2026-09-04T19:00:00.000Z", 2026, 8, 5),
  }).api;
  assert.equal(india.isDueTodayOrOverdue({ due_date: "2026-09-05" }), true);
  assert.equal(india.isDueTodayOrOverdue({ due_date: "2026-09-06" }), false);

  const america = loadApp({
    DateImpl: localDate("2026-09-06T03:30:00.000Z", 2026, 8, 5),
  }).api;
  assert.equal(america.isDueTodayOrOverdue({ due_date: "2026-09-05" }), true);
  assert.equal(america.isDueTodayOrOverdue({ due_date: "2026-09-06" }), false);
});

test("Today keeps server-selected attention items without dates, but an explicit due filter excludes them", () => {
  const { api, elementFor } = loadApp();
  const attentionItem = activeTask({ in_today: true, due_date: null });
  api.setState({
    queueMode: "today",
    filters: { provider: "any", priority: "any", terminal: "any", due: "any" },
  });
  elementFor("work-search").value = "";
  assert.equal(api.taskMatches(attentionItem), true);

  api.setState({
    filters: { provider: "any", priority: "any", terminal: "any", due: "today" },
  });
  assert.equal(api.taskMatches(attentionItem), false);
});

test("reordering a task before itself is a no-op and does not POST", async () => {
  const requests = [];
  const { api, elementFor } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ tasks: [] }) });
    },
  });
  const task = activeTask();
  api.setState({
    latestBoard: { tasks: [task], projects: [], folders: [] },
    queueMode: "all",
    sortBy: "manual",
    filters: { provider: "any", priority: "any", terminal: "any", due: "any" },
  });
  elementFor("work-search").value = "";

  api.reorderTask(task, "all", task.task_id);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(requests.filter(({ options }) => options.method === "POST").length, 0);
});

test("task field saves are serialized so an older response cannot overwrite the newer edit", async () => {
  const pending = [];
  const { api } = loadApp({
    fetchImpl: () =>
      new Promise((resolve) => {
        pending.push(resolve);
      }),
  });
  const task = activeTask({ title: "original" });
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });

  const first = api.saveTask(task, "title", "first");
  const second = api.saveTask(task, "title", "second");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(pending.length, 1, "the second PATCH waits for the first response");

  pending.shift()({ ok: true, json: () => Promise.resolve({ task: { task_id: "task-1", title: "first" } }) });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(pending.length, 1, "the queued PATCH begins only after the older response settles");
  pending.shift()({ ok: true, json: () => Promise.resolve({ task: { task_id: "task-1", title: "second" } }) });
  await Promise.all([first, second]);

  assert.equal(api.getState().latestBoard.tasks[0].title, "second");
});

test("moving a project swaps only with its folder peer while preserving global positions", async () => {
  const requests = [];
  const { api } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      return new Promise(() => {});
    },
  });
  api.setState({
    latestBoard: {
      tasks: [],
      folders: [],
      projects: [
        { project_key: "first", folder_id: "folder-a" },
        { project_key: "other-folder", folder_id: "folder-b" },
        { project_key: "last", folder_id: "folder-a" },
      ],
    },
  });

  api.moveProject("last", -1);
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(
    JSON.parse(requests[0].options.body),
    { project_keys: ["last", "other-folder", "first"] },
  );
});

test("a dirty project description prevents a task breadcrumb from changing projects", () => {
  const { api, elementFor } = loadApp();
  const task = activeTask();
  api.setState({
    latestBoard: {
      tasks: [task],
      folders: [],
      projects: [
        { project_key: "project-1", name: "Project one", description: "saved" },
        { project_key: "other-project", name: "Other project", description: "saved" },
      ],
    },
    queueMode: "all",
    selectedProject: "other-project",
  });
  elementFor("project-description-editor").hidden = false;
  elementFor("project-description").value = "unsaved draft";

  const row = api.renderTaskRow(task);
  const breadcrumb = findByClass(row, "task-breadcrumb");
  assert.ok(breadcrumb, "task row renders its project breadcrumb");
  breadcrumb.dispatch("click");

  assert.equal(api.getState().selectedProject, "other-project");
  assert.equal(elementFor("project-description").value, "unsaved draft");
});

test("the generic editor Cancel closes without running its save action", () => {
  const { api, elementFor } = loadApp({ initialize: true });
  let saves = 0, closes = 0;
  elementFor("editor-dialog").close = () => { closes += 1; };
  api.openEditor("Rename folder", "Folder name", "Original", () => { saves += 1; });
  elementFor("editor-input").value = "Do not save";
  elementFor("editor-cancel").dispatch("click");
  assert.equal(saves, 0);
  assert.equal(closes, 1);
  const html = fs.readFileSync(path.join(__dirname, "../claude_browse/webassets/index.html"), "utf8");
  assert.match(html, /id="editor-cancel"[^>]*type="button"/);
});

test("the due-date editor accepts an intentional empty value", async () => {
  const { api, elementFor } = loadApp({ initialize: true });
  const values = [];
  api.openEditor("Set due date", "Due date", "2026-09-05", value => { values.push(value); }, { allowEmpty: true, type: "date" });
  elementFor("editor-input").value = "";
  elementFor("editor-form").dispatch("submit", { preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(values, [""]);
  assert.equal(elementFor("editor-input").type, "date");
});

test("a board response started before a successful edit cannot overwrite the edit", async () => {
  const pending = [];
  const { api } = loadApp({ fetchImpl: (path, options) => new Promise(resolve => { pending.push({ path, options, resolve }); }) });
  const task = activeTask({ title: "old" });
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  const read = api.fetchBoard();
  const save = api.saveTask(task, "title", "new");
  await new Promise(resolve => setImmediate(resolve));
  pending.find(item => item.options.method === "PATCH").resolve({ ok: true, json: async () => ({ task: { ...task, title: "new" } }) });
  await save;
  pending.find(item => item.path === "/api/board").resolve({ ok: true, json: async () => ({ tasks: [task], projects: [], folders: [] }) });
  await read;
  assert.equal(api.getState().latestBoard.tasks[0].title, "new");
});

test("a stalled task save times out truthfully and releases its save queue", async () => {
  const timers = [], requests = [];
  const { api } = loadApp({
    setTimeoutImpl: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    fetchImpl: (path, options) => new Promise((resolve, reject) => {
      requests.push({ path, options, resolve });
      options.signal.addEventListener("abort", () => reject(new Error("aborted")));
    }),
  });
  const task = activeTask();
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  const first = api.saveTask(task, "title", "first");
  const rejection = assert.rejects(first, /Saving timed out.*may have reached the server/);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers[0].delay, 15000);
  timers[0].callback();
  await rejection;
  const second = api.saveTask(task, "title", "second");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, 2);
  requests[1].resolve({ ok: true, json: async () => ({ task: { ...task, title: "second" } }) });
  await second;
  assert.equal(api.getState().latestBoard.tasks[0].title, "second");
});

test("closing a detail dialog does not wait for another task's pending save", async () => {
  const { api, elementFor } = loadApp({ initialize: true });
  const task = activeTask();
  api.saveTask(task, "title", "pending");
  const dialog = elementFor("task-dialog");
  dialog.dataset.taskId = "different-task";
  dialog.dataset.openVersion = "1";
  dialog.open = true;
  let closes = 0;
  dialog.close = () => { closes += 1; };
  elementFor("dialog-task-fields").querySelector = () => null;
  elementFor("close-task-dialog").dispatch("click");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closes, 1);
});

test("workspace payload maps Lists into display projects without changing source identity", () => {
  const { api } = loadApp();
  const board = {
    workspace: {
      spaces: [{ space_id: "general", name: "General", position: 0 }],
      folders: [{ folder_id: "ops", space_id: "general", name: "Operations", position: 0 }],
      lists: [{ list_key: "list:yoga", space_id: "general", folder_id: "ops", name: "Yoga Nidra", description: "Launch notes", working_directory: null, folder_status: "unlinked", position: 0, source_project_key: "yoga" }],
    },
    projects: [{ project_key: "yoga", name: "Source repo" }],
    tasks: [activeTask({ project_key: "yoga", list_key: "list:yoga", list_name: "Yoga Nidra" })],
  };
  const projects = api.displayProjects(board);
  assert.deepEqual(JSON.parse(JSON.stringify(projects)), [{ project_key: "list:yoga", source_project_key: "yoga", name: "Yoga Nidra", description: "Launch notes", folder_status: "unlinked", working_directory: null, launch_revision: null, folder_id: "ops", space_id: "general", position: 0 }]);
});

test("workspace task moves use compare-and-set and expose a reverse undo operation", async () => {
  const requests = [];
  const { api } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      return Promise.resolve({ ok: true, json: async () => ({ context: { list_key: "list:next", list_name: "Next" } }) });
    },
  });
  const task = activeTask({ project_key: "legacy", list_key: "list:old", list_name: "Old" });
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [], workspace: { lists: [] } } });
  const undo = await api.moveTaskToList(task, "list:next");
  assert.deepEqual(JSON.parse(requests[0].options.body), { list_key: "list:next", expected_list_key: "list:old" });
  await undo();
  assert.deepEqual(JSON.parse(requests[1].options.body), { list_key: "list:old", expected_list_key: "list:next" });
});

test("task drag handle supplies a browser-recognized payload", () => {
  const { api } = loadApp();
  const task = activeTask();
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  const row = api.renderTaskRow(task);
  const handle = findByClass(row, "drag-handle");
  const payloads = {};
  handle.dispatch("dragstart", {
    dataTransfer: {
      setData: (type, value) => { payloads[type] = value; },
      effectAllowed: "",
    },
  });
  assert.equal(payloads["application/x-agent-board-task"], "task-1");
  assert.equal(payloads["text/plain"], "task-1");
});

test("header sorting supports every workspace data column with a stable task-id tie break", () => {
  const { api } = loadApp();
  api.setState({ sortBy: "priority", sortDirection: "asc" });
  assert.ok(api.taskComparator(activeTask({ task_id: "b", priority: "high" }), activeTask({ task_id: "a", priority: "high" })) > 0);
  assert.ok(api.taskComparator(activeTask({ priority: "urgent" }), activeTask({ priority: "low" })) < 0);
  api.setState({ sortBy: "name", sortDirection: "desc" });
  assert.ok(api.taskComparator(activeTask({ title: "Alpha" }), activeTask({ title: "Zulu" })) > 0);
});

test("saved view snapshots migrate closed to completed and retain table layout settings", () => {
  const { api } = loadApp();
  api.applySnapshot({ scope: "closed", sort: "agent", sortDirection: "desc", columns: ["agent", "name", "due", "updated", "priority", "terminal"] });
  const state = api.getState();
  assert.equal(state.queueMode, "all");
  assert.equal(state.statusFilter, "completed");
  const snapshot = api.snapshotView();
  assert.equal(snapshot.sortDirection, "desc");
  assert.deepEqual(snapshot.columns.slice(0, 2), ["agent", "name"]);
});

test("lifecycle status intersects Today and retains the selected List", () => {
  const { api, elementFor } = loadApp();
  const completedToday = activeTask({ task_id: "done", list_key: "list:yoga", work_status: "done", in_today: true });
  api.setState({
    latestBoard: { tasks: [completedToday], projects: [], folders: [], workspace: { spaces: [], folders: [], lists: [{ list_key: "list:yoga", name: "Yoga", space_id: "general", folder_id: null, position: 0 }] } },
    queueMode: "today",
    statusFilter: "active",
    selectedProject: "list:yoga",
  });
  elementFor("work-search").value = "";
  api.selectStatus("completed");
  assert.equal(api.getState().queueMode, "today");
  assert.equal(api.getState().selectedProject, "list:yoga");
  assert.equal(api.taskMatches(completedToday), true);
});

test("task-modal launch remains canonical even after selecting an older conversation", async () => {
  const requests = [];
  const { api, elementFor } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      if (path === "/api/session/old-session") return Promise.resolve({ ok: true, json: async () => ({ meta: { session_id: "old-session", title: "Old conversation", actions: {} }, turns: [] }) });
      return Promise.resolve({ ok: true, json: async () => ({ ok: true }) });
    },
  });
  const task = activeTask({
    session_id: "current-session",
    working_directory: "/linked/work",
    launch_revision: "rev-current",
    actions: { claude: { label: "Continue Claude in linked work", available: true } },
  });
  elementFor("task-dialog").dataset.taskId = task.task_id;
  elementFor("task-dialog").dataset.openVersion = "4";
  elementFor("task-dialog").open = true;
  api.setState({ readerMode: "task" });
  api.selectSession("old-session");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(api.getState().activeSid, "old-session");
  assert.equal(elementFor("thread-actions").hidden, true);
  api.updateTaskActions(task);
  await api.launchTask(task, "claude");
  assert.equal(requests[1].path, "/api/tasks/task-1/launch");
  assert.deepEqual(JSON.parse(requests[1].options.body), { provider: "claude", full_access: false, launch_revision: "rev-current" });
  assert.equal(elementFor("task-claude").textContent, "Continue Claude in linked work");
});

test("late task-history responses cannot overwrite a newer dialog", async () => {
  const pending = [];
  const { api, elementFor } = loadApp({
    fetchImpl: () => new Promise(resolve => pending.push(resolve)),
  });
  const dialog = elementFor("task-dialog");
  dialog.open = true;
  dialog.dataset.taskId = "task-old";
  dialog.dataset.openVersion = "1";
  api.loadTaskHistory(activeTask({ task_id: "task-old" }));
  dialog.dataset.taskId = "task-new";
  dialog.dataset.openVersion = "2";
  pending[0]({ ok: true, json: async () => ({ sessions: [{ session_id: "old-session", provider: "claude", cwd: "/old" }] }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elementFor("task-history-picker").hidden, true);
  assert.equal(elementFor("task-history-select").children.length, 0);
});

test("destination-unavailable task actions stay disabled with the server reason", () => {
  const { api, elementFor } = loadApp();
  api.updateTaskActions(activeTask({
    actions: { codex: { label: "Choose a working folder", available: false, reason: "Working folder is missing" } },
  }));
  assert.equal(elementFor("task-codex").disabled, true);
  assert.equal(elementFor("task-codex").textContent, "Choose a working folder");
  assert.equal(elementFor("task-codex-reason").textContent, "Working folder is missing");
});

test("workspace menu movement posts the atomic reorder contract for every node kind", async () => {
  const requests = [];
  const { api } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      return Promise.resolve({ ok: true, json: async () => ({ tasks: [] }) });
    },
  });
  for (const [kind, item, direction] of [
    ["space", { space_id: "space-a" }, -1],
    ["space", { space_id: "space-a" }, 1],
    ["folder", { folder_id: "folder-a" }, -1],
    ["folder", { folder_id: "folder-a" }, 1],
    ["list", { list_key: "list-a" }, -1],
    ["list", { list_key: "list-a" }, 1],
  ]) await api.workspacePosition(kind, item, direction);

  assert.deepEqual(
    requests.filter(({ options }) => options.method === "POST").map(({ path, options }) => [path, JSON.parse(options.body)]),
    [
      ["/api/workspace/reorder", { kind: "space", node_id: "space-a", direction: -1 }],
      ["/api/workspace/reorder", { kind: "space", node_id: "space-a", direction: 1 }],
      ["/api/workspace/reorder", { kind: "folder", node_id: "folder-a", direction: -1 }],
      ["/api/workspace/reorder", { kind: "folder", node_id: "folder-a", direction: 1 }],
      ["/api/workspace/reorder", { kind: "list", node_id: "list-a", direction: -1 }],
      ["/api/workspace/reorder", { kind: "list", node_id: "list-a", direction: 1 }],
    ],
  );
});

test("List parent moves serialize per List so a later drop cannot overtake an earlier one", async () => {
  const pending = [];
  const { api } = loadApp({
    fetchImpl: (path, options = {}) => new Promise(resolve => pending.push({ path, options, resolve })),
  });
  api.setState({ latestBoard: { workspace: { lists: [] }, tasks: [], projects: [], folders: [] } });
  const first = api.moveListToFolder("list-a", { space_id: "space-a", folder_id: "folder-a" });
  const second = api.moveListToSpaceRoot("list-a", { space_id: "space-b" });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pending.length, 1);
  assert.deepEqual(JSON.parse(pending[0].options.body), { space_id: "space-a", folder_id: "folder-a" });

  pending.shift().resolve({ ok: true, json: async () => ({}) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pending.length, 1, "the board refresh follows the first mutation before the second begins");
  pending.shift().resolve({ ok: true, json: async () => ({ tasks: [] }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pending.length, 1);
  assert.deepEqual(JSON.parse(pending[0].options.body), { space_id: "space-b", folder_id: null });
  pending.shift().resolve({ ok: true, json: async () => ({}) });
  await new Promise(resolve => setImmediate(resolve));
  pending.shift().resolve({ ok: true, json: async () => ({ tasks: [] }) });
  await Promise.all([first, second]);
});

test("List parent menu actions use the serialized parent-move helpers", async () => {
  const requests = [];
  const { api } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      return Promise.resolve({ ok: true, json: async () => ({ tasks: [] }) });
    },
  });
  const board = {
    tasks: [], projects: [], folders: [],
    workspace: {
      spaces: [{ space_id: "space-b", name: "Space B" }],
      folders: [{ folder_id: "folder-b", space_id: "space-b", name: "Folder B" }],
      lists: [],
    },
  };
  api.setState({ latestBoard: board });
  const list = { list_key: "list-a", name: "List A", folder_id: "folder-a" };
  const menu = api.workspaceMenu("list", list);
  const buttons = menu.children[1].children;
  buttons.find(button => button.textContent === "Move to Space B root").dispatch("click");
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(
    JSON.parse(requests.find(({ options }) => options.method === "PATCH").options.body),
    { space_id: "space-b", folder_id: null },
  );

  const folderRequests = [];
  const second = loadApp({
    fetchImpl: (path, options = {}) => {
      folderRequests.push({ path, options });
      return Promise.resolve({ ok: true, json: async () => ({ tasks: [] }) });
    },
  });
  second.api.setState({ latestBoard: board });
  const folderMenu = second.api.workspaceMenu("list", list);
  folderMenu.children[1].children.find(button => button.textContent === "Move to Folder B").dispatch("click");
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(
    JSON.parse(folderRequests.find(({ options }) => options.method === "PATCH").options.body),
    { space_id: "space-b", folder_id: "folder-b" },
  );
});

test("history reader launches a tracked older conversation through its current task", async () => {
  const requests = [];
  const { api, elementFor } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      if (path === "/api/session/old-session") return Promise.resolve({ ok: true, json: async () => ({
        meta: {
          session_id: "old-session",
          title: "Old conversation",
          actions: { claude: { label: "Resume Claude", available: true } },
          task_launch: {
            task_id: "task-current",
            session_id: "current-session",
            launch_revision: "current-revision",
            working_directory: "/linked/work",
            actions: { claude: { label: "Continue in Claude", available: true } },
          },
        }, turns: [] }) });
      return Promise.resolve({ ok: true, json: async () => ({ ok: true }) });
    },
  });
  api.setState({ readerMode: "history" });
  api.selectSession("old-session");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elementFor("thread-claude").textContent, "Continue in Claude (current task)");
  assert.match(elementFor("viewer-meta").textContent, /Next launch: \/linked\/work/);
  await api.launchActiveThread("claude");
  assert.equal(requests[1].path, "/api/tasks/task-current/launch");
  assert.deepEqual(JSON.parse(requests[1].options.body), { provider: "claude", full_access: false, launch_revision: "current-revision" });
});

test("history reader retains the direct-session route for an untracked conversation", async () => {
  const requests = [];
  const { api } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      if (path === "/api/session/legacy-session") return Promise.resolve({ ok: true, json: async () => ({
        meta: { session_id: "legacy-session", title: "Legacy", actions: { claude: { label: "Resume Claude", available: true } } }, turns: [] }) });
      return Promise.resolve({ ok: true, json: async () => ({ ok: true }) });
    },
  });
  api.setState({ readerMode: "history" });
  api.selectSession("legacy-session");
  await new Promise(resolve => setImmediate(resolve));
  await api.launchActiveThread("claude");
  assert.equal(requests[1].path, "/api/sessions/legacy-session/launch");
  assert.deepEqual(JSON.parse(requests[1].options.body), { provider: "claude", full_access: false });
});

test("a newer toast cancels the prior toast timeout", () => {
  const timers = [], cleared = [];
  const { api, elementFor } = loadApp({
    setTimeoutImpl: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeoutImpl: token => cleared.push(token),
  });
  api.toast("First");
  api.toast("Second");
  assert.deepEqual(cleared, [1]);
  timers[1].callback();
  assert.equal(elementFor("toast").className, "");
});

test("rendering restored view state keeps every toolbar control synchronized", () => {
  const { api, elementFor } = loadApp();
  api.setState({
    latestBoard: { tasks: [], projects: [], folders: [], workspace: { spaces: [], folders: [], lists: [] } },
    queueMode: "today",
    statusFilter: "completed",
    groupBy: "terminal",
    sortBy: "priority",
    filters: { provider: "codex", priority: "high", terminal: "idle", due: "today" },
  });
  api.renderBoard(api.getState().latestBoard);

  assert.equal(elementFor("group-by").value, "terminal");
  assert.equal(elementFor("sort-by").value, "priority");
  assert.equal(elementFor("filter-status").value, "completed");
  assert.equal(elementFor("filter-provider").value, "codex");
  assert.equal(elementFor("filter-priority").value, "high");
  assert.equal(elementFor("filter-terminal").value, "idle");
  assert.equal(elementFor("filter-due").value, "today");
});
