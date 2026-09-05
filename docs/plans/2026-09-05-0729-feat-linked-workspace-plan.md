---
title: Linked Workspace - Plan
type: feat
date: 2026-09-05
topic: linked-workspace
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
execution: code
---

# Linked Workspace - Plan

## Goal Capsule

- Objective: Organize terminal work in a familiar ClickUp-style hierarchy and continue a task in its chosen working folder without losing the conversation.
- Product authority: The user's approved September 5 linked-workspace proposal and supplied ClickUp screenshots.
- Open product blockers: None. Native cross-directory session identity is not promised; relocation uses an explicitly labeled continuation.

---

## Product Contract

### Summary

Provide editable Spaces, organizational Folders and Lists containing automatically captured terminal threads.
Connect each List to an optional working folder on this Mac and make future launch destinations explicit.
Add discoverable navigation menus, direct table sorting and persistent column ordering.

### Key Decisions

- **Linked organization rather than a filesystem mirror** (session-settled: user-approved — chosen over mirroring Desktop directories: organizing work must not move repositories). Governs R1, R3, R4, R6.
- **One visible task across an intentional continuation** (session-settled: user-approved — chosen over duplicate active tasks: changing execution context must preserve the user's work item). Governs R7, R8.
- **ClickUp screenshots are the visual authority** (session-settled: user-directed — chosen over a separate work-queue form: the user wants the familiar hierarchy and task list). Governs R1, R2, R10.

### Requirements

**Navigation and working folders**

- R1. Show expandable Spaces containing optional organizational Folders and Lists, retaining existing projects and their task metadata during migration.
- R2. Every Space, Folder and List has a visible three-dot menu with appropriate rename, move and creation actions.
- R3. Lists may link an existing local folder, create a new working folder at a displayed user-chosen location, or remain unlinked for planning.
- R4. Organizational renames and moves never rename, move or delete repository files.
- R5. Unlinked or missing working folders remain reviewable but cannot launch an agent until a valid destination is chosen.

**Moving and continuing work**

- R6. Dropping a task onto a List changes its membership and next-launch destination with an Undo action, without affecting a running terminal.
- R7. Same-provider continuation in the original working folder retains the existing native-resume behavior; a changed folder or provider is labeled as a context-carrying continuation.
- R8. An intentional continuation stays associated with the original visible task while preserving access to its earlier conversations.
- R9. Launch controls show the destination and permission choice; skipping permissions is not represented as filesystem confinement.
- R10. Every independently started terminal conversation is automatically captured without manual enrollment.

**Table and lifecycle**

- R11. Users can reorder data columns horizontally and sort by name, due date, last update, priority, terminal state and agent, with settings retained per saved view.
- R12. Row ordering, sidebar movement and column movement have non-drag menu alternatives.
- R13. Remove the standalone Done view and retain completed/archived work through explicit filters so removal from the active list is recoverable.
- R14. Retain readable conversations, project descriptions, Today filtering, priority grouping, dirty-edit protection and truthful failure states.

### Key Flows

- F1. **Covers R1-R5.** Create a Space, add an organizational Folder, create a List, then choose an existing working folder or leave the List unlinked.
- F2. **Covers R6-R9.** Move a task from Team Operations to YogaNidra, inspect the new destination, then continue in Claude or Codex; retain the source conversation and one active work item.
- F3. **Covers R11-R14.** Open Today, filter and group the rows, sort by priority or agent, rearrange columns, save the view and restore it after refresh.

The sidebar uses distinct Space, Folder and List icons, indentation guides, counts and a selected-row highlight. The main area has one compact view toolbar, a readable table, and a detail reader opened by the task title.

### Acceptance Examples

- AE1. **Covers R1-R4.** Existing aliases, descriptions, due dates, ordering and folder membership survive migration; creating an empty List does not require a terminal session.
- AE2. **Covers R3-R5.** Creating a working folder refuses an existing target instead of overwriting it; a failed creation leaves a truthful, recoverable List state.
- AE3. **Covers R6-R8.** After a task is moved and a new continuation reports its session start, the active table still has one task and the reader exposes both conversations.
- AE4. **Covers R6-R9.** Moving a running task does not interrupt it; a stale launch request for a previous destination fails instead of starting in the wrong folder.
- AE5. **Covers R7-R9.** Missing source history prevents context handoff without deleting, hiding or falsely marking the original task as continued.
- AE6. **Covers R10.** A normal terminal start without an intentional-continuation association creates its own automatically captured task.
- AE7. **Covers R11-R14.** Sorting, column movement and view refresh preserve unsaved edits; closed tasks remain recoverable through filters.

### Scope Boundaries

Deferred: cross-Mac synchronization, hosted Mission Control and exact native cross-directory resume certification.
Excluded: automatic Desktop mirroring, physical repository relocation, transcript deletion, automatic Git initialization, enterprise permissions, assignees, dependencies and recurring tasks.

### Sources

- `claude_browse/board/work_items.py`: current automatic enrollment, sparse user overlay and repository-derived grouping.
- `claude_browse/board/commands.py`: original-directory direct launch and safe terminal command construction.
- `claude_browse/browse.py`: existing native-resume versus relocated-handoff distinction.
- `docs/2026-09-05-clickup-polish-verification.md`: prior pass evidence and outstanding browser verification.
