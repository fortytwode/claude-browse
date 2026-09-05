# Linked Workspace Guide

Open Agent Board at [http://127.0.0.1:51444/](http://127.0.0.1:51444/).

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

Drag a task onto another List, or use the task's `...` menu and choose **Move to**. This changes the task's List membership and its **next launch destination**. It does not interrupt or relocate a terminal that is already running. A short-lived **Undo** action appears after a move.

The task reader shows the next launch destination. Selecting the task title opens the reader; selecting a List opens its description panel.

## Continue work

Task and List launch buttons show the destination and available agent.

- **Resume** is used for the same provider in the original working folder.
- **Continue in Claude/CodeX** is a handoff when the provider or working folder changes. It starts with the task's context rather than claiming a native resume in the new location.

For a handoff, the original raw transcript must still be available on this Mac. If it is not, the task and existing history remain saved, but the handoff is blocked.

The **Full access (skip permissions)** choice controls the agent's permission prompt behavior. It is not filesystem confinement: the List's working folder is the chosen launch destination, not a security sandbox.

New terminal conversations started independently are captured as their own tasks. An intentional continuation keeps one visible task and retains its older conversations. In the task reader, use the **Conversation** selector to open an earlier or current conversation.

## Work table and views

Use the toolbar to group, sort, search, and filter tasks. You can sort by name, due date, last update, priority, terminal state, or agent.

Drag column headers to change their order. Use the column-header `...` menu as a non-drag alternative. Task rows and sidebar items also offer **Move up** and **Move down** alternatives to drag-and-drop.

Save a configured view with the **+** beside **Saved views** in the sidebar. Saved views retain their filters, grouping, sorting, and column order in this browser.

Closed work is recoverable through the **Completed** and **Archived** status filters. These replace the older standalone Done navigation.

## Current boundaries

Linked workspaces are local to this Mac. Cross-Mac synchronization, hosted Mission Control, and archive workflows are deferred. Install the board hook on each Mac where you want terminal conversations captured.

This guide describes the current local workflow. It does not claim full end-to-end certification or production readiness.
