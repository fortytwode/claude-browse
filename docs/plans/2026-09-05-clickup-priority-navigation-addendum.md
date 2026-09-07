---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: user-feedback-addendum
title: "ClickUp Priority and Project Navigation - Addendum"
type: feat
date: 2026-09-05
depth: standard
---

# ClickUp Priority and Project Navigation - Addendum

## Goal Capsule

- **Objective:** Make Agent Board behave like the ClickUp task list Shamanth expects: persistent project navigation, understandable project/thread context, four priorities, grouping, and drag ordering.
- **Means:** Extend the existing local work overlay and dense Work UI; do not create a general project-management system.
- **Authority:** This addendum supersedes the original plan's decision to defer priority. The original automatic-capture, lifecycle, launch, security, and local-first decisions remain authoritative.
- **Stop conditions:** Do not make project identity user-mutable, do not let drag mutate terminal state, do not add hook Git/network work, and do not sync planning metadata remotely.

## Product Contract

### What and Why

The current board is a dense list, but it still lacks the familiar ClickUp mechanics required for daily prioritization. The user cannot navigate by project from a sidebar, see enough context to understand a project/thread, group by priority or terminal state, or arrange work manually.

### Requirements

- P1. Work has a persistent left sidebar with All active, Today, Done & Archived, and one entry/count per real Git repo or canonical folder.
- P2. Selecting a project filters its threads and shows project name, exact path, local editable description, and active/today/needs-input counts.
- P3. Each row shows a one-line local transcript preview without copying transcript text into the work database.
- P4. Every row has exactly one priority: Urgent, High, Normal, or Low; new and migrated rows default to Normal.
- P5. The main list groups by Priority by default or Terminal state on request. Priority and terminal groups have stable fixed ordering.
- P6. Dragging within a group persists manual order. In Priority grouping, cross-group drop atomically changes priority and order. In Terminal-state grouping, cross-state drop is rejected because terminal state is runtime truth.
- P7. Projects may be presentation-reordered in the sidebar, but tasks cannot move between projects. New projects append.
- P8. Drag behavior has keyboard Move up/Move down and Set priority equivalents, live announcements, rollback on failure, and retained focus.
- P9. Search disables reorder with a clear reason. Done/Archived can reorder only inside their closed view and drag never restores them.
- P10. Priority, order, and project descriptions are local-only and survive hooks, completion, restoration, refresh, and restart.

### Concrete Scenarios

- A new CodeX thread in `claude-browse` appears at the bottom of Normal; later hooks do not change its priority or position.
- Selecting `claude-browse` shows its description/path/counts and only its threads, with a transcript preview under each title.
- Dragging a Normal thread into Urgent updates both fields atomically and the placement survives restart.
- In Terminal-state grouping, reordering two Needs input rows succeeds; dropping one into Working is rejected and snaps back.
- Keyboard Move up and Set priority High produce the same persisted result as drag.
- A failed reorder or description save leaves stored state unchanged, restores the prior visual order, and announces the error.

### Scope

Building: sidebar navigation, local project description/order, four priorities, priority/terminal grouping, persisted task/project order, summaries, drag and keyboard controls.

Not building: arbitrary projects, moving threads between repos, custom statuses/priorities, per-view order, assignees, subtasks, estimates, comments, kanban, AI summaries, or cross-Mac planning sync.

### Readiness

- [x] One-sentence feature explanation
- [x] Happy, edge, failure, and accessibility scenarios
- [x] Two developers would implement the same lifecycle and drag semantics
- [x] Blocking decisions resolved

Feature clarity: **4/4 (100%)**.

## Key Technical Decisions

- D1. Extend `work_items` with `priority` and integer `position`; use `time.time_ns()` for cheap append-like hook insertion and deterministic task-id tie-breaks.
- D2. Add local `project_settings(project_key, description, position, updated_at)`; repo/folder name/path remain derived truth.
- D3. Use dedicated transactional reorder services. Client supplies the visible destination-group order; server validates bounded, unique, same-project/session-backed IDs and reassigns existing ordered slots atomically.
- D4. API owns normalized priorities, fixed group ordering, summaries, projects, counts, and mutation validation. UI owns selection, grouping, drag affordances, and rollback presentation.
- D5. Transcript summary is derived at response time from indexed `last_msg`, falling back to `first_msg` and then a placeholder; it is never persisted into planning tables.

## Implementation Units

### U6. Add priority, ordering, and project APIs

- **Goal:** Persist and safely mutate the new local planning fields.
- **Files:** `claude_browse/board/work_items.py`, `claude_browse/web.py`, `tests/test_board_work_items.py`, `tests/test_web.py`, `tests/test_board_sync.py`.
- **Approach:** Migration-safe fields/table/indexes with a new one-time backup; preserve fields through hook conflict updates and lifecycle transitions; add priority mutation, atomic task reorder, project description/order services and protected routes; return task summaries plus normalized project aggregates.
- **Proof first:** Add red tests for migration/default/preservation, invalid priority, atomic reorder validation/rollback, project persistence, response aggregates/summary fallback, new-route security, and remote projection exclusion.
- **Verification:** Focused store/work/web/sync tests and hook latency/no-subprocess regression pass.

### U7. Add sidebar, grouping, and drag interactions

- **Goal:** Deliver the familiar project/priority review surface with accessible persisted ordering.
- **Dependencies:** U6.
- **Files:** `claude_browse/webassets/index.html`, `claude_browse/webassets/app.js`, `claude_browse/webassets/app.css`, `tests/test_web.py`, `README.md`, `CHANGELOG.md`, `tests/test_readme.py`.
- **Approach:** Two-column Work layout; project sidebar/detail editor; row previews and priority control; Priority/Terminal-state grouping; HTML drag handles/drop zones; same-project/order validation; keyboard move/priority actions; search reorder lock; optimistic rollback/error/focus handling; responsive sidebar collapse.
- **Proof first:** Extend the static asset contract before implementation; browser-test all concrete scenarios at desktop and narrow widths.
- **Verification:** Node syntax, focused/full tests, lint, real Chrome QA, and diff check pass.

## Integration and Failure Map

`hook payload -> work_items(priority=normal, position=time_ns) -> read-only board normalization -> sidebar/grouped UI -> authenticated priority/reorder/project mutation -> one SQLite transaction -> refreshed canonical order`.

Invalid/cross-project/closed IDs, invalid priority, oversized description, hostile request, index outage, or database failure must affect only the requested mutation/view and never change project identity, runtime state, or unrelated ordering.

## Earned Confidence

| Section | Files read | Exact types/data traced | Signatures known | Failures known | Tests writable | Score |
|---|---:|---:|---:|---:|---:|---:|
| Data/migration | 20 | 20 | 20 | 20 | 20 | 100 |
| API/aggregation | 20 | 20 | 20 | 20 | 20 | 100 |
| UI/drag/accessibility | 20 | 20 | 20 | 20 | 20 | 100 |
| Integration | 20 | 20 | 20 | 20 | 20 | 100 |

Lowest score: **100/100. Build.**

## Definition of Done

- All P1-P10 scenarios are implemented without changing the original automatic-capture/launch/security contracts.
- Priority/order/description migrations are lossless, backed up, idempotent, and local-only.
- Drag and keyboard behavior persist identically; runtime/project truth cannot be mutated through grouping.
- Chrome desktop/narrow QA visibly matches the ClickUp navigation and grouping intent.
- Full tests, Ruff, JavaScript syntax, diff check, review, PR, and CI gates pass.
