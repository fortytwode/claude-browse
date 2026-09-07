# ClickUp-style reading and organization pass

## Goal and why

Make the automatically captured terminal-thread list pleasant to review and act on, using the user's September 5 ClickUp screenshot as the canonical visual reference. A row is a conversation, not an always-open edit form. Today the dense controls obscure the work, names do not open conversations, and browser transcript lookup differs from the CLI.

## Concrete product contract

1. In All active, click a task name → read that conversation in a detail panel → close it and return to the same project/view. Click the project breadcrumb instead → see that project's description and threads.
2. Open Today → group by priority or terminal state and filter by agent, priority, state or due date → see the matching work. Save the combination as a named browser-local view and reopen it after refresh. Today keeps its existing due/overdue-or-attention definition.
3. Open a project's three-dot menu → rename its display label or move it to a folder → the sidebar and breadcrumbs update, but repository files, session cwd, IDs and native resume remain unchanged. Folder renaming/reordering also persists.
4. Scan an airy, white task list → see project, name, due date, last update, priority, terminal state and agent → edit only the selected property or open the row menu. Priority drag/drop and keyboard movement remain available when manual ordering is unambiguous.
5. Open a hook-captured thread absent from the search index, or whose cached path is stale → use the same verified provider transcript resolution as launch → read and hand off when the raw file exists. A genuinely missing file produces a specific explanation, not invented content or silent success. Native launch availability remains separately evaluated.

## Decisions that constrain implementation

| Decision | Chosen | Alternative not chosen | Reason / reversibility |
|---|---|---|---|
| Visual baseline | Supplied ClickUp screenshot; white, airy list | Preserve dense form rows / OS-driven dark styling | Explicit user direction; CSS reversible |
| Hierarchy | One workspace, optional flat folders, repo-backed project lists | Arbitrarily nested enterprise spaces | Familiar three-level organization without new work semantics; additive |
| Move/rename | Organizational metadata only | Physically move repos / reassign native session cwd | Resume correctness; display changes reversible |
| Saved views | Browser-local named filter snapshots | Cross-Mac shared view storage now | Honest local scope before synchronization; replaceable |
| Transcript resolution | Shared verified local path resolution for read/launch | FTS-only viewer | Runtime capture and index are separate clocks |
| Task editing | Explicit contextual controls | Every field always editable | Reading is the primary action; reversible |

## Scope

Building the five scenarios above; keeping automatic enrollment, explicit done/archive, native versus cross-provider launch semantics, permission choice, local-only security, project description protection and drag safety. Not building cross-Mac synchronization, cloud hosting, archive deletion/restoration, assignees, recurring tasks, or arbitrary nested task systems. No transcript deletion or real-session launch is needed for validation.

## Implementation units and read map

- U1 Sidebar persistence: `board/work_items.py` (read completely), existing work-item tests (implementer reads before changes), new navigation tests. Add validated folder records and display aliases, transactional additive migration, preserve metadata during project reconciliation.
- U2 Transcript resolution: `board/commands.py` and `web.py` (read completely); provider resolver paths and their tests must be read during diagnosis before production edits. Unverified provider discovery is an explicit gate, not a claimed confidence score.
- U3 Reading-first UI: all three web assets and `tests/test_web.py` (read completely), new UI tests. Preserve per-field revision ordering, request sequencing, dirty guards and launch locks. UI worker owns assets only.
- U4 Integration/verification: root owns web HTTP routes, existing web test expectations and real-browser checks. Folder worker owns work-item storage; transcript worker owns command resolution. Disjoint shared-worktree writes, root owns integration, no worker git operations.

## Data and failure contracts

`sidebar menu {display_name:string|folder_id:string|null}` → guarded PATCH project route → `update_project(key, **changes) -> dict` → SQLite settings → board `{projects:[{project_key,name,path,description,display_name,folder_id,order,counts}],folders:[{folder_id,name,position}]}` → sidebar and breadcrumbs. Invalid names/folders reject without partial updates; filesystem paths never change.

`task click session_id:string` → GET session route → shared session resolution `{session_id,provider,cwd,path?,name?,timestamps?}` → provider `transcript_turns(path,sid,flatten=False)` → `{meta,turns:[{role,text}],transcript_error?}` → shared reader. Unknown session is 404; known missing transcript keeps metadata and truthful launch availability. Every path source must be read and verified before use.

`view controls` → validated `{scope,project,group,sort,filters}` snapshot → local filtering and sorting → rendered rows. Storage failure must be visible and cannot prevent ordinary browsing. Filtering never changes task lifecycle. Reordering is disabled under filters/search/nonmanual sort; priority edit remains possible independently.

## Build and verification order

| Unit | Dependency | Specific proof |
|---|---|---|
| U1 | Existing storage | Rename project/move folder/reopen database; same cwd, ID, transcript; invalid folder rolls back; migration repeat and reconciliation retain metadata |
| U2 | Verified provider discovery | Stale runtime + valid index; hook-only + canonical provider path; unknown ID; missing file; provider handoff availability agrees with reader |
| U3 | Fixed HTTP contracts | Name opens reader; breadcrumb selects project; Today grouping/filter; saved view refresh; contextual rename; priority drag and manual order safety |
| U4 | U1–U3 | Real HTTP tests, full pytest, ruff, JS syntax, Chrome desktop and narrow layout, real existing transcript read without launching agents |

## Premortem

- Most likely: stale runtime paths override valid index paths. U2 verifies candidate existence and shares resolution across interfaces.
- Second: UI redraw destroys edits or filter changes reorder hidden tasks. U3 retains dirty/edit revision guards and separates priority edits from manual reordering.
- Sneaky: repo discovery changes a project key and drops an alias/folder. U1 migration/reconciliation tests cover preservation; no filesystem move is permitted.

## Readiness and confidence gate

Feature clarity: 4/4 (one sentence, happy/edge/failure scenarios, deterministic behavior, blocking choices settled). Per section, award exactly 20 each for complete source reads, typed data trace, signatures, failure behavior, and a concrete test. U1/U3 current source contracts are traced and testable; implementers must confirm affected tests before production edits. U2 remains below build threshold until provider discovery diagnosis is complete. No score represents a guarantee of bug-free software. Verification receipts, not plan status edits, record completion.

## Definition of done

Every scenario has observed verification; safe local failures are explained; no enterprise/cloud scope added; screenshot visibly reflects the reference's list/navigation hierarchy and whitespace. Independent Codex-only review checks substantive regressions. User gets the working local UI and a concise nontechnical summary including any limitations.
