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

No network. No accounts. No API calls. It reads local session history from
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
claude-browse --no-canonicalize   # accepted for compatibility; canonicalization still happens at index time
```

While the TUI is up:

| Key              | What it does                                                     |
| ---------------- | ---------------------------------------------------------------- |
| Type             | Write a short sentence about the thread you want, or use exact names / phrases |
| ↑ ↓              | Move between sessions                                            |
| Shift-↑ Shift-↓  | Scroll the preview pane                                          |
| Enter            | Resume the selected thread in the app you launched (`claude-browse`, `codex-browse`, `gemini-browse`, `copilot-browse`, or `cursor-browse`) in yolo mode |
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
  - `where i was asking nevena about feedback`
  - `last closeout session for musopia`
  - `pokpok brief where we questioned the opportunities`
  - `"runna sca2"` when you know the exact phrase already

- In `claude-browse`, a Claude thread resumes natively in Claude and CodeX or Gemini threads start fresh Claude sessions with imported context.
- In `codex-browse`, a CodeX thread resumes natively in CodeX and Claude or Gemini threads start fresh CodeX sessions with imported context.
- In `gemini-browse`, a Gemini thread resumes natively in Gemini and Claude or CodeX threads start fresh Gemini sessions with imported context.
- In `copilot-browse`, a Copilot thread resumes natively in Copilot and Claude, CodeX, or Gemini threads start fresh Copilot sessions with imported context.
- In `cursor-browse`, Claude, CodeX, and Gemini threads start fresh Cursor sessions with imported context.
- The UI is meant to encourage sentence-style recall, not one- or two-word pecking. You should feel comfortable typing a short description like `where i was asking nevena about feedback`.
- `Enter` is the normal open key again. There is now a short paste guard: if a multiline paste just changed the query, the first immediate Enter is ignored so the picker does not auto-launch the top result. `Ctrl-O` still opens immediately.
- The picker now shows an interpreted-query tip row at the top, for example `Looking for: musopia + closeout` or `Looking for threads about: pokpok`.
- If your query is too vague, the picker tells you to add one anchor like a person, client, brand, or folder instead of pretending the search is confident.
- Result rows now show short why-this-matched tags like `folder match`, `title match`, `opening match`, `mentioned in thread`, `feedback`, `critique`, `closeout`, or `older topic` so you can trust a hit faster before opening preview.
- Descriptive queries are reduced to the most specific anchors under the hood, so `find me the thread where i was asking about nevena feedback` behaves like a thread-recall query, not a hard AND over filler words.
- Descriptive queries now separate anchor terms from intent words, so `last closeout session for musopia` treats `musopia` as the hard anchor and `last / closeout` as ranking signals instead of weighting every word equally.
- Descriptive queries now use local concept cues for things like closeout, feedback, critique, and human-performance review, so the ranker can still prefer the right exchange when the exact wording differs.
- Search now prioritizes the most recent relevant mention of your query, not only the thread's latest unrelated activity.
- The preview pane leads with the last matching exchange when a query matches earlier in the thread, then shows whether the thread later drifted to another topic.
- `Ctrl-T` is the honest cross-provider answer to thread drift: it starts a new session anchored on the matched exchange instead of pretending the tool can rewind the original thread in place.
- `Ctrl-Y` lets you emit the suggested next prompt without launching anything. `Ctrl-B` prints the restart card itself for copy/paste or handoff.
- `Ctrl-H` prints a fuller handoff brief with restart state, reopen intent, and recent turns. `Ctrl-U` prints a shorter status update you can paste into Slack, notes, or a standup.
- Cross-provider open is not a true native resume. It creates a new session seeded from the old thread.
- Cursor is currently a **target-only** built-in provider. It opens everything in Cursor, but this tool does not yet claim to index Cursor-origin CLI sessions.

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
  assistant progress, and a suggested next prompt.
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

## Troubleshooting

**`fzf: command not found`**
Install fzf via your package manager (see Install section above).

**`No sessions found`**
You haven't run `claude`, `codex`, `gemini`, or `copilot` yet — or your sessions are in a
non-standard location. The browsers read `~/.claude/projects/`,
`~/.codex/state_5.sqlite`, `~/.codex/history.jsonl`, `~/.gemini/tmp/`, and
`~/.copilot/session-state/`. If yours live elsewhere, file an issue.

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
`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`. CodeX stores thread metadata
in `~/.codex/state_5.sqlite` and user-turn history in `~/.codex/history.jsonl`.
Gemini stores project-scoped chat JSON under
`~/.gemini/tmp/<project>/chats/session-*.json` plus aliases in
`~/.gemini/projects.json`. Copilot stores each session in
`~/.copilot/session-state/<session-id>/` with an `events.jsonl` transcript and
`workspace.yaml` metadata. The browsers normalize all four into one local
SQLite index, then hand that to fzf. When you pick a thread, the tool `cd`s
back to the original cwd, rebuilds a restart card from the local transcript
plus current repo state, and then either launches the native resume command
for the target app or creates a Markdown import brief and starts a fresh
cross-provider handoff session.

No data leaves your machine. No telemetry. No API calls. The whole thing is
~500 lines of stdlib Python.

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

This is the free, local, single-machine tool. The paid companion products
(cross-device sync + mobile/web browsing + AI search across sessions) are
tracked in [ROADMAP.md](ROADMAP.md). If you want to know when they ship,
star the repo or open a discussion — a waitlist will go up close to launch.
