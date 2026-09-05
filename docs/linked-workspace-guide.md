# Linked Workspace Guide

Open Agent Board at [http://127.0.0.1:51444/](http://127.0.0.1:51444/).

## Start with what is open now

**Open terminals on this Mac** is the default work view. It shows verified open Claude/Codex conversations, including idle ones, independently of whether you marked their work complete. Closing a terminal never archives its task.

Use **All threads** for older conversations or **Today** for due work. The work-status filter distinguishes To do, Completed, and Archived. Presence is separate: Open, Closed, or Unknown. Unknown means the board could not verify that session on this Mac; it does not mean the terminal closed.

Already-open conversations missed by older hooks are captured automatically. Sidebar counts show **open / total** threads. Lists start in **Open first** order; choose **Manual order** or drag a list to keep your own sibling order. Drag the sidebar edge to resize it.

## Organize work

The sidebar is a hierarchy:

- A **Space** is the top-level area for a body of work.
- A **Folder** is an organizational grouping inside a Space.
- A **List** holds tasks and can have a local working folder.
- A **task** is the visible work item for a terminal conversation or thread.

Use the visible three-dot (`...`) menu beside a Space, Folder, or List to rename it, move it, or create child items. A Space can create Folders and Lists. A Folder can create Lists.

This organization is separate from your filesystem. Renaming or moving a Space, Folder, List, or task does not mirror your Desktop and does not move, rename, or delete repositories.

## Set a List's working folder

Open a List's `...` menu and choose one of these options:

- **Link existing folder**: enter the explicit absolute path of an existing local folder.
- **Create working folder...**: enter the explicit absolute path for a new folder. Its parent must already exist; Agent Board will not overwrite an existing path.
- **Keep unlinked (plan first)**: retain the List for planning without a local launch destination.

An unlinked or missing folder does not hide the List or its tasks. It prevents new launches until you link a valid folder on this Mac.

## Move a task

Drag a task onto another List. This changes the task's List membership and its **next launch destination**. It does not interrupt or relocate a terminal that is already running. A short-lived **Undo** action appears after a move.

The task reader shows the next launch destination. Selecting the task title opens the reader; selecting a List opens its description panel.

## Continue work

Task and List launch buttons show the destination and available agent.

The work table keeps its controls in the row: choose **Task status** directly, set **Due date** directly, and use the checkmark to toggle a task between **To do** and **Done**. The two agent buttons adapt to the task: **Continue** when its compatible conversation can be resumed, **Restart** when that continuation will launch a new terminal after its old terminal closed, and **Start** when a new conversation is required.

For an open conversation with the same provider, Continue first proves the live process and its Terminal TTY again, then selects that exact Terminal tab and brings it forward. If the session or tab changed between the board refresh and click, Agent Board does not guess: it safely starts the requested continuation in a new Terminal instead.

- **Resume** is used for the same provider in the original working folder.
- **Continue in Claude/CodeX** is a handoff when the provider or working folder changes. It starts with the task's context rather than claiming a native resume in the new location.

For a handoff, the original raw transcript must still be available on this Mac. If it is not, the task and existing history remain saved, but the handoff is blocked.

**Start fresh** offers Claude or Codex in the linked working folder even when the original transcript is missing. This starts without the old conversation's context. The new conversation attaches to the same task, while its earlier history links remain available.

The **Full access (skip permissions)** choice controls the agent's permission prompt behavior. It is not filesystem confinement: the List's working folder is the chosen launch destination, not a security sandbox.

New terminal conversations started independently are captured as their own tasks. An intentional continuation keeps one visible task and retains its older conversations. In the task reader, use the **Conversation** selector to open an earlier or current conversation.

## Work table and views

Use the toolbar to group, sort, search, and filter tasks. You can sort by name, due date, last update, priority, terminal state, or agent.

Click a colored priority flag to choose Urgent (red), High (amber), Normal (blue), or Low (gray). The chevron beside each group collapses or expands it.

Click a task title to read it, or its pencil to rename it. Enter saves and Escape cancels. The saved name also appears in the Agent Board terminal status line on its next update; existing operating-system tab titles and original provider transcripts are not rewritten.

Drag column headers to change their order, or use their left/right arrows. Drag a column's resize edge to adjust its width; focused resize controls also accept keyboard arrows. Task rows and sidebar items offer **Move up** and **Move down** alternatives to drag-and-drop.

Filters include agent, priority, presence, last reported terminal state, due date, and last update (24 hours, 7 days, or 30 days). Select **Last update** with descending order to see newest activity first.

Save a configured view with the **+** beside **Saved views** in the sidebar. Saved views retain their filters, grouping, sorting, and table layout in this browser. Column layout and collapsed groups are view-specific; sidebar width and list-order preference apply across the workspace.

Completed and archived work remain accessible through the work-status filter. **Thread History** stays available as a secondary, broader transcript search; ordinary reviewing and continuation do not require switching away from the work view.

## Current boundaries

Linked workspaces are local to this Mac. Cross-Mac synchronization, hosted Mission Control, and archive workflows are deferred. Install the board hook on each Mac where you want terminal conversations captured.

This guide describes the current local workflow. It does not claim full end-to-end certification or production readiness.
