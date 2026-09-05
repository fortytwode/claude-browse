---
title: Open Terminal Workspace Polish
type: feat
date: 2026-09-05
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Open Terminal Workspace Polish

## Goal Capsule

**Objective:** Find the conversations open on this Mac, prioritize them, and resume work comfortably from one familiar task list.

**Means:** Refine the existing local Agent Board using the user's ClickUp screenshots as the visual reference. Keep the existing task, workspace, transcript, and launch stores.

**Success:** A user can immediately distinguish open conversations from old work, change a priority/name/layout without hunting through menus, and either continue with available history or explicitly start a fresh conversation.

## Product Contract

### Problem frame

The board's unfinished-work status currently says Active even when the terminal conversation ended weeks ago. Native dropdowns, oversized controls, hidden sidebar menus, and fixed widths make reviewing 320 threads awkward. Some saved tasks have no remaining original transcript; hiding this fact or inventing a handoff would lose the distinction between continuing a conversation and starting over.

### Requirements

#### Daily workflow

- **R1:** The initial work view is Open terminals on this Mac. This is independent of work completion. All threads and explicitly archived work remain accessible; closing a terminal never archives a task.
- **R2:** Presence has three states: open, closed, unknown. Open requires a verified local live provider/session association, not a recent heartbeat. Unknown is visible and never described as closed. A bounded cached scan runs once per board request, not once per task.
- **R3:** Lists, folders, and spaces show open/total thread counts. Sidebar defaults to open-first, with an explicit Manual order option; dragging a list switches to Manual and persists sibling ordering. Task-to-list drag keeps its existing meaning and launch-directory safety.
- **R4:** Keep full Thread History as secondary transcript search. Daily reading, renaming, and continuation stay within the work view.

#### ClickUp-style interaction

- **R5:** Priority uses colored flag buttons: urgent red, high amber, normal blue, low gray. Each priority group has a collapse/expand chevron and count. Empty groups are compact drop targets, not large empty tables.
- **R6:** Name cells open the conversation. A separate pencil opens inline rename with Enter/save and Escape/cancel. Existing serialized title mutation remains authoritative and updates the board terminal status line. Do not rewrite provider transcript files or claim existing Terminal.app tab names were changed.
- **R7:** Headers offer direct left/right arrows, drag reorder, and resize handles. Column widths/order and group collapse persist per view; sidebar width persists locally. Clamp dimensions and handle invalid stored values. Keyboard arrows on resize handles provide an alternative to dragging.
- **R8:** Toolbar is compact: search, open/all scope, Group, Sort, Filters. Filter by agent, priority, terminal runtime state, due date, and last update (any / 24 hours / 7 days / 30 days). Sort by latest update, priority, terminal state, agent, name, due date, or manual with visible direction. Newest-first must actually put the newest timestamp first.
- **R9:** Sidebar must not require horizontal scrolling to reach names, counts, or ellipsis menus. Truncate long names with a tooltip, keep menus visible, retain Space → Folder → List indentation and responsive narrow-screen access.

#### Conversation actions

- **R10:** Resume/Continue retains existing native/handoff semantics, including exact transcript discovery. When the original is genuinely missing, say so once in plain language; do not display an empty search box as though the transcript loaded.
- **R11:** A secondary Start fresh action offers Claude or Codex in the task's linked directory, even if the original transcript is missing. Explicitly state that the old conversation is not carried over. The new session attaches to the same task through the existing single-use launch token; old linked history remains intact.
- **R12:** Preserve loopback/Host/CSRF checks, strict launch fields, current-directory revisions, argv quoting, one-use tokens, expiry, duplicate-launch exclusion, and full-access choice. No provider is started merely by reading/testing a page.

### Acceptance examples

1. Three verified Claude sessions and two verified Codex sessions are open on this Mac. Open terminals shows their tasks even if one is idle or marked complete; All threads shows older tasks too. A foreign-Mac or failed-scan row is Unknown, not falsely closed.
2. A user clicks Normal's flag, chooses Urgent, then collapses Urgent. The task persists as urgent; the group count remains visible. Reopening the view preserves collapse and chosen column widths.
3. A user renames a thread from the pencil, presses Enter, then opens it. The saved name appears in both places and the Agent Board status line on its next render; a failed request leaves a recoverable edit/error, not a false success.
4. A user drags a list above its sibling, then drags a task into that list. Manual list order survives refresh; the task's next launch uses the destination's linked directory. No live terminal cwd or repository folder is moved.
5. A saved Claude task has no JSONL. Continue in Codex is disabled with the specific reason. Start fresh in Codex is available if its binary/directory are ready, opens through a protected token, and the hook attaches the new session without deleting the old link. A missing folder or expired/stale token launches nothing.

### Scope boundaries

Building R1–R12 only. No cross-Mac synchronization, cloud deployment, transcript archive/restore/deletion, OS tab control, enterprise task system, dependency installation, or new frontend framework. User-provided historical screenshots are a visual reference, not proof of current filesystem state.

## Planning Contract

### Key decisions

| Decision | Chosen | Rejected | Why / reversal cost |
|---|---|---|---|
| Open versus unfinished (session-settled: user explicit) | Independent presence scope; status label To do / Completed / Archived | Reusing Active or heartbeat freshness | Matches an open terminal; additive API/UI, reversible |
| Presence evidence | Local provider process artifacts and exact session IDs; unknown on incomplete evidence | PID/TTY/provider-only matching | Avoid showing unrelated sessions as open; isolated scanner |
| Missing original | Preserve native resume, truthful unavailable handoff, explicit fresh start | Inventing context or deleting task | User data remains recoverable; additive launch kind |
| Organization | Board hierarchy and linked working directory remain separate | Automatically rename/move repos | Existing safe model retained |
| Table UI (session-settled: ClickUp reference) | Compact list, flag menus, disclosure groups, resize edges | New cards/dashboard or native priority popup | Directly addresses screenshots, no dependencies |
| Ordering | Open-first display; explicit Manual; server atomically reorders siblings | Repeated up/down HTTP calls | Predictable persistence, single transaction |

### Verified data paths and failures

- `store` runtime records `{session_id, provider, host, state, ...}` + live provider artifacts → `presence.snapshot(rows: list[dict]) -> dict[str, str]` → API `{terminal_presence, terminal_open}`. Bound subprocess deadlines/cache; malformed metadata, unsupported host, failed scan produce unknown as appropriate. Never publish presence to Firestore/Slack or change lifecycle.
- `work_items.list_items` + FTS session `{last_timestamp, ...}` + workspace context → `_task_to_json(...) -> dict` → frontend filter/sort/counts. `last_activity` is the maximum valid runtime/task/index timestamp; malformed/missing index dates fall back. API exposes numeric seconds, frontend uses milliseconds only at Date boundaries.
- Flag/pencil → serialized existing `saveTask` → strict PATCH → `work_items.mutate` → saved task + runtime manual name → repaint. Keep per-field revisions, edit focus, and polling guards; errors are visible and drafts recoverable.
- `POST /api/tasks/:id/start` `{provider, full_access, launch_revision}` → `launches.prepare(kind="task-new", ...)` → token-only terminal invocation → claimed revision-checked execution without old session context → existing hook `adopt_session` attaches new SID to canonical task. Duplicate task resume/fresh intents exclude each other. Error/expired token launches nothing; exec failure marks failed.
- Sidebar drop → existing `POST /api/workspace/reorder` with `{kind,node_id,target_id,placement:"before"|"after"}` (existing direction shape retained) → `workspace.place_node` same-sibling transaction → refreshed positions. Cross-parent movement continues existing explicit move path. Reject foreign target/unknown IDs and roll back atomically.

### Evidence and known surprises

Root read the complete web handler, frontend assets, launch module, commands module, workspace module, naming module, and status-line module before planning. Research workers traced provider discovery, local live processes, and existing tests. Each implementation owner must read its entire owned files and required callees before production edits, identify exact signatures/failures/tests, and report its five earned checklist items. Missing items cap that unit at 80 or below; no subjective 95% claim.

The screenshot's Team Operations session `acb992ff-1936-45ff-832a-40038b38f1be` has no existing original JSONL or index row on this Mac. Claude has a history-backed file discovery helper that should be reused; this cannot recreate the missing original. Codex processes can hold multiple rollout files: a descriptor must be session-identifying live evidence, not merely a read-only parent-history file. Probe ambiguity is a blocker for calling that row open; preserve unknown if needed.

### Pre-mortem

- A read-only Codex parent descriptor is mislabeled open → U1 must check descriptor access/identity and test multiple descriptors rather than treating any file as its terminal session.
- A polling refresh destroys an edit or drag → U3 must protect inline edits, menus, and resize interactions; browser QA holds them open over a poll interval.
- A Start fresh launch accidentally resumes the old session or duplicates its task → U2 verifies new-mode argv and shared duplicate lock; U4 validates route fields and hook adoption fixtures.

## Implementation Units

### U1 — Read-only local terminal presence (Terra)

Own new `claude_browse/board/presence.py` and `tests/test_board_presence.py`. No edits to web, hooks, providers, or persistence. Read provider-native metadata/parsers and relevant store host identity. Implement `snapshot(rows: list[dict]) -> dict[str, str]`, process/metadata parsing helpers, bounded cached scans (5 seconds), and injectable fixture boundaries. Match current task session only; prior linked conversations remain accessible in history and do not change which conversation the task currently represents. Tests cover live/stale PID, no TTY, wrong command, PID reuse where evidence exists, multiple read/write descriptors, same-ID/provider/host guards, local unmatched, foreign/hostless, failure/timeout, and no state writes.

Exact qualifying predicates: Claude PID metadata identifies the exact session and a live Claude process with a terminal TTY; validate its recorded process start when available. Codex requires a live terminal-backed Codex process holding a writable (`w`/`u`) canonical rollout descriptor, a bounded first `session_meta` record whose ID matches the filename and row ID, and a source that is not a subagent. Read-only parent descriptors, identity mismatches, and ambiguous or incomplete joins never produce Open; their candidate rows remain Unknown. Subagent writer descriptors cannot establish a terminal conversation. A successful complete scan with no evidence for a local row yields Closed. A verified live association takes precedence over a stale ended runtime record. Cap aggregate scan duration as well as individual command timeouts; process failure does not become a successful empty scan.

### U1b — Capture already-open conversations missed by older hooks (Terra)

Live verification found nine Codex user roots (`source=cli`, `originator=codex-tui`, `thread_source=user`) but only two had board runtime rows. Seven long-lived terminal conversations therefore require additive automatic capture to satisfy R1; fifteen explicit subagent descriptors must not enroll.

Own additions to `board/presence.py`, new `board/discovery.py`, and presence/discovery tests. Expose `presence.live_sessions() -> list[dict]` containing only verified local user conversations `{session_id,provider,cwd,path?}` from the same cached native evidence (Claude live PID metadata, Codex exact writable root `session_meta`). Require explicit user CLI source for newly enrolling Codex roots; no inference from TTY alone. Use FTS only for optional exact-provider title enrichment, not presence or identity authority.

`discovery.capture_live_sessions() -> int` runs at startup and within the existing periodic reconciliation worker, never from GET. Insert missing runtime rows with `INSERT OR IGNORE` using observed host/provider/cwd/path, then `ensure_for_session(..., reactivate_done=False)` only for sessions without an existing canonical task. Preserve existing runtime updates in races and never reactivate or rename completed/archived/manual tasks. A failure can leave a runtime row for normal reconciliation to adopt, but never removes data. No transcript writes or invented runtime work state. Tests prove user roots absent from FTS are captured, explicit subagents/unknown evidence never enroll, repeated capture is idempotent, concurrent hooks win, linked prior sessions do not create duplicate tasks, and archived task metadata is unchanged.

Root U4 wires this into the existing startup/30-second reconciliation loop and validates the full local open count. Presence remains read-only; discovery is the explicit capture boundary. This is a completion of automatic terminal capture, not a new opt-in work queue or indexing policy.

### U2 — Fresh starts, transcript discovery, sidebar ordering (Terra)

Own `claude_browse/board/commands.py`, `claude_browse/board/launches.py`, `claude_browse/board/workspace.py`, and focused tests in new `tests/test_workspace_polish.py` / existing transcript tests if needed. Reuse Claude's exact history-backed file discovery within approved root; preserve filename/SID/provider checks. Extend launch kind `task-new`: resolve source task for identity/revision, but validate/execute new-session action with no transcript input. Use the original source SID only for canonical adoption. Add `place_node(kind: str,node_id: str,target_id: str,placement: str) -> dict` atomic same-parent dense ordering. Do not edit web or UI. Test missing originals, argv/full-access, duplicate resume-vs-new, stale revision/expiry, adoption/history preservation, sibling before/after, invalid parent rollback.

Specifically, `_resolve` may retain the source session for identity, but `prepare`/`claim` call `action_status(None,...)` for `task-new`; `execute` selects fresh argv by kind, not source-session truthiness. `adopt_session` handles both `task` and `task-new` as canonical attachments. Pending-intent lookup shares a task-target lock across these two kinds; List starts remain separately scoped.

### U3 — ClickUp-style work surface (Terra)

Own `claude_browse/webassets/app.js`, `app.css`, `index.html`, `tests/web_ui_logic.test.cjs`, and `tests/test_web_ui_polish.py`. No production Python changes. Implement against this plan's frozen API shapes; U4 wires the API independently, then verifies both sides together. Initial Open terminals scope spans work statuses; explicit All threads scope retains work-status filter. Preserve legacy saved views but use a versioned new default. Closed terminals and Archived are distinct labels; history is secondary.

Terminal column shows Open / Closed / Unknown from presence first; for Open only, show the last known working/idle/needs-input runtime state as secondary text, never label an open process Gone because its heartbeat is old. Runtime-state filter and sort use that separate last-reported state, with the label explaining it. Open scope contains verified-open only. Unknown stays available in All threads and a presence filter; the toolbar reports unknown count beside an All threads escape. Until first response show Checking open terminals; zero verified open says No verified open terminals with All threads action. Partial unknown results say some sessions could not be verified. A request error preserves last data with a stale/error notice and retry; it never substitutes a zero count.

Implement colored accessible radio menus (Arrow keys/Enter/Escape, click-away, focus return); collapsible groups; inline rename; direct column arrows; pointer/keyboard resize handles; sidebar truncation/counts/open-first vs manual sibling drops; last-update filters and correct sort direction. Prefer native semantics for simple toolbar selects styled as compact chips; only priority needs custom flag options. Persist bounded dimensions/collapse/view state defensively. Protect pending saves, inline edits, open menus, and pointer gestures from 10-second polling re-render. On mobile preserve cards and touch-friendly controls; sidebar has reachable scroll and resize only where meaningful.

Group toggles are buttons with accessible names, aria-expanded/aria-controls, native Enter/Space behavior, and retained focus. Existing sidebar Move up/down and move-parent dialogs remain the keyboard/touch alternatives; add a task row-menu Move to list selector as the non-drag task-placement path. Announce success/errors in the existing toast/live region. No drag-only action.

Manual task reorder may operate on a visible subset using existing slot-preserving server ordering, but only within the same list/status group in Manual mode. Never silently reorder filtered-out rows or change status. Clearly disable cross-list rank drops; task-to-sidebar move stays enabled. Start fresh buttons use R11 route and `start_actions`; prior source handoff semantics remain distinct. Missing transcript UI has one explanation and no pointless empty search state.

### U4 — API integration and verification (root)

Implements the frozen API shapes independently; final integration verification depends on U1/U2/U3 code. Own `claude_browse/web.py`, new `tests/test_web_polish.py`, plan and verification/guide docs. Add one cached presence snapshot for board tasks, aggregate project/list/space/folder counts, include fresh actions in task/detail payload, start route and existing reorder route's alternate validated shape. Include FTS timestamp in last_activity. Do not expand sync projection.

Read final worker diffs, run authoritative full suites only after concurrent edits settle, review in separate Codex contexts (no external provider), and exercise real browser UI against isolated test DB/transcripts. Back up the real SQLite database before restarting only the identified board server. Leave a live verified board tab for the user.

## Verification Contract

- Focused Python fixtures: U1 presence positive/negative/error cases, explicitly read-only Codex parent and writable subagent/mismatched rollout never Open; U2 token, filesystem, exact transcript, reorder/adoption cases; U4 strict API/CSRF and numeric timestamp/count cases.
- Node UI tests: open/unknown/status composition, newest-first and filter boundaries, saved view round trip, flags/keyboard, collapse, inline rename success/failure, resize clamp/persistence, sidebar open/manual counts, start endpoint payload.
- Full regression: `.venv/bin/python -m pytest`, `node --test tests/web_ui_logic.test.cjs`, `.venv/bin/ruff check .`.
- Browser fixture at desktop and narrow widths: priority change, collapse, resize columns/sidebar, arrows, rename, filter/sort, sibling drag, task-to-list drop, missing transcript start choices, loaded transcript continuation. Verify one full polling interval with an edit open. Do not launch real AI conversations for QA; assert launch invocation with test boundaries instead.
- Live data read-only verification: local open count matches qualifying process artifacts; real missing transcript remains accurately explained. Record actual results/limits, not estimated confidence.

## Definition of Done

R1–R12 have specific test/browser evidence or an explicitly documented genuine platform limit. No unreviewed production edits, no mutations to original transcripts, no data cleanup, no remote deployment. Full regressions pass, local board is activated from verified code with recoverable DB backup, and final executive summary explains what changed and what remains impossible without the missing original files.
