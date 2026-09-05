# Open-terminal workspace polish: verification

Date: 2026-09-05. Plan: [Open Terminal Workspace Polish](plans/2026-09-05-1020-feat-open-terminal-polish-plan.md).

## Executive outcome

The local board now starts with verified open conversations, not every unfinished task. The ClickUp-style list has colored priority flags, collapsible groups, inline renaming, direct column arrows, adjustable column/sidebar widths, open/total sidebar counts, and persistent layout preferences. All threads and Today remain available; Thread History is secondary search.

Missing original transcripts are explained once. Explicit fresh starts offer either agent in the linked working folder without pretending to carry unavailable history. Token-authorized continuation keeps the same task and its previous conversation links. Workspace organization does not move repositories or existing terminal working directories.

Use the [workspace guide](linked-workspace-guide.md) and [live local board](http://127.0.0.1:51444/).

## Automated evidence

- Full Python suite: **785 passed** in the final full run (54.38 seconds), including the Node-suite wrapper.
- Node UI suite: **45 passed**. After the full run, two assertions were strengthened to verify the late poll did not replace board data and that actual fresh-action rendering enabled the buttons; the complete Node suite passed again.
- Ruff: all checks passed. `git diff --check`: clean.
- Presence fixtures cover actual macOS absent-PID exit status, PID/TTY/start identity, writable Codex descriptor identity, parent/subagent exclusions, unknown states, cache behavior, and a shared three-second scan deadline.
- Launch/adoption fixtures cover both providers and task/task-new intents, expiry, revision checks, duplicate exclusion, exact argv, history preservation, discovery between SessionStart and adoption, real SQLite writer exclusion, and concurrent reconciliation plus delayed historical hooks.
- UI handler tests cover priority keyboard selection/Escape/click-away, collapse persistence/focus, pointer cancellation cleanup, deferred polling during edits/resizes, sidebar drop bubbling and before/after placement, global sidebar preferences, per-view column widths, malformed preferences, date-filter boundaries, fresh-route payloads, and missing-transcript presentation.
- API tests retain Host/CSRF/strict-field checks and verify batched presence, timestamps, placement counts, and provider availability reuse.

No real provider conversation was launched for QA. Tests intercept terminal invocation and provider execution rather than spending model tokens or changing real conversation history.

## Live activation and data preservation

The identified previous board process was stopped and the updated server started at the same loopback address, `127.0.0.1:51444`.

At activation the API reported:

| Measure | Result |
| --- | --- |
| Saved tasks | 328 |
| Verified open conversations | 12: 3 Claude, 9 Codex |
| Closed conversations | 316 |
| Unknown presence | 0 |
| Previously missed open conversations captured | 7 |
| Existing tasks removed | 0 |
| Existing title/status/priority/due-date changes | 0 |
| Database integrity check | ok |

These are point-in-time counts, not fixed expected totals. Native read-only checks observed one Claude conversation close during verification; presence changed accordingly. Typical cold local scans measured roughly 0.18–0.30 seconds.

Recoverable SQLite backups are in:

`/Users/shamanth/.claude/agent-board/open-terminal-activation.Ult1tJ/`

`state-at-restart.db` is the immediate pre-activation copy. The backup was created with SQLite's online backup operation, not by copying an active WAL-backed file. Original provider transcripts were neither edited nor deleted.

The screenshot's missing Team Operations original was checked against the live API: native Claude resume remains offered; a Codex handoff is unavailable because the original is genuinely absent; fresh Claude and fresh Codex starts are both available. Fresh start is not recovery of missing context.

## Independent review and resolution

Compound Engineering planning, simplification, code review, and caller-owned fixes were used. Implementation/mechanical work and most reviewers used Terra; critical independent review and the adoption-race fix used the stronger Codex context. No project content was sent to an external model provider.

Review run: `/tmp/agent-board-polish-review.UiNrYK/` (`polishUiNrYK`), base `5998beb`.

Nine review lenses ran: correctness, security, adversarial, testing, API contract, maintainability, performance, reliability, and frontend races. One independent validator batch confirmed eight actionable findings. All eight were applied in file-scoped batches and inspected before final verification:

1. An exited Claude PID must not hide unrelated live conversations.
2. Fresh-start static test must match the shared action-route contract.
3. Discovery and reconciliation must not take ownership before token adoption.
4. Canceled resizing must release polling guards and event listeners.
5. Late polling responses must preserve an in-progress rename or resize.
6. Sidebar width is workspace-global, not view-local.
7. A list-row drop must not bubble into a second parent-move request.
8. Both provider scans share one aggregate deadline.

The concurrency fix deliberately defers an unowned session sharing provider/directory with an unexpired claimed/adopting task launch. It never adopts by directory. If the session is unrelated, a later pass captures it after that intent resolves or expires. This is a conservative temporary delay, not deletion or hidden permanent exclusion.

Optional frontend-module extraction and batched-lsof optimization were deferred: neither demonstrated a current user-facing failure, and neither is required for this pass. The review receipt's pre-fix verdict remains unchanged for auditability; this document records the subsequent resolution and verification.

## Genuine remaining validation limit

Real Chrome QA exercised the open/completed distinction, inline rename, flag colors, and desktop/narrow rendering before the automation connection dropped. It exposed clipping and alignment problems that were subsequently corrected.

The final drag/resize gestures and final visual layout have **not** been rechecked in a connected real browser. Handler-level regression tests are passing but are not a substitute for browser geometry or OS-level terminal launching. Chrome/extension/native-host diagnostics passed; an optional request to open a new Chrome window for reconnection was unanswered. No alternate automation channel or native-host repair was used. The QA viewport had been set to 1440×900 and could not be reset through the disconnected browser connection.

Remaining acceptance check after reconnection: resize both sidebar and table, drag a list and a task, check compact menus at desktop/narrow sizes, and hold an edit over a poll interval. No application-code blocker remains known from the completed review and tests; do not interpret that as a fabricated 95% end-to-end certification.

Cross-Mac synchronization, hosted Mission Control, transcript archival/restoration, and OS tab-title renaming remain outside this pass.
