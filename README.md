# claude-browse

**Resume software work across Claude Code, CodeX, Gemini, and Copilot from the terminal, then continue it in Claude, CodeX, Gemini, Copilot, or Cursor.**
Interactive fzf browser with a restart-card preview pane, full-text search
across folders and first messages, provider-aware native resume, and
target-app browsers that open everything in Claude, CodeX, Gemini, Copilot,
or Cursor by default.

<!-- Replace with a real asciinema/terminalizer GIF before launch -->
<p align="center">
  <em>[demo GIF goes here — 10–15s: open, filter, preview, resume]</em>
</p>

```text
claude-browse

Find thread where...
  45m ago  team-ops   22msg  finalize pre-flight smoke tests  ###abc…
  3h ago   claude-br  7msg   roadmap for shipping v1          ###def…
  Apr 19   sales      14msg  draft proposal for acme co       ###ghi…
  Apr 17   web        3msg   why is signup failing on safari  ###jkl…
  ...
```

By default: no network, no accounts, no API calls. It reads local session history from
`~/.claude/projects/`, `~/.codex/`, `~/.gemini/tmp/`, and
`~/.copilot/session-state/`, then reconstructs enough task state to help you
keep moving instead of dropping you into a stale transcript. Cursor is
currently a launch target, not a local session source.

---

## Install

### With pip *(recommended once the package is on PyPI)*

```bash
pip install claude-browse
```

### From source

```bash
git clone https://github.com/fortytwode/claude-browse.git
cd claude-browse
./install.sh
```

### External dependency — fzf

`claude-browse`, `codex-browse`, `gemini-browse`, `copilot-browse`, and `cursor-browse` use
[fzf](https://github.com/junegunn/fzf) for the interactive UI. Install it once
via your system package manager:

```bash
brew install fzf        # macOS
sudo apt install fzf    # Debian / Ubuntu
sudo dnf install fzf    # Fedora / RHEL
sudo pacman -S fzf      # Arch
sudo apk add fzf        # Alpine
```

### Requirements

- Python 3.9+
- fzf (for `claude-browse`, `codex-browse`, `gemini-browse`, `copilot-browse`, and `cursor-browse`)
- Claude Code, CodeX, Gemini, GitHub Copilot CLI, and/or Cursor Agent CLI
- Optional experimental providers can be loaded from local Python modules or local provider directories

---

## Usage

### Interactive TUI

```bash
claude-browse               # most recent 100 sessions, opens everything in Claude
codex-browse                # most recent 100 sessions, opens everything in CodeX
gemini-browse               # most recent 100 sessions, opens everything in Gemini
copilot-browse             # most recent 100 sessions, opens everything in Copilot
cursor-browse               # most recent 100 sessions, opens everything in Cursor
claude-browse --all         # every session you've ever run
codex-browse --here         # only sessions started in the current directory
claude-browse --relocate    # force-resume the chosen thread in the current dir, even if it's the thread's own folder
codex-browse --fork         # always branch the chosen thread into a new, diverging one
codex-browse --no-fork      # never branch; attach even if the thread is open elsewhere (may fail)
claude-browse --no-canonicalize   # accepted for compatibility; canonicalization still happens at index time
claude-browse --web         # open a local browser tab to read full past transcripts and scan sessions
```

### Web viewer

```bash
claude-browse --web
```

Opens a local-only browser tab (bound to `127.0.0.1`, no accounts, no outbound
network calls) alongside the usual fzf picker -- not a replacement for it.
Use it when you actually want to *read* a past conversation: a sidebar lists
sessions (current folder first, searchable, "this folder only" toggle) and
selecting one renders the full thread -- opened at the latest exchange,
scroll up for history -- with fenced code blocks and an in-thread search
box. This is prose only (user/assistant text) -- tool calls, file edits,
and command output aren't captured. `--here` scopes the whole server to the
current folder; `--all` widens the sidebar past the default 100 sessions.
Requests from any Host other than `127.0.0.1`/`localhost` are rejected
(DNS-rebinding protection). `Ctrl-C` in the terminal shuts the server down.

#### Scripting the web viewer (JSON API)

The page is backed by three JSON endpoints you can call directly (the
server prints its URL to stderr on startup; the port is OS-assigned):

| Endpoint | Returns |
| -------- | ------- |
| `GET /api/meta` | `{"here_only_forced": bool}` -- whether `--here` scoping is forced server-side |
| `GET /api/sessions?q=<query>&here=1` | `{"sessions": [...]}` -- session list; `q` runs the same ranked search as the fzf picker, `here=1` scopes to the launch folder. Each session carries `session_id`, `provider`, `provider_name`, `folder`, `cwd`, `title`, `first_msg`, `last_msg`, `msg_count`, `timestamp`, `last_timestamp`, and a preformatted `when` |
| `GET /api/session/<sid>` | `{"meta": {...}, "turns": [{"role", "text"}, ...]}` -- the full prose transcript, newlines preserved |

Errors come back as JSON too: `404` (unknown session/route), `403` (foreign
Host header), `500` (unreadable transcript), `503` (search index mid-rebuild
-- retry shortly). For non-HTTP scripting, the same primitives are plain
Python imports: `claude_browse.fts.list_recent` / `sessions_for_cwd` /
`search_ranked` / `get_by_sid`, and
`claude_browse.providers.get_provider(p).transcript_turns(path, sid)`.

While the TUI is up:

| Key              | What it does                                                     |
| ---------------- | ---------------------------------------------------------------- |
| Type             | Write a short sentence about the thread you want, or use exact names / phrases |
| ↑ ↓              | Move between sessions                                            |
| Shift-↑ Shift-↓  | Scroll the preview pane                                          |
| Enter            | Resume the selected thread in the app you launched (`claude-browse`, `codex-browse`, `gemini-browse`, `copilot-browse`, or `cursor-browse`) in yolo mode. If the thread is from a **different folder** than where you launched, it relocates automatically — grafting its context into a fresh session in your current directory instead of yanking you back to the origin folder. |
| Ctrl-O           | Resume immediately, bypassing the multiline-paste safety guard   |
| Ctrl-T           | Re-enter the matched topic in a fresh session in that app        |
| Ctrl-S           | Open in that same app in safe mode                               |
| Ctrl-Y           | Print the suggested next prompt for the selected thread          |
| Ctrl-B           | Print the restart card for the selected thread                   |
| Ctrl-H           | Print a reusable handoff brief                                   |
| Ctrl-U           | Print a concise status update                                    |
| Esc              | Quit                                                             |

Examples:

- Sentence-style query examples:
  - `where i was asking about teammate feedback`
  - `last closeout session for client`
  - `brand brief where we questioned the opportunities`
  - `"runna sca2"` when you know the exact phrase already

- In `claude-browse`, a Claude thread resumes natively in Claude and CodeX or Gemini threads start fresh Claude sessions with imported context.
- In `codex-browse`, a CodeX thread resumes natively in CodeX and Claude or Gemini threads start fresh CodeX sessions with imported context.
- In `gemini-browse`, a Gemini thread resumes natively in Gemini and Claude or CodeX threads start fresh Gemini sessions with imported context.
- In `copilot-browse`, a Copilot thread resumes natively in Copilot and Claude, CodeX, or Gemini threads start fresh Copilot sessions with imported context.
- In `cursor-browse`, Claude, CodeX, and Gemini threads start fresh Cursor sessions with imported context.
- The UI is meant to encourage sentence-style recall, not one- or two-word pecking. You should feel comfortable typing a short description like `where i was asking about teammate feedback`.
- `Enter` is the normal open key again. There is now a short paste guard: if a pasted long or multiline quote just changed the query, the first Enter arms the selection and the second Enter opens it. `Ctrl-O` still opens immediately.
- The picker now shows an interpreted-query tip row at the top, for example `Looking for: client + closeout` or `Looking for threads about: brand`.
- If your query is too vague, the picker tells you to add one anchor like a person, client, brand, or folder instead of pretending the search is confident.
- Result rows now show trust/provenance tags like `primary subject`, `folder match`, `title match`, `opening match`, `mentioned later`, `feedback`, `critique`, `closeout`, or `drifted` so you can tell why a hit surfaced before opening preview.
- Descriptive queries are reduced to the most specific anchors under the hood, so `find me the thread where i was asking about teammate feedback` behaves like a thread-recall query, not a hard AND over filler words.
- Descriptive queries now separate anchor terms from intent words, so `last closeout session for client` treats `client` as the hard anchor and `last / closeout` as ranking signals instead of weighting every word equally.
- Descriptive queries now use local concept cues for things like closeout, feedback, critique, and human-performance review, so the ranker can still prefer the right exchange when the exact wording differs.
- Search now prioritizes the most recent relevant mention of your query, not only the thread's latest unrelated activity.
- The preview pane now starts with a `Why this surfaced` block: match type, match time, match confidence, and best action (`Enter` vs `Ctrl-T`), then shows the last matching exchange and whether the thread later drifted to another topic.
- `Ctrl-T` is the honest cross-provider answer to thread drift: it starts a new session anchored on the matched exchange instead of pretending the tool can rewind the original thread in place.
- `Ctrl-Y` lets you emit the suggested next prompt without launching anything. `Ctrl-B` prints the restart card itself for copy/paste or handoff.
- `Ctrl-H` prints a fuller handoff brief with restart state, reopen intent, and recent turns. `Ctrl-U` prints a shorter status update you can paste into Slack, notes, or a standup.
- Cross-provider open is not a true native resume. It creates a new session seeded from the old thread.
- Cross-folder open auto-relocates: native `claude --resume <id>` only works from a thread's own project folder, so selecting a thread from a different directory now grafts its context into a fresh session in your current directory instead of chdir-ing you back (or failing with "No conversation found" when the origin is gone). Same-folder threads still resume natively. Use `--relocate` to force this even for a thread's own folder.
- Cursor is currently a **target-only** built-in provider. It opens everything in Cursor, but this tool does not yet claim to index Cursor-origin CLI sessions.

---

## Optional Dense Embeddings

The default search stack is local only: exact URL/page ID matching, weighted
FTS, segment windows, and a local TF-IDF-style semantic window index. If you
want paraphrase-level recall, you can opt into dense embeddings while keeping
storage and retrieval local:

```bash
export CLAUDE_BROWSE_DENSE_EMBEDDINGS=1
export OPENAI_API_KEY=...
```

Optional knobs:

```bash
export CLAUDE_BROWSE_EMBEDDING_MODEL=text-embedding-3-small
export CLAUDE_BROWSE_EMBEDDING_DIMENSIONS=256
export CLAUDE_BROWSE_EMBEDDING_BATCH_SIZE=64
export CLAUDE_BROWSE_DENSE_MIN_SCORE=0.25
```

When enabled, `claude-browse` embeds local transcript windows through the
embeddings API and stores the resulting vectors in the same local SQLite
cache. Query embeddings are cached locally too. It does not use a hosted vector
store or File Search. Exact URL/page ID search still runs first.

Privacy/cost boundary: transcript window text and search queries are sent to
the embedding API only when `CLAUDE_BROWSE_DENSE_EMBEDDINGS=1` is set. With
the default `text-embedding-3-small` model, the current local corpus measured
around 10.75M overlapping-window tokens, which is roughly $0.21 to embed at
$0.02 per 1M tokens.

---

## Why

Claude Code already has `claude --resume`, CodeX has `codex resume`, Gemini
has `gemini --resume`, and Copilot has `copilot --resume`, but all four are
provider-local pickers. `claude-browse`, `codex-browse`, `gemini-browse`,
`copilot-browse`, and `cursor-browse` are better at three things:

- **Thread recall across all your sessions, not just the last few.** Type any
  thread description, person, folder, client, or phrase and recall the exact
  old thread you want.
- **Recover work state before you resume.** The preview pane reconstructs the
  current task, topic drift, repo status, last meaningful ask, latest
  assistant progress, suggested next prompt, and a provenance block that says
  why this result surfaced and whether `Enter` or `Ctrl-T` is the better move.
- **Re-enter an earlier topic honestly.** When topic A is buried inside a thread
  that later drifted to B/C/D, the browser can start a fresh session anchored
  on the matched exchange instead of faking a mid-thread native rewind.
- **Choose the target app up front.** Launch `claude-browse` if you want to
  work in Claude, `codex-browse` if you want to work in CodeX, or
  `gemini-browse` if you want to work in Gemini, `copilot-browse` if you want
  to work in Copilot, or `cursor-browse` if you want to work in Cursor. When
  the source app differs,
  the browser writes a compact import brief and starts a fresh session in the
  target app instead of pretending cross-vendor native resume exists.

If you live in `tmux` and start a lot of agent sessions across different
projects, this is the tool.

---

## Cross-machine setup (Mac ↔ Linux)

If you sync `~/.claude/projects/` between a Mac and a Linux box (Syncthing,
rclone, etc.), session cwds recorded on one machine won't match the other
(`/Users/<name>` vs `/home/<name>`). By default the browsers
**canonicalize** both to `$HOME`, so the same project shows up once, not
twice. `--no-canonicalize` is still accepted for compatibility, but it no
longer changes display behavior because canonicalization now happens at index
time.

For custom path aliases (corporate devcontainers, Windows drives, etc.),
set an environment variable:

```bash
export CLAUDE_BROWSE_PATH_ALIASES="/workspaces/repo=$HOME/repo"
# multiple pairs separated by :
export CLAUDE_BROWSE_PATH_ALIASES="/Volumes/Work=$HOME/work:/mnt/c/code=$HOME/code"
```

### Short folder names

If your sessions all live under a monorepo, you can set
`CLAUDE_BROWSE_FOLDER_PREFIXES` to strip common prefixes from the folder
column:

```bash
export CLAUDE_BROWSE_FOLDER_PREFIXES="monorepo/apps/:monorepo/lib/"
```

### Experimental external providers

Built-in providers are still the supported path, but the registry can now load
additional providers from local Python modules via:

```bash
export CLAUDE_BROWSE_PROVIDER_MODULES="my_pkg.my_provider"
```

or from plugin-style directories of `.py` files via:

```bash
export CLAUDE_BROWSE_PROVIDER_DIRS="$HOME/.config/claude-browse/providers"
```

Each external provider module or file must expose:

```python
from claude_browse.providers.base import (
    PROVIDER_API_VERSION as API_V1,
    ProviderSpec,
)

PROVIDER_API_VERSION = API_V1  # optional today, but recommended
PROVIDER = ProviderSpec(...)
```

The contract is intentionally **experimental**:

- It may change between releases
- There is no compatibility promise or marketplace yet
- External providers can be source-capable (index local sessions), target-capable
  (launch target only), or both
- Directory-loaded providers are regular local Python files, not sandboxed plugins

For target-capable external providers, you can use either:

```bash
claude-browse --target my-provider
```

or a thin shim/symlink named `my-provider-browse` that points at
`claude-browse`.

To inspect what loaded successfully without opening fzf:

```bash
claude-browse --list-providers
```

That prints built-in vs external providers, source/target capability, binary
availability, experimental status, and auth state when a provider reports one.

---

## Agent Board (live session status + notifications)

Turns every Claude Code **and Codex** session into a tracked, auto-named
thread with a live state (`working` / `idle` / `needs-input` / `gone` /
`ended`), shown in your statusline, pushed as a native macOS notification
(with sound) on completion or when blocked, and (optionally) mirrored to
Firestore + a private Slack channel so you can see every session across
multiple machines in one place.

Native notification titles are formatted as `[folder] Model state` (for
example `[claude-browse] Codex needs input` or `[team-operations] Opus
done`) so the repo and model are visible even when macOS truncates the
message body.

**Unattended completions (the thing the board is really for).** Every
completed turn marks the session as *finished, not picked up* until you come
back. Duration is not the signal; a 10-second turn that ends with a question
is waiting on you just as much as an hour-long run. Ending the session does
not clear it either (you can always resume the thread, so it stays listed).
Two things clear it: sending the session a new prompt, or acknowledging
explicitly:

```bash
agent-board ack <session-id-prefix | name-substring>
```

`aj` and the Slack board lead with a `⏳ finished, not picked up` section
listing exactly these, oldest first, each with its resume command. If you do
want a length floor, `AGENT_BOARD_UNATTENDED_MIN_TURN_S=<seconds>` sets one
(default 0). The
cross-machine re-surfacing (a Slack ping only once you have NOT come back
for a while, escalating once, then folding into the morning briefing) lives
in the team-operations repo (`team-planning/agent_board/`), reading the
same Firestore docs this board writes.

Because of that, a plain "done" no longer posts a fresh Slack message the
instant a turn ends (it could not tell a run you walked away from apart
from a turn you sat and watched, and on an interactive evening that meant a
dozen-plus alerts from three threads). The local banner still fires, and
`needs-input` is still an immediate Slack post. To restore the immediate
"done" Slack post: `export AGENT_BOARD_IMMEDIATE_DONE_ALERT=1` in the
environment the hooks run in.

**Dedicated notification identity and persistence:** `./install.sh` builds a
small local `~/Applications/Agent Board Notifier.app` with the stable identity
`Agent Board`. That lets macOS grant Agent Board an exception under System
Settings > Focus > your Focus > Allowed Apps > Agent Board Notifier without
also allowing every Script Editor notification. If the Mac has no Swift
compiler, delivery falls back safely to Script Editor and the installer
reports that downgrade.

The banner plays a sound (your System Settings > Sound > Alert sound), but
still auto-dismisses after a few seconds by default -- that timing is a per-app
Notification Center setting. To make it stay until you dismiss it: System
Settings > Notifications > Agent Board > set Alert Style to "Persistent"
instead of "Temporary". Focus cannot be bypassed programmatically: add Agent
Board Notifier to Allowed Apps for each Focus that should permit completion
alerts. The Slack `#agent-status` board is the durable fallback if you miss
the banner entirely -- it never auto-dismisses.

**Setup (one machine):**

```bash
./install.sh
```

This idempotently wires hooks + a statusLine command into
`~/.claude/settings.json` and hooks into `~/.codex/hooks.json` (backing
each up before a change; safe to re-run), symlinks `agent-board`, disables Claude
Code's built-in folderless push notifications so Agent Board is the only
local alert source, and reports:
- whether hooks/statusLine were already wired (skips if so)
- any stale, duplicate, matcher-scoped, or option-drifted Agent Board
  registrations it removed. Foreign hooks and their matcher groups remain
  untouched. Each lifecycle event ends with one dedicated, matcherless Agent
  Board hook.
- the local notification setting (`agentPushNotifEnabled`, kept false to
  avoid duplicate lower-information banners) and how many of your recent
  sessions already have an `ai-title` -- the
  namer only calls Haiku for the rest)
- live Firestore + Slack connectivity (`agent-board sync check`)

To audit without changing anything (exit 1 on any drift):

```bash
python3 scripts/install_agent_board.py --check
```

**Codex.** Codex sends the same core stdin envelope
(`hook_event_name`, `session_id`, `cwd`, `model`) and stores definitions in
`~/.codex/hooks.json` as `{"hooks": {Event: [...]}}`. Command handlers use
the `timeout` field. The installer registers `agent-board hook --provider codex`
on `SessionStart` / `UserPromptSubmit` / `Stop` / `PermissionRequest` /
`Interrupt` / `SessionEnd` (Codex has no `Notification` event;
`PermissionRequest` is its blocked-on-you signal and maps to `needs-input`).
`Interrupt` returns the thread to `idle` and does not record a completion,
because interrupted work did not finish. The provider is stored on
the row, so Codex threads get `codex resume <id>` on every surface, never a
`claude --resume` that cannot open them. `SessionEnd` has Codex's maximum
timeout of 3 seconds; it only commits local state and starts detached,
nonblocking publication. Every event uses one hook: after the local commit,
that hook starts a serialized sync worker, avoiding races between sibling
hook commands.

After a fresh or changed Codex installation, open Codex, run `/hooks`, and
review/trust the Agent Board definitions. Codex skips new or changed hooks
until you do this. The installer and `--check` cannot verify trust; `--check`
validates only hook definitions and the hooks feature configuration. Hooks
are on by default in current Codex releases; if your `~/.codex/config.toml`
sets `[features] hooks = false`, `--check` fails and the installer warns
without editing that file. Codex rows keep their prompt-derived name
(the Haiku namer reads Claude transcripts only, for now). Pass `--no-codex`
to skip.

Add this to your shell rc (not done automatically) for `work <name>`
(tmux attach-or-create) and `aj` (board glance):

```bash
source "/path/to/claude-browse/shell/agent-board.zsh"
```

**Cross-laptop sync (optional):** requires the `board-sync` extra and
Firestore/Slack creds. Without it, the local loop (statusline,
notifications, `aj`) still works fully -- sync just no-ops and logs to
`~/.claude/agent-board/sync.log`.

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[board-sync]"
```

Agent Board automatically uses `.venv/bin/python` on the next hook event; no
hook rewiring or installer re-run is needed.

Firestore uses Application Default Credentials (`gcloud auth
application-default login`). Point it at YOUR project via
`CLAUDE_BROWSE_BOARD_PROJECT`, `CLAUDE_BROWSE_BOARD_DATABASE`, and
`CLAUDE_BROWSE_BOARD_COLLECTION` (defaults preserve existing installs). Slack
needs `SLACK_BOT_TOKEN` -- set in your environment, or in
`~/team-operations/.env` (auto-detected as a fallback, since hooks run
with a minimal inherited environment that usually won't have it exported).

Each session's Firestore doc (`agent_board_sessions/<host>:<session_id>`)
carries `provider`, `folder`, `done_at`, `done_turn_s`, `acked_at` and a
ready-to-paste `resume_command` alongside the live state, and is written
with `merge=True` so fields owned by downstream consumers (the unattended
sweep's `alert_count` / `last_alert_at` / `alert_ts`) survive every push.

**Rolling out to a second laptop:**

1. `git pull` this repo on the second machine.
2. Run `./install.sh` there -- same idempotent wiring, own local `state.db`.
3. Set up `board-sync` + creds the same way if you want that machine's
   sessions on the shared board too.
4. Both machines' sessions appear together, grouped by hostname, in the
   `#agent-status` board and via `agent-board board` (which only reads
   the local machine's `state.db` -- Slack is the cross-machine view).

---

## Troubleshooting

Search diagnostics are written locally to:

```bash
~/.claude/cache/claude-browse-search.log.jsonl
```

Each line records the query, ranker, elapsed time, result count, and top result
metadata/snippets. This is the closest equivalent to a server log for the local
fzf workflow. Set `CLAUDE_BROWSE_LOG=0` to disable it, or
`CLAUDE_BROWSE_LOG_PATH=/path/to/log.jsonl` to move it. The log rotates at
5 MB by default; override with `CLAUDE_BROWSE_LOG_MAX_BYTES`.

**`fzf: command not found`**
Install fzf via your package manager (see Install section above).

**`No sessions found`**
You haven't run `claude`, `codex`, `gemini`, or `copilot` yet — or your sessions are in a
non-standard location. The browsers read `~/.claude/projects/`,
`~/.codex/sessions/`, `~/.codex/state_5.sqlite`, `~/.codex/history.jsonl`,
`~/.gemini/tmp/`, and `~/.copilot/session-state/`. If yours live elsewhere,
file an issue.

**`Original folder no longer exists`**
The directory you ran that session from has been deleted or moved. You can
still resume with the native command (`claude --resume <session-id>`,
`codex resume <session-id>`, `gemini --resume <session-id>`, or
`copilot --resume <session-id>`) manually from any cwd.

**Resume opens but the session looks empty**
The session file may be in a different encoded-directory than Claude Code
expects for the current cwd. See the cross-machine section for context. A
proper fix is on the roadmap as part of the `claude-sync` companion tool.

---

## How it works

Claude Code writes each session as a JSONL file under
`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`. CodeX writes canonical
session JSONL transcripts under `~/.codex/sessions/`, with thread metadata
in `~/.codex/state_5.sqlite` and user-turn history fallback in
`~/.codex/history.jsonl`.
Gemini stores project-scoped chat JSON under
`~/.gemini/tmp/<project>/chats/session-*.json` plus aliases in
`~/.gemini/projects.json`. Copilot stores each session in
`~/.copilot/session-state/<session-id>/` with an `events.jsonl` transcript and
`workspace.yaml` metadata. The browsers normalize all four into one local
SQLite index, then hand that to fzf. Search combines exact identifier lookup
for URLs/page IDs, weighted FTS, segment-window matching, a local
TF-IDF-style semantic window index for natural-language recall, and optional
local dense-vector retrieval when explicitly enabled. When you pick a thread,
the tool `cd`s back to the original cwd, rebuilds a restart card from the
local transcript plus current repo state, and then either launches the native
resume command for the target app or creates a Markdown import brief and starts
a fresh cross-provider handoff session.

Without optional dense embeddings, no data leaves your machine. No telemetry.
No API calls. The core runtime remains stdlib Python.

See [ROADMAP.md](ROADMAP.md) for what's planned, what's out of scope, and
the direction for the paired `claude-sync` and `claude-browse-web` projects.

---

## Contributing

Small, focused PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for how
to run tests and what's in/out of scope.

---

## License

[MIT](LICENSE) — © 2026 Shamanth Rao

---

## Related work and future products

This is the free, local, single-machine tool — including `--web`, which is a
local-only reading surface for this machine's sessions (it binds to
127.0.0.1, serves your own indexed history, and works offline). The paid
companion products (cross-device sync + hosted mobile/web browsing across
all your machines + AI search across sessions) are a different surface:
they follow your sessions across devices without a terminal on each one.
They're tracked in [ROADMAP.md](ROADMAP.md). If you want to know when they
ship, star the repo or open a discussion — a waitlist will go up close to
launch.
