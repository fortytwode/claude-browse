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

function loadApp({ DateImpl = Date, fetchImpl, initialize = false, setTimeoutImpl } = {}) {
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
    querySelector: () => null,
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
    clearTimeout() {},
    window: {
      addEventListener() {},
      crypto: { randomUUID: () => "test-client" },
    },
  };
  const testExports = `
globalThis.testApi = {
  fetchBoard,
  isDueTodayOrOverdue,
  renderTaskRow,
  renderTurnBody,
  moveProject,
  openEditor,
  reorderTask,
  saveTask,
  taskMatches,
  getState: function () {
    return { latestBoard: latestBoard, queueMode: queueMode, selectedProject: selectedProject };
  },
  setState: function (next) {
    if (Object.prototype.hasOwnProperty.call(next, "latestBoard")) latestBoard = next.latestBoard;
    if (Object.prototype.hasOwnProperty.call(next, "queueMode")) queueMode = next.queueMode;
    if (Object.prototype.hasOwnProperty.call(next, "selectedProject")) selectedProject = next.selectedProject;
    if (Object.prototype.hasOwnProperty.call(next, "sortBy")) sortBy = next.sortBy;
    if (Object.prototype.hasOwnProperty.call(next, "filters")) filters = next.filters;
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
