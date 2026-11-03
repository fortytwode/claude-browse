# Agent Board desktop

A compact macOS menu-bar app for the existing local Agent Board. It shows a
scrollable thread list with repo names, status, search, filters, and Claude and
CodeX actions. Open terminals, All threads, and Today default to latest activity.

Each identified thread shows its last conversation activity, a star, and a snooze
clock. Stars move threads above ordinary entries. Snooze offers 15 minutes,
one hour, three hours, tomorrow at 9am, or a local date and time.

Future snoozes move to the bottom. If their terminal closes, they remain in
All threads until the reminder is due, then return to Open terminals and Today
with a **Reminder due** label and their actual terminal status. This does not
launch a terminal. Due reminders appear above starred threads until dismissed
or snoozed again. Preferences survive app restarts; reminders refresh with the
board while it is running and appear on reopening if their time has passed.
Completed and archived tasks do not resurface as reminders.

## Install and run

From a checkout with the Python package available:

```sh
python3 scripts/install_desktop.py --open
python3 scripts/install_desktop.py --check
```

The installer builds and locally signs `~/Applications/Agent Board.app`.
It records the checkout and Python interpreter in the app bundle, so keep that
checkout in place. Updating a running native executable requires quitting and
reopening the app. Backend-only changes can use **Restart Connection**.

The app starts a private loopback server. Its port is saved in
`~/Library/Application Support/Agent Board/desktop-port`; logs are written to
`~/Library/Logs/Agent Board/desktop.log`. A busy saved port produces an error
rather than attaching to another process. Quit stops the private server;
closing the window only hides it. The menu-bar icon restores it. The menu also
provides pinning, Spaces, reload, Terminal access, and Open in Browser.

## Opening and renaming sessions

Click a thread name or its current provider button to focus its verified
Terminal tab. A different provider uses the existing conversation handoff.
Closed sessions can be resumed with normal permissions. Terminal automation
may require macOS consent; **Enable Terminal Access…** requests it.

The pencil opens an inline name editor: Enter saves; Escape cancels. A rename
updates the board, provider naming metadata, and the verified Terminal tab's
custom title. CodeX uses its native naming API and reads the result back.
Claude appends a native title record without rewriting history; a running
Claude process can retain its old internal title until reload. Partial
synchronization failures are reported without discarding the saved board name.

Missing local history is labeled **History missing**. Those entries cannot
transfer context to another provider; saved task entries remain intact.

## Automatic terminal identification

New local Agent Board and codex-browse sessions use CodeX's `--no-daemon`
option. Each worker stays attached to its terminal rather than joining the
shared background server. Existing running sessions are not migrated.

For direct `codex` commands, source `shell/codex-inline.zsh` from `.zshrc`.
New local interactive shells then apply the same behavior for new, resumed,
and forked sessions. Explicit remote connections, noninteractive commands, and
existing SSH behavior are preserved. Already-open shells retain the functions
they previously loaded.

Session-start and user-prompt hooks record validated session, process-start,
and TTY evidence. Detection checks the live process and transcript writer;
background events and subagents cannot select a thread. Hooks record the last
foreground interaction, not silent UI selection. Multiple cached user writers
therefore remain uncertain even when a previous interaction was recorded.

Unidentified live terminals remain visible with their repo and TTY. They can
be focused after fresh process verification, but cannot be renamed or handed
off until the conversation is identified. Fork-source command arguments are
never treated as the current conversation ID.

## Verification

The desktop UI was exercised locally for scrolling, filters, scope changes,
repo labels, latest-first sorting, and pencil editing/cancellation. Provider
renaming is tested with isolated metadata and API fixtures; real user threads
are not renamed as test data. Automated coverage includes stale process IDs,
subagents, ambiguous writers, missing history, HTTP mutation guards, backend
shutdown, shell arguments, and rename synchronization failures.
