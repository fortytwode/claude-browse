# Agent Board desktop app

The native macOS app is built from `claude_browse/board/macos/AgentBoardDesktop.swift` and installed at `~/Applications/Agent Board.app`.

Build or update it with:

```sh
python scripts/install_desktop.py
```

Check whether the installed app matches the current source with:

```sh
python scripts/install_desktop.py --check
```

Build and open it with `python scripts/install_desktop.py --open`.

The app provides a menu bar control to show or hide the board, pin its window, reload the page, open the board in a browser, or quit. Its window opens small in the upper-right by default. The desktop app reuses the existing Agent Board interface and runs against the backend in this repository. It does not configure login auto-start.

The installer compiles a native Swift binary without third-party dependencies or frameworks, packages it as an app bundle, and signs it with a stable bundle identifier. The bundle records absolute paths for the Python interpreter and repository so the app can connect to the local backend. It prefers `.venv/bin/python` when that interpreter can import `claude_browse.web`.


The compact panel shows a scrollable list of original thread names and runtime
status. Click a name to focus that thread's existing terminal. Each row also has Claude
and CodeX buttons: choose either to continue the thread in that agent using
the board's existing conversation handoff. If
that terminal has closed, the same thread is resumed with normal permissions;
it does not send a new prompt. macOS may ask you to allow Terminal automation
on the first use. The menu also has **Enable Terminal Access…** to request it explicitly. Search and Open terminals / All threads / Today remain in
the panel. Use the pencil beside a thread name to rename it; Enter saves and Escape cancels. **Open in Browser** remains available for full editing.
Closing the window hides it. The menu-bar icon shows it again; Quit stops its
private backend. Restart Connection recovers a failed backend. The local port
is saved in `~/Library/Application Support/Agent Board/desktop-port`, keeping
saved browser settings available across restarts. If that port is occupied,
the app reports a connection failure rather than attaching to another server.
Logs are in `~/Library/Logs/Agent Board/desktop.log`.

After updating a running app, quit and reopen it to use the new executable.


Verified locally: native build and signature, app install/current check, clean
backend shutdown, stable origin on relaunch, live thread list, scrolling, and
click-to-focus an existing Claude terminal. The affected web suites passed
(125 tests), the UI logic suite passed (61 tests), and the final presence,
discovery, and terminal-focus suites passed (50 tests). Ruff and diff checks
passed. Existing unrelated checkout changes were preserved.


The compact panel now includes a Filters menu with provider, priority, runtime
state, terminal presence, due date, activity date, and sorting. Open terminals,
All threads, and Today default to newest conversation activity first. This uses
provider timestamps and hook activity rather than transcript file modification, task-renaming
or ordering timestamps. The real repo name appears beneath each title; hover
it to see the repo root. The full browser board retains its existing settings.


Missing-history audit: exact-provider/session fallback resolves 308 of 907
saved Claude rows on this Mac. The Anna, Friars, and Chughtai examples have no
remaining local transcript in the provider folders or index; their saved task
entries remain. These rows display History missing and cannot transfer context
until the source history is restored. Native-provider resume remains available
where the provider can attempt it. No empty conversation is presented as a
successful handoff.

Follow-up validation: 89 affected Python tests and 67 UI logic tests passed;
Ruff and diff checks passed. The compact filter was exercised live (CodeX only,
then Clear filters), latest-first ordering and repo labels were checked, and
all three scopes were opened. Deeper terminal audit found that the verified
rows are internally consistent but do not yet cover every fork/daemon-backed
terminal process; see the current implementation verification before treating
that count as a census of all open agent windows.


The terminal census now includes verified live provider processes even when
current conversation identity cannot be proved. The November 3 audit found
18 distinct agent-terminal TTYs: 10 identified conversations and 8 unidentified
terminals, including both YogaNidra windows. Process rows show the actual repo
and TTY, and can focus that verified terminal. They cannot be renamed or handed
to another provider until the conversation is identified. Source IDs in
`codex fork` arguments are not treated as the active fork. Loaded daemon threads
and Unix socket peers do not currently supply the missing client/thread mapping.

Renaming a known thread saves the Agent Board name, updates provider naming
metadata, and updates the exact verified Terminal tab's persistent custom
name. CodeX uses its native `thread/name/set` RPC and reads the result back;
Claude appends a native custom-title metadata record without rewriting history.
An already-running Claude process may retain its old internal title until
reload, separately from the Terminal tab title. Failures are reported after
the board name is saved. No keystrokes or prompts are sent to a running agent.
Provider rename tests use isolated fixtures; no real user session was renamed
for testing. Pencil entry and Escape cancellation were verified in the app.

Final verification: 182 affected Python tests passed (including 48 naming and
Terminal tests), plus 70 UI logic tests. The newer transcript-mtime regression
also passed. Ruff and diff checks passed. The running desktop backend was
restarted and both YogaNidra rows and pencil controls were read back live.
Exact identity for existing daemon-backed windows remains blocked on the
provider's missing client/thread association. `/status` exposes the current
Session UUID without restarting; use Session, not Forked from, with a
user-confirmed TTY pairing.

Automatic identification for new local sessions: Agent Board and codex-browse
now start local CodeX with its supported `--no-daemon` option. The installed
zsh helper does the same for interactive `codex`, `codex resume`, and `codex fork`
in newly opened local shells. Explicit remote connections, SSH behavior, and
noninteractive commands retain their existing behavior. This keeps each local
worker attached to its terminal process rather than the shared daemon; those
sessions are not shared-daemon background workers. Existing running sessions
are not stopped or migrated. Already-running shells retain their previously
loaded functions until their normal reload.

The installed SessionStart and UserPromptSubmit hooks also record a local
provider PID, process-start clock, TTY, and validated user-session identity.
The detector rechecks that evidence and the open transcript writer before using
it. Stop/background events and subagent hooks cannot select a thread; shared
daemon hooks without a terminal ancestor cannot create a binding. This removes
the need to copy session IDs for newly started local sessions. Existing
shared-daemon windows remain visible and are not automatically migrated.

Selection limitation: hooks prove the last foreground interaction, not a
silent cached-thread UI switch. Multiple loaded user writers therefore remain
unverified even with a binding. Normal new/resume/fork terminal launches with
one user writer are automatically identifiable; no `/status` copying is needed
for that path. Launch/shell checks passed (174 tests); detector/hook checks
passed (204 tests). Ruff and diff checks passed. The desktop backend was reloaded.
