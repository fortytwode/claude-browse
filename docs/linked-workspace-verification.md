# Linked workspace: validation and operating notes

## Product outcome

One automatically captured terminal conversation is one task. The ClickUp-style
Space / Folder / List hierarchy organizes those tasks without rearranging files.
A List can link a working directory, explicitly create one directory, or remain
unlinked for planning. Moving a task changes its next launch destination, not an
already-running terminal. See [the user guide](linked-workspace-guide.md).

The local board remains at `http://127.0.0.1:51444/`. This is not a hosted or
cross-Mac deployment.

## Implementation boundaries

| Requirement | Implementation and evidence |
| --- | --- |
| R1-R5: hierarchy and explicit directory binding | `board/workspace.py`; additive migration, stable imported List identities, persistent empty Lists, strict parent validation, no-overwrite single-directory creation, missing/unlinked launch guards |
| R6-R10: task placement and intentional continuation | Task-placement CAS and Undo; `board/launches.py` one-use intents; `work_items.attach_continuation`; hook association before ordinary capture; original/new session history links |
| R11-R14: table and reader polish | Horizontal columns, header sorting, saved-view settings, lifecycle filters independent of Today, sidebar menus, project descriptions and escaped transcript reader |

The data path is: SessionStart payload (`session_id`, `cwd`, provider) -> runtime
session row -> automatic or canonical work item -> effective List context
(`list_key`, `working_directory`, `launch_revision`) -> board JSON -> explicit
provider/permission/revision launch request -> one-use local intent -> CLI
revalidation and provider invocation -> SessionStart association. Old transcript
files are neither moved nor rewritten by this path.

Same-provider/original-directory execution uses the existing native resume
implementation. A changed provider or directory uses a fresh context handoff.
Tracked History-reader launches use the same canonical task route, with the
next destination shown separately from the viewed conversation's original path.
Stale or missing launch revisions fail closed; old clients must refresh rather
than silently accepting a changed destination.

## Independent review and fixes

Compound Engineering review completed in a separate Codex context:
`/tmp/compound-engineering-501/ce-code-review/20260905-080458-a0ec84ad/review.json`.
The reviewed base was `83c66a7`. The initial verdict was **Not ready**; the three
retained findings were independently validated and then fixed in disjoint
storage, HTTP and UI batches:

1. Creating a working directory for an imported List now disables original-cwd
   inheritance, so its tasks actually use the new destination.
2. Tracked History launches now preserve canonical task association and require
   the current revision. The reader exposes authoritative launch metadata.
3. Sidebar Move up/down now uses a transactional, sibling-scoped server reorder,
   including dense positions to eliminate ties and browser integer rounding.

Additional cleanup removed misleading direct-session commands from task JSON,
disabled static-asset caching, serialized same-List parent moves, canceled stale
toast timers, synchronized restored toolbar values, and fixed narrow project
headers. No code or review content was sent to an external model provider.
Review personas reused bounded contexts due the thread cap; persona agreement
was not represented as fully independent. A separate Codex context validated
the three retained findings.

## Verification

- Full Python suite after the main review fixes: **704 passed**.
- UI logic suite after toolbar synchronization and menu queue coverage: **28 passed**.
- Ruff and `git diff --check`: passed.
- Browser checks used eight synthetic tasks and a separate temporary database.
  Terminal launching was replaced by command capture; no real agent was started.
- Observed browser passes: task-title transcript reading, List rename, task-to-List
  native dragging, immediate Undo, horizontal native column dragging, priority
  sorting, terminal-state grouping, Today filtering, saved-view layout restoration,
  unlinked-List launch disabling, and corrected project-header layout at 900px.
- Browser reconnects required fresh tabs. Initial incomplete checks in the review
  receipt were subsequently supplemented by the observed passes above.
- Final separate-context browser QA passed actual sidebar Move down/Move up,
  canonical History launch request (capture only), and 900px/390px layouts. The
  390px document had no horizontal overflow; temporary viewport overrides were reset.

## Local activation

The reviewed server was restarted on its existing port, 51444. A fresh database
backup was created at
`/Users/shamanth/.claude/agent-board/workspace-activation.68mhzK/state-before-activation.db`
and independently opened with `immutable=1`; its integrity check returned `ok`.
The normal additive migration backup remains separate from this activation copy.

After activation, both the read-only API and database checks showed **319 active
tasks**, **28 Lists**, and a launch context for every task. A SQL comparison with
the activation backup found **zero changed or missing task metadata records**,
**zero lost project aliases**, and **zero lost descriptions**. The live database
integrity check returned `ok`. The real board rendered successfully in a new
Chrome tab; no real task was moved or launched during that check.

## Limits and recovery expectations

- Real Claude/Codex processes and fresh-Mac native resumes were not end-to-end
  certified in this pass. Automated checks cover command selection, cwd, permission
  flags, intent consumption and hook association without launching paid sessions.
- Handoff requires the original raw transcript. Missing history leaves the task
  visible and blocks handoff; this feature is not an archive or backup system.
- A prepared/claimed launch is exclusive for up to 15 minutes. Check its Terminal
  window before retrying. Source tasks and conversations remain available on failure.
- Retry/recovery from a persistence failure during hook adoption is not implemented.
  Such a failure can leave the new conversation independently captured rather than
  attached; original transcripts remain intact. Do not interpret passing happy-path
  association tests as fault-injection certification of this case.
- Parent moves are serialized within one browser. Cross-tab conflict resolution,
  cross-Mac synchronization and hosted launch transport are not included.
- Local migration backups protect the board database, not years of transcript data.
  No agent-history cooling or deletion is authorized by this implementation.

## Post-Deploy Monitoring & Validation

For the first day of use, the board owner should check the first real continuation:
the selected directory in Terminal must match the displayed next destination, the
task must remain a single item, and both conversations must be readable from its
Conversation selector. Independently started terminal work must still appear.

Read-only database checks include `PRAGMA quick_check`, task counts by `status`,
and launch-intent counts grouped by `state`. Healthy signals are `quick_check=ok`,
preserved task metadata, and consumed intents after successful SessionStart.
Failure signals are repeated pending/failed intents, duplicate continuation tasks,
missing descriptions or an incorrect cwd. Stop using launch controls on an
incorrect destination; keep the original transcripts and inspect the displayed
error and intent state. Do not restore an old database over newly captured work.

Rollback is a code/server operation first. Preserve the current database and its
migration backup before any repair; additive workspace tables must not be dropped
as a cleanup step. Hosting, sync and archive work remain separate follow-up phases.
