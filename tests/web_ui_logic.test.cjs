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
  constructor(tag = "div", documentFor = () => null) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.attributes = new Map();
    this.parentElement = null;
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.textContent = "";
    this.className = "";
    this.style = {};
    this._documentFor = documentFor;
    this._id = "";
    Object.defineProperty(this, "id", {
      get: () => this._id,
      set: (value) => {
        this._id = String(value);
        const document = this._documentFor();
        if (document) document.registerId(this._id, this);
      },
    });
    this.classList = {
      add: (name) => {
        if (!this.className.split(/\s+/).includes(name))
          this.className = (this.className + " " + name).trim();
      },
      contains: (name) => this.className.split(/\s+/).includes(name),
      toggle: (name, enabled) => {
        if (enabled) this.classList.add(name);
        else this.className = this.className.split(/\s+/).filter((item) => item && item !== name).join(" ");
      },
    };
  }

  append(...children) {
    children.forEach((child) => this.appendChild(child));
  }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }

  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentElement = null; });
    this.children = [];
    this.append(...children);
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  dispatch(type, event = {}) {
    if (!event.target) event.target = this;
    if (!event.preventDefault) event.preventDefault = () => { event.defaultPrevented = true; };
    if (!event.stopPropagation) event.stopPropagation = () => { event.cancelBubble = true; };
    event.currentTarget = this;
    for (const listener of this.listeners[type] || []) listener.call(this, event);
    if (!event.cancelBubble && this.parentElement) this.parentElement.dispatch(type, event);
  }

  click() { this.dispatch("click"); }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "id") this.id = value;
  }
  getAttribute(name) { return this.attributes.get(name) || null; }
  removeAttribute(name) { this.attributes.delete(name); }
  getBoundingClientRect() { return { top: 0, height: 20, width: 0 }; }
  closest(selector) {
    let node = this;
    while (node) {
      if (matchesSelector(node, selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
  focus() { this._documentFor().activeElement = this; }
  select() {}
  showModal() {}
  close() {}
}

function matchesSelector(node, selector) {
  const open = selector.endsWith("[open]");
  const className = selector.replace(/^\./, "").replace("[open]", "");
  if (selector.startsWith("."))
    return node.classList.contains(className) && (!open || Boolean(node.open));
  return node.tagName.toLowerCase() === selector.toLowerCase();
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

function fixedDate(instant) {
  return class FixedDate extends Date {
    constructor(...args) {
      super(...(args.length ? args : [instant]));
    }

    static now() {
      return instant;
    }
  };
}

function loadApp({ DateImpl = Date, fetchImpl, initialize = false, setTimeoutImpl, clearTimeoutImpl, storage } = {}) {
  const source = fs.readFileSync(APP_PATH, "utf8");
  const markerIndex = source.lastIndexOf(INITIALIZATION_MARKER);
  assert.notEqual(markerIndex, -1, "app.js initialization marker must remain present");

  const elements = new Map();
  const allElements = new Set();
  let document;
  const createElement = (tag) => {
    const element = new FakeElement(tag, () => document);
    allElements.add(element);
    return element;
  };
  const elementFor = (id) => {
    if (!elements.has(id)) {
      const element = createElement();
      element.id = id;
      elements.set(id, element);
    }
    return elements.get(id);
  };
  document = {
    activeElement: null,
    listeners: {},
    registerId: (id, element) => elements.set(id, element),
    createElement,
    getElementById: elementFor,
    querySelector: (selector) => selector === ".thread-search-label" ? elementFor("thread-search-label") : [...allElements].find((element) => matchesSelector(element, selector)) || null,
    querySelectorAll: (selector) => [...allElements].filter((element) => matchesSelector(element, selector)),
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); },
    dispatch(type, event = {}) {
      if (!event.preventDefault) event.preventDefault = () => { event.defaultPrevented = true; };
      if (!event.stopPropagation) event.stopPropagation = () => { event.cancelBubble = true; };
      for (const listener of this.listeners[type] || []) listener.call(this, event);
    },
    documentElement: { style: { setProperty() {} } },
  };
  const windowListeners = {};
  const window = {
    addEventListener(type, listener) { (windowListeners[type] ||= []).push(listener); },
    removeEventListener(type, listener) {
      windowListeners[type] = (windowListeners[type] || []).filter((item) => item !== listener);
    },
    dispatch(type, event = {}) {
      for (const listener of [...(windowListeners[type] || [])]) listener.call(window, event);
    },
    listeners: windowListeners,
    crypto: { randomUUID: () => "test-client" },
  };
  const context = {
    Date: DateImpl,
    AbortController,
    Promise,
    console,
    document,
    fetch: fetchImpl || (() => new Promise(() => {})),
    localStorage: storage || { getItem: () => null, setItem() {} },
    setTimeout: setTimeoutImpl || (() => 0),
    clearTimeout: clearTimeoutImpl || (() => {}),
    window,
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
  continueOrFocusTask,
  loadTaskHistory,
  updateTaskActions,
  updateFreshTaskActions,
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
  taskPresence,
  startFreshTask,
  beginColumnResize,
  beginSidebarResize,
  inlineRename,
  prioritySelect,
  renderSidebar,
  restoreViewSettings,
  storeViewSettings,
  getState: function () {
    return { latestBoard: latestBoard, queueMode: queueMode, statusFilter: statusFilter, selectedProject: selectedProject, groupBy: groupBy, sortBy: sortBy, sortDirection: sortDirection, columns: columnOrder.slice(), widths: Object.assign({}, columnWidths), collapsedGroups: Object.assign({}, collapsedGroups), sidebarWidth: sidebarWidth, sidebarManualOrder: sidebarManualOrder, activePointerGesture: activePointerGesture, filters: filters, activeSid: activeSid };
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
    if (Object.prototype.hasOwnProperty.call(next, "sidebarWidth")) sidebarWidth = next.sidebarWidth;
    if (Object.prototype.hasOwnProperty.call(next, "sidebarManualOrder")) sidebarManualOrder = next.sidebarManualOrder;
    if (Object.prototype.hasOwnProperty.call(next, "activePointerGesture")) activePointerGesture = next.activePointerGesture;
    if (Object.prototype.hasOwnProperty.call(next, "readerMode")) readerMode = next.readerMode;
  }
};
})();`;
  const prefix = initialize ? source.slice(0, source.lastIndexOf("})();")) : source.slice(0, markerIndex);
  vm.runInNewContext(prefix + testExports, context, {
    filename: APP_PATH,
  });
  return { api: context.testApi, elementFor, document, window };
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

function findAllByClass(node, className, found = []) {
  if (node.classList && node.classList.contains(className)) found.push(node);
  for (const child of node.children || []) findAllByClass(child, className, found);
  return found;
}

function findListRow(root, name) {
  return findAllByClass(root, "list-row").find((row) =>
    findAllByClass(row, "project-name").some((label) => label.textContent === name),
  );
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
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

test("a poll response arriving during a pointer gesture does not repaint", async () => {
  let resolve;
  const { api } = loadApp({ fetchImpl: () => new Promise(next => { resolve = next; }) });
  const original = activeTask({ title: "editing" });
  api.setState({ latestBoard: { tasks: [original], projects: [], folders: [] } });
  const poll = api.fetchBoard();
  api.setState({ activePointerGesture: true });
  resolve({ ok: true, json: async () => ({ tasks: [activeTask({ title: "stale" })], projects: [], folders: [] }) });
  await poll;
  assert.equal(api.getState().latestBoard.tasks[0].title, "editing");
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

test("grid exposes direct status, due-date, and provider actions without a row menu", () => {
  const { api } = loadApp();
  const task = activeTask({
    terminal_presence: "closed",
    actions: {
      claude: { label: "Resume Claude", available: true },
      codex: { label: "Continue in CodeX", available: false, reason: "Transcript unavailable" },
    },
    start_actions: {
      claude: { label: "Start fresh Claude", available: true },
      codex: { label: "Start fresh CodeX", available: true },
    },
  });
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  const row = api.renderTaskRow(task);
  assert.ok(findByClass(row, "task-status"), "status is directly editable in the grid");
  assert.ok(findByClass(row, "due-input"), "due date is directly editable in the grid");
  assert.equal(findByClass(row, "row-menu"), null, "row menu clutter is removed");
  const actions = findAllByClass(row, "grid-launch");
  assert.deepEqual(actions.map((button) => button.textContent), ["Restart Claude", "Start CodeX"]);
  assert.match(actions[0].title, /new Terminal launch/i);
});

test("Continue focuses its verified open same-provider terminal before launching", async () => {
  const requests = [];
  const { api } = loadApp({
    fetchImpl: (path, options = {}) => {
      requests.push({ path, options });
      return Promise.resolve({ ok: true, json: async () => ({ focused: true }) });
    },
  });
  const task = activeTask({
    terminal_presence: "open",
    session_provider: "codex",
    actions: { codex: { label: "Continue in CodeX", available: true } },
  });
  await api.continueOrFocusTask(task, "codex");
  assert.deepEqual(requests.map((request) => request.path), ["/api/tasks/task-1/focus"]);
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

test("saved view widths and collapsed groups round trip with clamps", () => {
  const { api } = loadApp();
  api.applySnapshot({
    scope: "all", columns: ["name", "due", "updated", "priority", "terminal", "agent"],
    columnWidths: { name: 12, due: 9999 }, collapsedGroups: { "group-all-priority-urgent": true },
  });
  const state = api.getState();
  assert.equal(state.widths.name, 72);
  assert.equal(state.widths.due, 600);
  assert.equal(state.collapsedGroups["group-all-priority-urgent"], true);
  api.applySnapshot({ scope: "all", columns: ["name", "name", "updated", "priority", "terminal", "agent"] });
  assert.deepEqual(JSON.parse(JSON.stringify(api.getState().columns)), ["name", "status", "due", "updated", "priority", "terminal", "agent"]);
  assert.deepEqual(JSON.parse(JSON.stringify(api.getState().widths)), {});
});

test("sidebar size and manual order persist globally across views", () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const { api } = loadApp({ storage });
  api.setState({ queueMode: "open", sidebarWidth: 420, sidebarManualOrder: true });
  api.storeViewSettings();
  api.setState({ queueMode: "today", sidebarWidth: 250, sidebarManualOrder: false });
  api.restoreViewSettings();
  assert.equal(api.getState().sidebarWidth, 420);
  assert.equal(api.getState().sidebarManualOrder, true);
});

test("last update filters honor 24h, 7d, and 30d boundaries", () => {
  const now = 1_789_000_000;
  const { api, elementFor } = loadApp({ DateImpl: fixedDate(now * 1000) });
  elementFor("work-search").value = "";
  const base = { provider: "any", priority: "any", terminal: "any", due: "any", presence: "any" };
  api.setState({ queueMode: "all", filters: { ...base, lastUpdate: "24h" } });
  assert.equal(api.taskMatches(activeTask({ last_activity_at: now - 86399 })), true);
  assert.equal(api.taskMatches(activeTask({ last_activity_at: now - 86401 })), false);
  api.setState({ filters: { ...base, lastUpdate: "7d" } });
  assert.equal(api.taskMatches(activeTask({ last_activity_at: now - 604799 })), true);
  api.setState({ filters: { ...base, lastUpdate: "30d" } });
  assert.equal(api.taskMatches(activeTask({ last_activity_at: now - 2592001 })), false);
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

test("Today restores its heading and attention note", () => {
  const { api, elementFor } = loadApp();
  api.setState({
    latestBoard: { tasks: [], projects: [], folders: [], workspace: { spaces: [], folders: [], lists: [] } },
    queueMode: "today",
  });
  api.renderBoard(api.getState().latestBoard);
  assert.equal(elementFor("work-heading").textContent, "Today");
  assert.equal(elementFor("today-note").hidden, false);
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

test("Open terminals is presence-only while unknown remains available in All threads", () => {
  const { api, elementFor } = loadApp();
  const openCompleted = activeTask({ work_status: "done", terminal_presence: "open" });
  const unknown = activeTask({ task_id: "unknown", terminal_presence: "unknown" });
  api.setState({ queueMode: "open", statusFilter: "active", filters: { provider: "any", priority: "any", terminal: "any", due: "any", presence: "any", lastUpdate: "any" } });
  elementFor("work-search").value = "";
  assert.equal(api.taskMatches(openCompleted), true, "presence, not work completion, defines Open terminals");
  assert.equal(api.taskMatches(unknown), false);
  api.setState({ queueMode: "all", statusFilter: "all", filters: { provider: "any", priority: "any", terminal: "any", due: "any", presence: "unknown", lastUpdate: "any" } });
  assert.equal(api.taskMatches(unknown), true);
  assert.equal(api.taskPresence(activeTask({ terminal_open: true })), "open");
  assert.equal(api.taskPresence(activeTask({})), "unknown");
});

test("latest update descending puts the newest numeric timestamp first", () => {
  const { api } = loadApp();
  api.setState({ sortBy: "updated", sortDirection: "desc" });
  assert.ok(api.taskComparator(activeTask({ task_id: "new", last_activity_at: 200 }), activeTask({ task_id: "old", last_activity_at: 100 })) < 0);
  api.setState({ sortDirection: "asc" });
  assert.ok(api.taskComparator(activeTask({ task_id: "new", last_activity_at: 200 }), activeTask({ task_id: "old", last_activity_at: 100 })) > 0);
});

test("Start fresh uses the isolated start endpoint and never the continuation route", async () => {
  const requests = [];
  const { api } = loadApp({ fetchImpl: (path, options = {}) => {
    requests.push({ path, options });
    return Promise.resolve({ ok: true, json: async () => ({}) });
  } });
  const task = activeTask({ launch_revision: "rev-new", start_actions: { codex: { label: "Start fresh in CodeX", available: true } } });
  await api.startFreshTask(task, "codex");
  assert.equal(requests[0].path, "/api/tasks/task-1/start");
  assert.deepEqual(JSON.parse(requests[0].options.body), { provider: "codex", full_access: false, launch_revision: "rev-new" });
});

test("pointercancel releases both resize gestures so board polling resumes", async () => {
  const requests = [];
  const board = { tasks: [activeTask({ terminal_presence: "open" })], projects: [], folders: [] };
  const { api, document, window } = loadApp({
    fetchImpl: (requestPath, options = {}) => {
      requests.push({ requestPath, options });
      return Promise.resolve({ ok: true, json: async () => board });
    },
  });
  api.setState({ latestBoard: board, queueMode: "all" });

  const header = document.createElement("th");
  const resize = document.createElement("button");
  resize.id = "column-resize-all-priority-normal-name";
  header.appendChild(resize);
  api.beginColumnResize({ currentTarget: resize, clientX: 10 }, "name");
  assert.equal(api.getState().activePointerGesture, true);
  assert.equal(window.listeners.pointercancel.length, 1);
  window.dispatch("pointercancel");
  assert.equal(api.getState().activePointerGesture, false);
  assert.equal(window.listeners.pointermove.length, 0);
  assert.equal(window.listeners.pointerup.length, 0);
  assert.equal(window.listeners.pointercancel.length, 0);

  api.beginSidebarResize({ clientX: 10 });
  assert.equal(api.getState().activePointerGesture, true);
  window.dispatch("pointercancel");
  assert.equal(api.getState().activePointerGesture, false);
  assert.equal(window.listeners.pointermove.length, 0);
  assert.equal(window.listeners.pointerup.length, 0);
  assert.equal(window.listeners.pointercancel.length, 0);

  await api.fetchBoard();
  assert.equal(requests.filter(({ requestPath }) => requestPath === "/api/board").length, 1);
});

test("a deferred board response preserves an inline rename draft opened in flight", async () => {
  let resolve;
  const original = activeTask({ title: "Original", terminal_presence: "open" });
  const { api, document } = loadApp({
    fetchImpl: () => new Promise((next) => { resolve = next; }),
  });
  api.setState({ latestBoard: { tasks: [original], projects: [], folders: [] }, queueMode: "all" });

  const poll = api.fetchBoard();
  const row = api.renderTaskRow(original);
  findByClass(row, "rename-pencil").dispatch("click");
  const input = document.querySelector(".inline-rename");
  input.value = "Unsaved draft";
  resolve({ ok: true, json: async () => ({ tasks: [activeTask({ title: "Stale" })], projects: [], folders: [] }) });
  await poll;

  assert.equal(document.querySelector(".inline-rename"), input);
  assert.equal(input.value, "Unsaved draft");
  assert.equal(api.getState().latestBoard.tasks[0].title, "Original", "the response must not replace board data during the edit");
});

test("List-row drops stop bubbling and keep sibling ordering separate from parent moves", async () => {
  const requests = [];
  const board = {
    tasks: [], projects: [], folders: [],
    workspace: {
      spaces: [{ space_id: "space-a", name: "Space A", position: 0 }],
      folders: [
        { folder_id: "folder-a", space_id: "space-a", name: "Folder A", position: 0 },
        { folder_id: "folder-b", space_id: "space-a", name: "Folder B", position: 1 },
      ],
      lists: [
        { list_key: "source", space_id: "space-a", folder_id: "folder-a", name: "Source", position: 0 },
        { list_key: "sibling", space_id: "space-a", folder_id: "folder-a", name: "Sibling", position: 1 },
        { list_key: "cross-parent", space_id: "space-a", folder_id: "folder-b", name: "Cross parent", position: 0 },
      ],
    },
  };
  const { api, elementFor } = loadApp({
    fetchImpl: (requestPath, options = {}) => {
      requests.push({ requestPath, options });
      return Promise.resolve({ ok: true, json: async () => requestPath === "/api/board" ? board : { tasks: [] } });
    },
  });
  api.setState({ latestBoard: board, queueMode: "all" });
  api.renderBoard(board);

  async function dropOn(name, clientY) {
    const root = elementFor("folder-list");
    const source = findListRow(root, "Source");
    const target = findListRow(root, name);
    target.getBoundingClientRect = () => ({ top: 10, height: 20, width: 0 });
    source.children[0].dispatch("dragstart", { dataTransfer: { setData() {} } });
    target.dispatch("drop", { clientY });
    await flush();
    await flush();
  }

  await dropOn("Sibling", 11);
  await dropOn("Sibling", 29);
  await dropOn("Cross parent", 11);
  await dropOn("Source", 11);

  const mutations = requests
    .filter(({ options }) => options.method === "POST" || options.method === "PATCH")
    .map(({ requestPath, options }) => [requestPath, JSON.parse(options.body)]);
  assert.deepEqual(mutations, [
    ["/api/workspace/reorder", { kind: "list", node_id: "source", target_id: "sibling", placement: "before" }],
    ["/api/workspace/reorder", { kind: "list", node_id: "source", target_id: "sibling", placement: "after" }],
    ["/api/workspace/reorder", { kind: "list", node_id: "source", target_id: "cross-parent", placement: "before" }],
  ]);
});

test("priority menus support keyboard selection, Escape, and click-away dismissal", async () => {
  const requests = [];
  const task = activeTask({ priority: "normal" });
  const { api, document } = loadApp({
    initialize: true,
    fetchImpl: (requestPath, options = {}) => {
      requests.push({ requestPath, options });
      if (requestPath === "/api/meta") return Promise.resolve({ ok: true, json: async () => ({ csrf_token: "token" }) });
      if (requestPath === "/api/board") return Promise.resolve({ ok: true, json: async () => ({ tasks: [task], projects: [], folders: [] }) });
      return Promise.resolve({ ok: true, json: async () => ({ task: { ...task, priority: "high" } }) });
    },
  });
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] }, queueMode: "all" });
  const menu = api.prioritySelect(task);
  const summary = menu.children[0];
  const options = menu.children[1].children;
  summary.dispatch("keydown", { key: "ArrowDown" });
  assert.equal(menu.open, true);
  assert.equal(document.activeElement, options[2]);
  options[2].dispatch("keydown", { key: "ArrowUp" });
  assert.equal(document.activeElement, options[1]);
  options[1].dispatch("keydown", { key: "Enter" });
  await flush();
  assert.equal(menu.open, false);
  const priorityPayload = JSON.parse(
    requests.find(({ options }) => options.method === "PATCH").options.body,
  );
  assert.equal(priorityPayload.priority, "high");
  assert.equal(priorityPayload._edit_client, "test-client");
  assert.equal(priorityPayload._edit_revision, 1);

  const escapeMenu = api.prioritySelect(task);
  escapeMenu.open = true;
  escapeMenu.children[1].children[0].dispatch("keydown", { key: "Escape" });
  assert.equal(escapeMenu.open, false);
  assert.equal(document.activeElement, escapeMenu.children[0]);

  const outsideMenu = api.prioritySelect(task);
  outsideMenu.open = true;
  document.dispatch("click", { target: document.createElement("div") });
  assert.equal(outsideMenu.open, false);
});

test("group collapse persists across reloads and restores focus to its toggle", () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const board = { tasks: [activeTask({ priority: "urgent", terminal_presence: "open" })], projects: [], folders: [] };
  const first = loadApp({ storage });
  first.api.setState({ latestBoard: board, queueMode: "all", statusFilter: "all" });
  first.api.renderBoard(board);
  const toggleId = "group-toggle-group-all-priority-urgent";
  first.elementFor(toggleId).dispatch("click");
  assert.equal(first.api.getState().collapsedGroups["group-all-priority-urgent"], true);
  assert.equal(first.document.activeElement.id, toggleId);
  assert.equal(first.elementFor("group-all-priority-urgent").hidden, true);

  const restored = loadApp({ storage });
  restored.api.setState({ latestBoard: board, queueMode: "all", statusFilter: "all" });
  restored.api.restoreViewSettings();
  restored.api.renderBoard(board);
  assert.equal(restored.api.getState().collapsedGroups["group-all-priority-urgent"], true);
  assert.equal(restored.elementFor("group-all-priority-urgent").hidden, true);
});

test("column resize controls are unique per priority group and retain focus after cancel", () => {
  const board = {
    tasks: [
      activeTask({ task_id: "urgent", priority: "urgent", terminal_presence: "open" }),
      activeTask({ task_id: "normal", priority: "normal", terminal_presence: "open" }),
    ],
    projects: [], folders: [],
  };
  const { api, elementFor, document, window } = loadApp();
  api.setState({ latestBoard: board, queueMode: "all", statusFilter: "all" });
  api.renderBoard(board);
  const resizeControls = findAllByClass(elementFor("task-groups"), "column-resize");
  assert.equal(new Set(resizeControls.map((control) => control.id)).size, resizeControls.length);
  const normalNameResize = resizeControls.find((control) => control.id.includes("normal") && control.id.endsWith("-name"));
  assert.ok(normalNameResize, "the normal-priority name resize control is addressable");
  normalNameResize.dispatch("pointerdown", { clientX: 10 });
  window.dispatch("pointercancel");
  assert.equal(document.activeElement.id, normalNameResize.id);
});

test("column resize handles expose a drag affordance and filter summary marks active filters", () => {
  const task = activeTask({ priority: "urgent", terminal_presence: "open" });
  const { api, elementFor } = loadApp();
  const board = { tasks: [task], projects: [], folders: [] };
  api.setState({ latestBoard: board, queueMode: "all", statusFilter: "all" });
  api.renderBoard(board);
  const resize = findAllByClass(elementFor("task-groups"), "column-resize")[0];
  assert.equal(resize.textContent, "");
  assert.match(resize.title, /Drag to resize/);
  assert.match(resize.getAttribute("aria-label"), /Resize/);
  assert.equal(elementFor("filter-summary").getAttribute("data-active"), "false");
  api.setState({ filters: { provider: "any", priority: "urgent", terminal: "any", due: "any", presence: "any", lastUpdate: "any" } });
  api.renderBoard(board);
  assert.equal(elementFor("filter-summary").getAttribute("data-active"), "true");
});

test("a missing current-task transcript keeps fresh starts available without duplicate errors", async () => {
  const task = activeTask({
    session_id: "current-session",
    actions: {
      claude: { label: "Continue Claude", available: false, reason: "Transcript is unavailable" },
      codex: { label: "Continue CodeX", available: false, reason: "Transcript is unavailable" },
    },
    start_actions: {
      claude: { label: "Start fresh Claude", available: true },
      codex: { label: "Start fresh CodeX", available: true },
    },
  });
  const { api, elementFor, document } = loadApp({
    fetchImpl: (requestPath) => {
      assert.equal(requestPath, "/api/session/current-session");
      return Promise.resolve({ ok: true, json: async () => ({
        meta: {
          session_id: "current-session", title: "Current task", folder: "/repo",
          cwd: "/repo", provider_name: "Claude", msg_count: 0,
        },
        turns: [],
        transcript_error: "The original transcript is unavailable.",
      }) });
    },
  });
  elementFor("task-dialog").dataset.taskId = task.task_id;
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] }, readerMode: "task" });
  ["claude", "codex"].forEach((provider) => { elementFor("task-fresh-" + provider).disabled = true; });
  api.updateFreshTaskActions(task);
  api.selectSession("current-session");
  await flush();

  assert.equal(elementFor("thread-error").hidden, false);
  assert.match(elementFor("thread-error").textContent, /original transcript/i);
  assert.equal(elementFor("thread-search").hidden, true);
  assert.equal(document.querySelector(".thread-search-label").hidden, true);
  assert.equal(elementFor("transcript").children.length, 0, "the generic empty-transcript card is suppressed");
  ["claude", "codex"].forEach((provider) => {
    assert.equal(elementFor("task-" + provider + "-reason").hidden, true);
    assert.match(elementFor("task-" + provider).title, /transcript/i);
    assert.equal(elementFor("task-fresh-" + provider).disabled, false);
    assert.equal(elementFor("task-fresh-" + provider).textContent, task.start_actions[provider].label);
  });
});

test("stored view settings reset by scope and invalid data falls back to defaults", () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  values.set("agent-board.view-settings.v2", JSON.stringify({
    sidebarWidth: 420,
    sidebarManualOrder: true,
    views: {
      open: {
        sort: "priority", sortDirection: "desc",
        columns: ["agent", "terminal", "priority", "updated", "due", "name"],
        widths: { name: 310 }, collapsedGroups: { "group-open-priority-urgent": true },
      },
    },
  }));
  const { api } = loadApp({ storage });
  api.setState({ queueMode: "open" });
  api.restoreViewSettings();
  assert.equal(api.getState().sortBy, "priority");
  assert.equal(api.getState().widths.name, 310);

  api.setState({ queueMode: "today" });
  api.restoreViewSettings();
  assert.equal(api.getState().sortBy, "manual");
  assert.deepEqual(JSON.parse(JSON.stringify(api.getState().columns)), ["name", "status", "due", "updated", "priority", "terminal", "agent"]);
  assert.deepEqual(JSON.parse(JSON.stringify(api.getState().widths)), {});
  assert.deepEqual(JSON.parse(JSON.stringify(api.getState().collapsedGroups)), {});
  assert.equal(api.getState().sidebarWidth, 420, "sidebar preference remains global");
  assert.equal(api.getState().sidebarManualOrder, true);

  values.set("agent-board.view-settings.v2", "not json");
  api.restoreViewSettings();
  assert.equal(api.getState().sortBy, "manual");
  assert.equal(api.getState().sidebarWidth, 250);
  assert.equal(api.getState().sidebarManualOrder, false);
});

test("Unknown presence shows the All-threads escape note", () => {
  const { api, elementFor } = loadApp();
  api.setState({
    queueMode: "open",
    latestBoard: { tasks: [activeTask({ terminal_presence: "unknown" })], projects: [], folders: [] },
  });
  api.renderBoard(api.getState().latestBoard);
  assert.equal(elementFor("unknown-count").textContent, "(1 unknown)");
  assert.equal(elementFor("presence-note").hidden, false);
  assert.match(elementFor("presence-note").textContent, /Find them in All threads/);
});
