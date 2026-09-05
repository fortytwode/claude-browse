# ClickUp-style Agent Board polish: verification receipt

Date: September 5, 2026. Baseline: `72e9245`, branch `feat/thread-work-queue`.
Scope: [reading and organization plan](plans/2026-09-05-clickup-reading-and-polish-plan.md).

## Delivered locally

- The supplied September 5 ClickUp screenshot is the canonical design reference: light workspace/sidebar, optional folders containing repo-backed project lists, purple accents, spacious task rows, understated property controls, and contextual menus.
- Project and folder three-dot menus support display renaming and movement. Repository paths, session IDs, and native resume targets do not move. Project descriptions are available in the project view.
- Task titles open a shared transcript reader with editable task properties and native/cross-provider launch actions. Project breadcrumbs open their project. The permission toggle remains accessible inside the reader dialog.
- Today/All active/Done retain automatic thread enrollment and support grouping by priority/terminal state, filtering, Last update and due-date sorting, and browser-local named views. Manual order and priority drop operations retain project/status boundaries and are disabled under ambiguous filters/sorts.
- Transcript reading and launching now share local path resolution: valid runtime path, same-provider index path, then exact provider filename discovery. Hook-only threads can be read without waiting for the search index. Missing local transcripts retain task metadata and a truthful explanation; no restoration is invented.
- Save/cancel, stale-response, dirty-description, per-task edit ordering, folder-specific movement, mobile navigation availability, and stalled-save regressions found during independent Codex review were corrected. A task edit times out after 15 seconds without claiming the server rolled back; the user retains the unsaved field and can explicitly dismiss it.

## Automated evidence

| Check | Final result |
|---|---|
| `.venv/bin/python -m pytest -q` | 655 passed in 33.14 seconds |
| Node behavior suite, also invoked by pytest | 12 passed |
| `.venv/bin/ruff check claude_browse tests` | Passed |
| `node --check claude_browse/webassets/app.js` | Passed |
| `git diff --check` | Passed |

New coverage includes additive sidebar migration, alias/folder reconciliation, invalid moves and ordering, stale/missing/provider-mismatched transcript paths, hook-only HTTP reads, metadata retained on missing transcripts, folder mutation guards, local-day due filtering, editor cancellation, clearing dates, serialized edits, stale GET rejection, stalled-save release, and dialog close isolation. Source-contract checks are not substitutes for real pointer/keyboard tests.

Independent review used separate Codex contexts only. The backend review found no actionable regression. The UI reviewer rechecked the corrections and reported no remaining finding in the bounded follow-up scope.

## Live checks

The real board was restarted at the same loopback origin, `http://127.0.0.1:51444/`, preserving the current browser address. A launcher can now optionally request a stable local port; default CLI behavior is unchanged. The refreshed code and backend are running together.

- Board GET returned 318 tasks and 28 projects in 0.071 seconds at the check time. Counts can change as hooks capture more threads. Every task exposed Last update; every project exposed the new organization metadata.
- One small existing Codex transcript and one small existing Claude transcript each returned HTTP 200 with matching task metadata, parsed turns, and launch controls. This was read-only; no real agent was launched.
- HTML, JavaScript and CSS assets returned HTTP 200 with Content Security Policy headers.
- A mutation without the request token returned HTTP 403 and created no folder.
- The additive-migration backup exists at `~/.claude/agent-board/state.db.pre-sidebar-organization.bak`. No transcript files or repositories were moved or deleted.

## Outstanding validation and deliberate limits

**Visual acceptance is not complete.** Chrome automation repeatedly detached/timed out, including after reacquiring the browser. Official diagnostics reported Chrome, its enabled extension, and its native host installed correctly. No unauthorized fallback browser-control method was used. Code/source and behavior tests do not earn the missing real-browser evidence; do not call this a fully visually verified or 95%+ end-to-end signoff.

The remaining browser pass is: compare the actual desktop rendering to the canonical screenshot; open a task and project from All active; read a thread and close it; exercise sidebar rename/move and saved-view recall; drag within a priority and into another priority; filter/group Today; check narrow layout and keyboard navigation. Use isolated sample records for mutations and capture screenshots after the automation connection recovers.

Named views are stored in this browser at this origin, not synchronized between Macs. Folders/aliases/task properties are local database metadata. Cross-Mac synchronization, hosted Mission Control, durable archive/restoration, physical repo moves and enterprise hierarchy are not part of this pass. A genuinely absent transcript still cannot be read or handed off until its original provider-compatible history is restored by the separate storage workflow.
