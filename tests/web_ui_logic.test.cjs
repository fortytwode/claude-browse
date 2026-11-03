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
  blur() {
    this.blurred = true;
    if (this._documentFor().activeElement === this) this._documentFor().activeElement = null;
  }
  select() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}

function matchesSelector(node, selector) {
  if (selector.startsWith("#")) return node.id === selector.slice(1);
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

function loadApp({ DateImpl = Date, fetchImpl, initialize = false, setTimeoutImpl, clearTimeoutImpl, storage, desktop = false } = {}) {
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
    documentElement: { style: { setProperty() {} }, classList: { add() {} } },
  };
  const windowListeners = {};
  const window = {
    location: { search: desktop ? "?desktop=1" : "" },
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
    URLSearchParams,
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
  renderDesktopThread,
  activityAge,
  reminderDue,
  localDateTimeValue,
  openSnooze,
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
  closeFilterPopover,
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

test("Today keeps server-selected updated items without dates, but an explicit due filter excludes them", () => {
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

test("Work search includes tasks matched by the server's full transcript index", () => {
  const { api, elementFor } = loadApp();
  const task = activeTask({ task_id: "historical-match", title: "Unrelated current title", summary: "Recent text" });
  elementFor("work-search").value = "launcher";
  api.setState({
    latestBoard: {
      tasks: [task], projects: [], folders: [],
      search_query: "launcher", search_task_ids: ["historical-match"],
    },
    queueMode: "all",
    statusFilter: "all",
    filters: { provider: "any", priority: "any", terminal: "any", due: "any", presence: "any", lastUpdate: "any" },
  });
  api.renderBoard(api.getState().latestBoard);

  assert.equal(api.taskMatches(task), true);
  elementFor("work-search").value = "different";
  assert.equal(api.taskMatches(task), false, "stale server results must not match a newer query");
});

test("fetchBoard sends the Work query to the full-history search endpoint", async () => {
  const requests = [];
  const { api, elementFor } = loadApp({
    fetchImpl: (requestPath) => {
      requests.push(requestPath);
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ tasks: [], projects: [], folders: [] }) });
    },
  });
  elementFor("work-search").value = "launch plan";

  await api.fetchBoard(true);

  assert.equal(requests[0], "/api/board?q=launch+plan");
});

test("shared matches remain visible when the Mac relay is offline", async () => {
  const { api, elementFor } = loadApp({
    fetchImpl: (requestPath) => {
      if (requestPath.startsWith("/api/board")) return Promise.reject(new Error("Mac offline"));
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ matches: [{ host: "other-mac", title: "Anna review", text_snippet: "One-on-one notes" }] }),
      });
    },
  });
  elementFor("work-search").value = "review Anna";

  await api.fetchBoard(true);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(elementFor("shared-work-results").hidden, false);
  assert.equal(elementFor("shared-work-results").children.length, 3);
  assert.equal(elementFor("board-error").hidden, false);
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

test("task rows label their List rather than a nested working directory", () => {
  const { api } = loadApp();
  const task = activeTask({
    project_name: "Team Operations",
    working_directory: "/Users/shamanth/team-operations/client/MaxRewards/",
  });
  api.setState({
    latestBoard: {
      tasks: [task],
      projects: [{ project_key: "project-1", name: "Team Operations" }],
      folders: [],
    },
  });

  const row = api.renderTaskRow(task);

  assert.equal(findByClass(row, "task-breadcrumb").textContent, "List · Team Operations");
});

test("task rows identify temporary worktrees without calling them repos", () => {
  const { api } = loadApp();
  const task = activeTask({
    list_name: "littlegrove-four-region-review.CQnQfi",
    working_directory: "/private/tmp/littlegrove-four-region-review.CQnQfi",
    project_key: "path:/private/tmp/littlegrove-four-region-review.CQnQfi",
  });
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  assert.equal(findByClass(api.renderTaskRow(task), "task-breadcrumb").textContent, "Temporary folder · littlegrove-four-region-review.CQnQfi");
  const nestedWorktree = activeTask({
    working_directory: "/Users/shamanth/repos/team-operations/.worktrees/security/meta-token-boundary",
  });
  assert.equal(findByClass(api.renderTaskRow(nestedWorktree), "task-breadcrumb").textContent, "Worktree · team-operations");
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
  const automatic = api.renderTaskRow(activeTask({ due_date: "2026-09-20", due_date_defaulted: true }));
  assert.match(findByClass(automatic, "due-input").title, /last update/i);
  assert.equal(findByClass(row, "row-menu"), null, "row menu clutter is removed");
  const actions = findAllByClass(row, "grid-launch");
  assert.deepEqual(actions.map((button) => button.textContent), ["Restart Claude", "Start CodeX"]);
  assert.match(actions[0].title, /new Terminal launch/i);
});

test("Agent cells show the provider and exact reported model", () => {
  const { api } = loadApp();
  const task = activeTask({ session_provider: "claude", model: "claude-opus-4-8" });
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });

  const row = api.renderTaskRow(task);

  assert.equal(findByClass(row, "agent").textContent, "Claude · claude-opus-4-8");
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

test("last update filters honor all preset and custom date boundaries", () => {
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
  for (const [preset, days] of [["19d", 19], ["60d", 60], ["90d", 90], ["365d", 365]]) {
    api.setState({ filters: { ...base, lastUpdate: preset } });
    assert.equal(api.taskMatches(activeTask({ last_activity_at: now - days * 86400 + 1 })), true, preset);
    assert.equal(api.taskMatches(activeTask({ last_activity_at: now - days * 86400 - 1 })), false, preset);
  }
  const day = new Date(now * 1000).toLocaleDateString("en-CA");
  api.setState({ filters: { ...base, lastUpdate: "custom", lastUpdateFrom: day, lastUpdateTo: day } });
  assert.equal(api.taskMatches(activeTask({ last_activity_at: now })), true);
});

test("due date supports custom inclusive dates without losing no-date filter", () => {
  const { api, elementFor } = loadApp();
  elementFor("work-search").value = "";
  const base = { provider: "any", priority: "any", terminal: "any", presence: "any", lastUpdate: "any" };
  api.setState({ queueMode: "all", filters: { ...base, due: "custom", dueFrom: "2026-09-10", dueTo: "2026-09-20" } });
  assert.equal(api.taskMatches(activeTask({ due_date: "2026-09-10" })), true);
  assert.equal(api.taskMatches(activeTask({ due_date: "2026-09-20" })), true);
  assert.equal(api.taskMatches(activeTask({ due_date: "2026-09-21" })), false);
  assert.equal(api.taskMatches(activeTask({ due_date: null })), false);
  api.setState({ filters: { ...base, due: "custom" } });
  assert.equal(api.taskMatches(activeTask({ due_date: null })), true, "choosing Custom alone does not filter");
  api.setState({ filters: { ...base, due: "none" } });
  assert.equal(api.taskMatches(activeTask({ due_date: null })), true);
});

test("filter popover closes on outside click and stays open inside", () => {
  const { api, document, elementFor } = loadApp();
  const popover = elementFor("filter-popover");
  popover.className = "popover";
  popover.open = true;
  const inner = document.createElement("button");
  popover.appendChild(inner);
  api.closeFilterPopover({ target: inner });
  assert.equal(popover.open, true);
  api.closeFilterPopover({ target: document.createElement("div") });
  assert.equal(popover.open, false);
});

test("Sort and Filters popovers both dismiss on outside click and Escape", () => {
  const { api, document, elementFor } = loadApp({ initialize: true });
  const sort = elementFor("sort-popover"), filters = elementFor("filter-popover");
  sort.className = filters.className = "popover";
  sort.open = filters.open = true;
  const insideSort = document.createElement("button");
  sort.appendChild(insideSort);
  document.dispatch("click", { target: insideSort });
  assert.equal(sort.open, true, "clicking inside Sort keeps it open");
  assert.equal(filters.open, false, "the unrelated Filters popover closes");

  sort.open = filters.open = true;
  document.dispatch("click", { target: document.createElement("button") });
  assert.equal(sort.open, false);
  assert.equal(filters.open, false);

  sort.open = filters.open = true;
  document.dispatch("keydown", { key: "Escape" });
  assert.equal(sort.open, false);
  assert.equal(filters.open, false);
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

test("Today restores its heading and updated-today note", () => {
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


test("compact thread names open the existing terminal and preserve the original title", async () => {
  const requests = [];
  const { api } = loadApp({ fetchImpl: (path) => {
    requests.push(path);
    return Promise.resolve({ ok: true, json: async () => ({ focused: true }) });
  }});
  const task = activeTask({ title: "Original thread name", terminal_presence: "open", terminal_runtime_state: "working", session_provider: "codex" });
  const row = api.renderDesktopThread(task);
  assert.equal(row.children.length, 3);
  assert.equal(findByClass(row, "desktop-thread-name").textContent, "Original thread name");
  assert.equal(findByClass(row, "desktop-thread-status").textContent, "Working");
  await findByClass(row, "desktop-thread-name").dispatch("click");
  assert.deepEqual(requests, ["/api/tasks/task-1/focus"]);
});


test("compact list distinguishes unknown terminal presence from closed", () => {
  const { api } = loadApp();
  const row = api.renderDesktopThread(activeTask({ terminal_presence: "unknown" }));
  assert.equal(findByClass(row, "desktop-thread-status").textContent, "Unknown");
});

test("desktop activity age uses conversation activity and handles future and missing timestamps", () => {
  const now = Date.UTC(2026, 10, 3, 12, 0, 0);
  const { api } = loadApp({ desktop: true, DateImpl: fixedDate(now) });
  const nowSeconds = now / 1000;
  assert.equal(api.activityAge(activeTask({ last_active_at: nowSeconds - 30 })), "Updated just now");
  assert.equal(api.activityAge(activeTask({ last_active_at: nowSeconds - 59 * 60 })), "Updated 59 min ago");
  assert.equal(api.activityAge(activeTask({ last_active_at: nowSeconds - 3 * 3600 })), "Updated 3h ago");
  assert.equal(api.activityAge(activeTask({ last_active_at: nowSeconds - 2 * 86400 })), "Updated 2d ago");
  assert.equal(api.activityAge(activeTask({ last_active_at: nowSeconds + 60 })), "Updated just now");
  assert.equal(api.activityAge(activeTask({})), "Update time unknown");
});

test("desktop ordering puts due reminders first, starred next, and future snoozes last", () => {
  const now = Date.UTC(2026, 10, 3, 12) / 1000;
  const { api } = loadApp({ desktop: true, DateImpl: fixedDate(now * 1000) });
  api.setState({ sortBy: "updated", sortDirection: "desc" });
  const tasks = [
    activeTask({ task_id: "snoozed", snoozed_until: now + 3600 }),
    activeTask({ task_id: "plain" }),
    activeTask({ task_id: "starred", starred: true }),
    activeTask({ task_id: "due", snoozed_until: now - 60 }),
  ];
  assert.deepEqual(tasks.sort(api.taskComparator).map((task) => task.task_id), ["due", "starred", "plain", "snoozed"]);
  assert.equal(api.reminderDue(tasks[0]), true);
});

test("closed threads enter Open only when their snooze reminder is due, and done or archived reminders stay out", () => {
  const now = Date.UTC(2026, 10, 3, 12) / 1000;
  const { api } = loadApp({ desktop: true, DateImpl: fixedDate(now * 1000) });
  api.setState({ queueMode: "open", statusFilter: "active" });
  const closedFuture = activeTask({ terminal_presence: "closed", snoozed_until: now + 60 });
  const closedDue = activeTask({ terminal_presence: "closed", snoozed_until: now - 60 });
  const doneDue = activeTask({ terminal_presence: "closed", work_status: "done", snoozed_until: now - 60 });
  const archivedDue = activeTask({ terminal_presence: "closed", work_status: "archived", snoozed_until: now - 60 });
  assert.equal(api.taskMatches(closedFuture), false);
  assert.equal(api.taskMatches(closedDue), true);
  api.setState({ queueMode: "today" });
  assert.equal(api.taskMatches(closedDue), true, "a due reminder is shown in Today");
  assert.equal(api.taskMatches(doneDue), false);
  assert.equal(api.taskMatches(archivedDue), false);
  assert.equal(api.reminderDue(doneDue), false);
  assert.equal(api.reminderDue(archivedDue), false);
});

test("due reminder row says Reminder due today and terminal presence remains truthful", () => {
  const now = Date.UTC(2026, 10, 3, 12) / 1000;
  const { api } = loadApp({ desktop: true, DateImpl: fixedDate(now * 1000) });
  const row = api.renderDesktopThread(activeTask({ terminal_presence: "closed", terminal_runtime_state: "working", snoozed_until: now - 1 }));
  assert.equal(findByClass(row, "desktop-thread-status").textContent, "Closed");
  assert.equal(findByClass(row, "desktop-thread-reminder").textContent, "Reminder due");
  const openRow = api.renderDesktopThread(activeTask({ terminal_presence: "open", terminal_runtime_state: "working" }));
  assert.equal(findByClass(openRow, "desktop-thread-status").textContent, "Working");
  const unknownRow = api.renderDesktopThread(activeTask({ terminal_presence: "unknown", terminal_runtime_state: "working" }));
  assert.equal(findByClass(unknownRow, "desktop-thread-status").textContent, "Unknown");
});

test("desktop star action PATCHes the starred preference", async () => {
  const requests = [];
  const task = activeTask({ starred: false });
  const { api } = loadApp({ desktop: true, fetchImpl: (url, options) => {
    requests.push({ url, method: options.method, body: JSON.parse(options.body) });
    return Promise.resolve({ ok: true, json: async () => ({ task: { ...task, starred: requests.at(-1).body.starred } }) });
  }});
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  const row = api.renderDesktopThread(task);
  findByClass(row, "desktop-thread-star").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/tasks/task-1");
  assert.equal(requests[0].method, "PATCH");
  assert.equal(requests[0].body.starred, true);
  assert.equal(task.starred, true);
  findByClass(row, "desktop-thread-star").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 2);
  assert.equal(requests[1].body.starred, false);
  assert.equal(task.starred, false);
});

test("snooze rejects a nonfuture time and PATCHes a future reminder as a number", async () => {
  const now = Date.UTC(2026, 10, 3, 12, 0, 0);
  const requests = [];
  const task = activeTask();
  const { api, elementFor } = loadApp({ desktop: true, DateImpl: fixedDate(now), fetchImpl: (url, options) => {
    requests.push({ url, method: options.method, body: JSON.parse(options.body) });
    return Promise.resolve({ ok: true, json: async () => ({ task: { ...task, snoozed_until: requests.at(-1).body.snoozed_until } }) });
  }});
  const row = api.renderDesktopThread(task);
  findByClass(row, "desktop-thread-snooze").click();
  const input = elementFor("snooze-time");
  input.value = api.localDateTimeValue(new Date(now - 60000));
  findByClass(elementFor("snooze-dialog"), "snooze-save").click();
  assert.equal(requests.length, 0);
  assert.match(findByClass(elementFor("snooze-dialog"), "field-error").textContent, /future/);
  input.value = "2030-01-02T12:15";
  findByClass(elementFor("snooze-dialog"), "snooze-save").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, "PATCH");
  assert.equal(requests[0].body.snoozed_until, new Date(input.value).getTime() / 1000);
  assert.equal(typeof requests[0].body.snoozed_until, "number");
  assert.equal(task.snoozed_until, requests[0].body.snoozed_until);
  assert.equal(elementFor("snooze-dialog").open, false);
});

test("a completed older snooze save does not close or overwrite a newer snooze dialog", async () => {
  const now = Date.UTC(2026, 10, 3, 12, 0, 0);
  let resolveSave;
  const task = activeTask();
  const { api, elementFor } = loadApp({
    desktop: true,
    DateImpl: fixedDate(now),
    fetchImpl: () => new Promise((resolve) => { resolveSave = resolve; }),
  });
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  const row = api.renderDesktopThread(task);
  const snoozeButton = findByClass(row, "desktop-thread-snooze");
  snoozeButton.click();
  const dialog = elementFor("snooze-dialog");
  const input = elementFor("snooze-time");
  input.value = "2030-01-02T12:15";
  findByClass(dialog, "snooze-save").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof resolveSave, "function", "the first snooze PATCH is in flight");

  snoozeButton.click();
  const newerInputValue = "2031-04-05T09:30";
  elementFor("snooze-time").value = newerInputValue;
  resolveSave({
    ok: true,
    json: async () => ({ task: { ...task, snoozed_until: new Date("2030-01-02T12:15").getTime() / 1000 } }),
  });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(dialog.open, true);
  assert.equal(elementFor("snooze-time").value, newerInputValue);
});

test("snooze dialog X closes without saving its edited time", () => {
  const requests = [];
  const { api, elementFor } = loadApp({ desktop: true, fetchImpl: (url, options) => {
    requests.push({ url, options });
    return Promise.resolve({ ok: true, json: async () => ({ task: activeTask() }) });
  }});
  const row = api.renderDesktopThread(activeTask());
  findByClass(row, "desktop-thread-snooze").click();
  const dialog = elementFor("snooze-dialog");
  elementFor("snooze-time").value = "2030-01-02T12:15";
  findByClass(dialog, "snooze-close").click();
  assert.equal(dialog.open, false);
  assert.equal(requests.length, 0);
});

test("clearing a snooze PATCHes a null reminder", async () => {
  const now = Date.UTC(2026, 10, 3, 12, 0, 0) / 1000;
  const requests = [];
  const task = activeTask({ snoozed_until: now + 3600 });
  const { api, elementFor } = loadApp({ desktop: true, DateImpl: fixedDate(now * 1000), fetchImpl: (url, options) => {
    requests.push({ url, method: options.method, body: JSON.parse(options.body) });
    return Promise.resolve({ ok: true, json: async () => ({ task: { ...task, snoozed_until: null } }) });
  }});
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  const row = api.renderDesktopThread(task);
  findByClass(row, "desktop-thread-snooze").click();
  findByClass(elementFor("snooze-dialog"), "snooze-clear").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/tasks/task-1");
  assert.equal(requests[0].method, "PATCH");
  assert.equal(requests[0].body.snoozed_until, null);
  assert.equal(task.snoozed_until, null);
  assert.equal(elementFor("snooze-dialog").open, false);
});

test("canceling desktop rename preserves star and snooze preference controls", async () => {
  const { api } = loadApp({ desktop: true });
  const row = api.renderDesktopThread(activeTask({ starred: true, snoozed_until: 1900000000 }));
  const star = findByClass(row, "desktop-thread-star");
  const snooze = findByClass(row, "desktop-thread-snooze");
  assert.equal(star.textContent, "★");
  assert.equal(star.getAttribute("aria-pressed"), "true");
  await findByClass(row, "desktop-thread-rename").dispatch("click");
  const editor = findByClass(row, "inline-rename");
  editor.value = "Uncommitted title";
  await editor.dispatch("keydown", { key: "Escape", preventDefault() {} });
  assert.equal(findByClass(row, "desktop-thread-name").textContent, "Task one");
  assert.equal(findByClass(row, "desktop-thread-star"), star);
  assert.equal(findByClass(row, "desktop-thread-star").textContent, "★");
  assert.equal(findByClass(row, "desktop-thread-star").getAttribute("aria-pressed"), "true");
  assert.equal(findByClass(row, "desktop-thread-snooze"), snooze);
});

test("virtual terminal-window rows do not expose star or snooze preferences", () => {
  const { api } = loadApp({ desktop: true });
  const row = api.renderDesktopThread(activeTask({ terminal_window: { pid: 123, provider: "codex" } }));
  assert.equal(findByClass(row, "desktop-thread-star"), null);
  assert.equal(findByClass(row, "desktop-thread-snooze"), null);
});


for (const [source, target] of [["claude", "codex"], ["codex", "claude"]]) {
  test(`compact ${source} thread can continue in ${target}`, async () => {
    const requests = [];
    const { api } = loadApp({ fetchImpl: (path, options) => {
      requests.push({ path, body: JSON.parse(options.body) });
      return Promise.resolve({ ok: true, json: async () => ({}) });
    }});
    const task = activeTask({ session_provider: source, terminal_presence: "open", actions: { [target]: { available: true } }, launch_revision: "revision-1" });
    const row = api.renderDesktopThread(task);
    const targetButton = findByClass(row, target);
    assert.equal(targetButton.disabled, false);
    await targetButton.dispatch("click");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].path, "/api/tasks/task-1/launch");
    assert.equal(requests[0].body.provider, target);
    assert.equal(requests[0].body.launch_revision, "revision-1");
  });
}

test("compact cross-agent continuation stays disabled when unavailable", () => {
  const { api } = loadApp();
  const row = api.renderDesktopThread(activeTask({ session_provider: "claude", terminal_presence: "open", actions: { codex: { available: false, reason: "Original transcript unavailable" } } }));
  assert.equal(findByClass(row, "claude").disabled, false);
  assert.equal(findByClass(row, "codex").disabled, true);
  assert.equal(findByClass(row, "codex").title, "Original transcript unavailable");
});


test("compact defaults to latest activity in Open, All threads and Today", () => {
  const { api } = loadApp({ desktop: true });
  for (const queueMode of ["open", "all", "today"]) {
    api.setState({ queueMode });
    api.restoreViewSettings();
    assert.equal(api.getState().sortBy, "updated");
    assert.equal(api.getState().sortDirection, "desc");
    assert.ok(api.taskComparator(activeTask({ last_active_at: 20, last_activity_at: 1 }), activeTask({ last_active_at: 10, last_activity_at: 100 })) < 0);
  }
});

test("desktop priority-number sorting handles decimals, missing values in either direction, and priority buckets", () => {
  const now = Date.UTC(2026, 10, 3, 12) / 1000;
  const { api } = loadApp({ desktop: true, DateImpl: fixedDate(now * 1000) });
  api.setState({ sortBy: "priority_number", sortDirection: "asc" });
  const items = [
    activeTask({ task_id: "missing-null", priority_number: null }),
    activeTask({ task_id: "starred-nine", priority_number: 9, starred: true }),
    activeTask({ task_id: "decimal", priority_number: 2.5 }),
    activeTask({ task_id: "due", priority_number: 50, snoozed_until: now - 1 }),
    activeTask({ task_id: "one", priority_number: 1 }),
    activeTask({ task_id: "future-snooze", priority_number: 0, snoozed_until: now + 60 }),
    activeTask({ task_id: "missing-undefined" }),
  ];
  assert.deepEqual(items.slice().sort(api.taskComparator).map((task) => task.task_id), [
    "due", "one", "decimal", "starred-nine", "missing-null", "missing-undefined", "future-snooze",
  ]);
  api.setState({ sortDirection: "desc" });
  assert.deepEqual(items.slice().sort(api.taskComparator).map((task) => task.task_id), [
    "due", "starred-nine", "decimal", "one", "missing-null", "missing-undefined", "future-snooze",
  ]);
});

test("desktop priority-number edits persist decimals, reject invalid values, and restore on failure", async () => {
  const requests = [];
  const task = activeTask({ priority_number: 4 });
  const { api, elementFor } = loadApp({ desktop: true, fetchImpl: (url, options) => {
    requests.push({ url, method: options.method, body: JSON.parse(options.body) });
    if (requests.length === 1)
      return Promise.resolve({ ok: true, json: async () => ({ task: { ...task, priority_number: requests[0].body.priority_number } }) });
    return Promise.resolve({ ok: false, json: async () => ({ error: "Priority save failed" }) });
  }});
  api.setState({ latestBoard: { tasks: [task], projects: [], folders: [] } });
  const row = api.renderDesktopThread(task);
  const input = findByClass(row, "desktop-thread-priority");

  input.value = "-0.25";
  input.dispatch("change");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 0);
  assert.equal(input.getAttribute("aria-invalid"), "true");

  input.value = "2.75";
  input.dispatch("change");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "/api/tasks/task-1");
  assert.equal(requests[0].method, "PATCH");
  assert.equal(requests[0].body.priority_number, 2.75);
  assert.equal(task.priority_number, 2.75);

  input.value = "3.5";
  input.dispatch("change");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 2);
  assert.equal(task.priority_number, 2.75);
  assert.equal(input.value, "2.75");
  assert.match(elementFor("toast").textContent, /Priority save failed/);
});

test("a completed star save preserves a different focused priority draft", async () => {
  let resolveSave;
  const first = activeTask({ task_id: "first", title: "First", terminal_presence: "open", starred: false });
  const second = activeTask({ task_id: "second", title: "Second", terminal_presence: "open", priority_number: 3 });
  const board = { tasks: [first, second], projects: [], folders: [] };
  const { api, document } = loadApp({ desktop: true, fetchImpl: () => new Promise((resolve) => { resolveSave = resolve; }) });
  api.setState({ latestBoard: board });
  api.renderBoard(board);
  const rows = findAllByClass(document.getElementById("task-groups"), "desktop-thread");
  const firstRow = rows.find((row) => findByClass(row, "desktop-thread-name").textContent === "First");
  const secondRow = rows.find((row) => findByClass(row, "desktop-thread-name").textContent === "Second");
  findByClass(firstRow, "desktop-thread-star").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof resolveSave, "function", "the star PATCH is in flight");

  const priorityInput = findByClass(secondRow, "desktop-thread-priority");
  priorityInput.focus();
  priorityInput.value = "unfinished 8.25";
  resolveSave({ ok: true, json: async () => ({ task: { ...first, starred: true } }) });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(findByClass(document.getElementById("task-groups"), "desktop-thread"), rows[0]);
  assert.equal(findByClass(secondRow, "desktop-thread-priority"), priorityInput);
  assert.equal(document.activeElement, priorityInput);
  assert.equal(priorityInput.value, "unfinished 8.25");
});

test("desktop priority-number edit keeps focused input draft through a board refresh, and Enter blurs", async () => {
  const task = activeTask({ priority_number: 6, terminal_presence: "open" });
  const board = { tasks: [task], projects: [], folders: [] };
  const { api, document } = loadApp({ desktop: true, fetchImpl: () => Promise.resolve({ ok: true, json: async () => board }) });
  api.setState({ latestBoard: board });
  api.renderBoard(board);
  const input = findByClass(document.getElementById("task-groups"), "desktop-thread-priority");
  input.focus();
  input.value = "6.25";
  await api.fetchBoard(true);
  assert.equal(input.value, "6.25");
  assert.equal(document.activeElement, input);
  input.dispatch("keydown", { key: "Enter", preventDefault() {} });
  assert.equal(input.blurred, true);
  assert.equal(document.activeElement, null);
});

test("desktop sort preference restores per scope and defaults to latest updated otherwise", () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const { api } = loadApp({ desktop: true, storage });
  api.setState({ queueMode: "open", sortBy: "priority_number", sortDirection: "desc" });
  api.storeViewSettings();
  api.setState({ queueMode: "all", sortBy: "updated", sortDirection: "desc" });
  api.storeViewSettings();
  api.setState({ queueMode: "open" });
  api.restoreViewSettings();
  assert.equal(api.getState().sortBy, "priority_number");
  assert.equal(api.getState().sortDirection, "desc");
  api.setState({ queueMode: "today" });
  api.restoreViewSettings();
  assert.equal(api.getState().sortBy, "updated");
  assert.equal(api.getState().sortDirection, "desc");
});

test("compact thread displays repo beneath the title with the actual root as tooltip", () => {
  const { api } = loadApp({ desktop: true });
  const row = api.renderDesktopThread(activeTask({ repo_name: "team-operations", repo_path: "/repos/team-operations", project_name: "sales", session_cwd: "/repos/team-operations/clients/launcher", session_provider: "claude" }));
  assert.equal(findByClass(row, "desktop-thread-repo").textContent, "team-operations");
  assert.equal(findByClass(row, "desktop-thread-folder").textContent, "launcher");
  assert.equal(findByClass(row, "desktop-thread-folder").title, "/repos/team-operations/clients/launcher");
  assert.equal(findByClass(row, "desktop-thread-provider-label").textContent, "Claude");
});

test("compact metadata suppresses only the actual repo root and shows missing cwd and CodeX", () => {
  const { api } = loadApp({ desktop: true });
  const root = api.renderDesktopThread(activeTask({ repo_name: "Renamed project", repo_path: "/repos/team-operations/", session_cwd: "/repos/team-operations/", session_provider: "codex" }));
  assert.equal(findByClass(root, "desktop-thread-folder").textContent, "");
  assert.equal(findByClass(root, "desktop-thread-repo").title, "/repos/team-operations/");
  assert.equal(findByClass(root, "desktop-thread-provider-label").textContent, "CodeX");
  const nestedSameName = api.renderDesktopThread(activeTask({ repo_name: "team-operations", repo_path: "/repos/team-operations", session_cwd: "/repos/team-operations/clients/repo/", session_provider: "claude" }));
  assert.equal(findByClass(nestedSameName, "desktop-thread-folder").textContent, "repo");
  const renamedRoot = api.renderDesktopThread(activeTask({ repo_name: "Renamed project", repo_path: "/repos/team-operations", session_cwd: "/repos/team-operations/", session_provider: "claude" }));
  assert.equal(findByClass(renamedRoot, "desktop-thread-folder").textContent, "");
  const missing = api.renderDesktopThread(activeTask({ repo_name: "team-operations", session_cwd: null, session_provider: "claude" }));
  assert.equal(findByClass(missing, "desktop-thread-folder").textContent, "");
  assert.equal(findByClass(missing, "desktop-thread-repo").title, activeTask({ session_cwd: null }).session_cwd || "");
  assert.equal(findByClass(missing, "desktop-thread-provider-label").textContent, "Claude");
});

test("compact Today uses conversation activity rather than task edits", () => {
  const { api } = loadApp({ desktop: true });
  api.setState({ queueMode: "today" });
  assert.equal(api.taskMatches(activeTask({ in_today: true, active_today: false })), false);
  assert.equal(api.taskMatches(activeTask({ in_today: false, active_today: true })), true);
});


test("compact missing-history rows explain why cross-agent continuation is disabled", () => {
  const { api } = loadApp({ desktop: true });
  const row = api.renderDesktopThread(activeTask({ transcript_available: false }));
  assert.equal(findByClass(row, "desktop-thread-history-warning").textContent, "History missing");
});


test("compact board renders newest activity first in every scope, not server order", () => {
  const { api, elementFor } = loadApp({ desktop: true });
  const tasks = [activeTask({ title: "Older", task_id: "old", terminal_presence: "open", last_active_at: 10, active_today: true }), activeTask({ title: "Newest", task_id: "new", terminal_presence: "open", last_active_at: 20, active_today: true })];
  for (const queueMode of ["open", "all", "today"]) {
    api.setState({ queueMode });
    api.restoreViewSettings();
    api.renderBoard({ tasks, projects: [], folders: [] });
    assert.deepEqual(findAllByClass(elementFor("task-groups"), "desktop-thread-name").map((node) => node.textContent), ["Newest", "Older"]);
  }
});


test("closed or unknown compact rows never show a stale working status dot", () => {
  const { api } = loadApp({ desktop: true });
  for (const terminal_presence of ["closed", "unknown"]) {
    const row = api.renderDesktopThread(activeTask({ terminal_presence, terminal_runtime_state: "working" }));
    const status = findByClass(row, "desktop-thread-status");
    assert.equal(status.classList.contains("state-working"), false);
  }
});


test("compact includes verified process windows and focuses without inventing a session", async () => {
  const requests = [];
  const { api, elementFor } = loadApp({ desktop: true, fetchImpl: (path, options) => {
    requests.push({ path, body: JSON.parse(options.body) });
    return Promise.resolve({ ok: true, json: async () => ({ focused: true }) });
  }});
  const terminal_window = { pid: 123, provider: "codex", started: "Tue Nov 3 12:00:00 2026" };
  const rowTask = activeTask({ title: "CodeX terminal · ttys006", repo_name: "YogaNidra", terminal_presence: "open", session_provider: "codex", terminal_window, actions: {} });
  api.setState({ queueMode: "open" });
  api.renderBoard({ tasks: [], terminal_windows: [rowTask], projects: [], folders: [] });
  const row = elementFor("task-groups").children[0];
  assert.equal(findByClass(row, "desktop-thread-repo").textContent, "YogaNidra");
  assert.equal(findByClass(row, "desktop-thread-status").textContent, "Thread unidentified");
  assert.equal(findByClass(row, "claude").disabled, true);
  await findByClass(row, "codex").dispatch("click");
  assert.deepEqual(requests, [{ path: "/api/terminal-windows/focus", body: terminal_window }]);
});


test("compact pencil opens a rename editor and Escape preserves the title", async () => {
  const { api } = loadApp({ desktop: true });
  const row = api.renderDesktopThread(activeTask({ title: "Existing session" }));
  await findByClass(row, "desktop-thread-rename").dispatch("click");
  const editor = findByClass(row, "inline-rename");
  assert.equal(editor.value, "Existing session");
  editor.value = "Unsaved";
  await editor.dispatch("keydown", { key: "Escape", preventDefault() {} });
  assert.equal(findByClass(row, "desktop-thread-name").textContent, "Existing session");
});


test("compact rename saves via the session-backed task endpoint and reports sync failure", async () => {
  const requests = [];
  const task = activeTask({ title: "Existing session", terminal_presence: "open" });
  const { api, elementFor } = loadApp({ desktop: true, fetchImpl: (path, options) => {
    requests.push({ path, body: JSON.parse(options.body) });
    return Promise.resolve({ ok: true, json: async () => ({ task: { ...task, title: "Renamed session", session_name_updated: false, session_name_reason: "Provider name could not be updated.", terminal_title_updated: true } }) });
  }});
  api.setState({ latestBoard: { tasks: [task] } });
  const row = api.renderDesktopThread(task);
  findByClass(row, "desktop-thread-rename").dispatch("click");
  const editor = findByClass(row, "inline-rename");
  editor.value = "Renamed session";
  editor.dispatch("keydown", { key: "Enter" });
  editor.dispatch("keydown", { key: "Enter" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/tasks/task-1");
  assert.equal(requests[0].body.title, "Renamed session");
  assert.equal(findByClass(row, "desktop-thread-name").textContent, "Renamed session");
  assert.match(elementFor("toast").textContent, /Provider name could not be updated/);
});
