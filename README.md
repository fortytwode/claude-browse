# claude-browse

**Find and reopen past Claude Code and CodeX sessions from the terminal.**
Interactive fzf browser with a preview pane, full-text search across folders
and first messages, provider-aware native resume, plus paired browsers that
open everything in Claude or everything in CodeX by default.

<!-- Replace with a real asciinema/terminalizer GIF before launch -->
<p align="center">
  <em>[demo GIF goes here — 10–15s: open, filter, preview, resume]</em>
</p>

```text
claude-browse

Sessions >
  45m ago  team-ops   22msg  finalize pre-flight smoke tests  ###abc…
  3h ago   claude-br  7msg   roadmap for shipping v1          ###def…
  Apr 19   sales      14msg  draft proposal for acme co       ###ghi…
  Apr 17   web        3msg   why is signup failing on safari  ###jkl…
  ...
```

No network. No accounts. No API calls. It reads local session history from
`~/.claude/projects/` and `~/.codex/`, then gives you a fast way to find and
reopen conversations. That's it.

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

`claude-browse` and `codex-browse` use [fzf](https://github.com/junegunn/fzf) for the
interactive UI. Install it once via your system package manager:

```bash
brew install fzf        # macOS
sudo apt install fzf    # Debian / Ubuntu
sudo dnf install fzf    # Fedora / RHEL
sudo pacman -S fzf      # Arch
sudo apk add fzf        # Alpine
```

`claude-resume` is a plain terminal prompt — it works without fzf.

### Requirements

- Python 3.9+
- fzf (for `claude-browse` and `codex-browse`)
- Claude Code and/or CodeX (otherwise there are no sessions to browse)

---

## Usage

### Interactive TUI

```bash
claude-browse               # most recent 100 sessions, opens everything in Claude
codex-browse                # most recent 100 sessions, opens everything in CodeX
claude-browse --all         # every session you've ever run
codex-browse --here         # only sessions started in the current directory
claude-browse --no-canonicalize   # show raw cwds (see "Cross-machine" below)
```

While the TUI is up:

| Key              | What it does                                                     |
| ---------------- | ---------------------------------------------------------------- |
| Type             | Full-text search across Claude + CodeX threads                   |
| ↑ ↓              | Move between sessions                                            |
| Shift-↑ Shift-↓  | Scroll the preview pane                                          |
| Enter            | Open in the app you launched (`claude-browse` or `codex-browse`) in yolo mode |
| Ctrl-S           | Open in that same app in safe mode                                |
| Ctrl-X           | Open in the other app instead, also in yolo mode                  |
| Esc              | Quit                                                             |

Examples:

- In `claude-browse`, a Claude thread resumes natively in Claude and a CodeX thread starts a fresh Claude session with imported context.
- In `codex-browse`, a CodeX thread resumes natively in CodeX and a Claude thread starts a fresh CodeX session with imported context.
- Cross-app open is not a true native resume. It creates a new session seeded from the old thread.

### claude-resume — keyword resume without the TUI

```bash
claude-resume <session-id>             # resume by exact UUID
claude-resume <keyword>                # search sessions, pick, resume
claude-resume --last [N]               # pick from N most recent (default 10)
claude-resume --yolo <keyword>         # resume with skip-permissions
claude-resume --here <keyword>         # only sessions from current dir
claude-resume aditi -- --model sonnet  # example Claude flag when the selected session is a Claude thread
claude-resume taxes -- --model gpt-5   # example CodeX flag when the selected session is a CodeX thread
```

Useful when you remember a keyword from the conversation and don't want to
leave your shell. It routes to the native app automatically: Claude threads
open in Claude, CodeX threads open in CodeX.

---

## Why

Claude Code already has `claude --resume`, and CodeX has `codex resume`, but
both are provider-local pickers. `claude-browse` and `codex-browse` are better
at three things:

- **Fuzzy search across all your sessions, not just the last few.** Type any
  word from any past conversation, any folder name, any relative date —
  find it.
- **Preview before you resume.** See where the conversation ended up (latest
  messages first) so you pick the right thread, not a stale one.
- **Choose the target app up front.** Launch `claude-browse` if you want to
  work in Claude or `codex-browse` if you want to work in CodeX. When the
  source app differs, the browser writes a compact import brief and starts a
  fresh session in the target app instead of pretending cross-vendor native
  resume exists.

If you live in `tmux` and start a lot of Claude Code sessions across
different projects, this is the tool.

---

## Cross-machine setup (Mac ↔ Linux)

If you sync `~/.claude/projects/` between a Mac and a Linux box (Syncthing,
rclone, etc.), session cwds recorded on one machine won't match the other
(`/Users/<name>` vs `/home/<name>`). By default the browsers
**canonicalizes** both to `$HOME`, so the same project shows up once, not
twice. Pass `--no-canonicalize` to see raw paths.

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

---

## Troubleshooting

**`fzf: command not found`**
Install fzf via your package manager (see Install section above).

**`No sessions found`**
You haven't run `claude` or `codex` yet — or your sessions are in a
non-standard location. The browsers read `~/.claude/projects/`,
`~/.codex/state_5.sqlite`, and `~/.codex/history.jsonl`. If yours live
elsewhere, file an issue.

**`Original folder no longer exists`**
The directory you ran that session from has been deleted or moved. You can
still resume with `claude --resume <session-id>` manually from any cwd.

**Resume opens but the session looks empty**
The session file may be in a different encoded-directory than Claude Code
expects for the current cwd. See the cross-machine section for context. A
proper fix is on the roadmap as part of the `claude-sync` companion tool.

---

## How it works

Claude Code writes each session as a JSONL file under
`~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`. CodeX stores thread metadata
in `~/.codex/state_5.sqlite` and user-turn history in `~/.codex/history.jsonl`.
The browsers normalize both into one local SQLite index, then hand that to
fzf. When you pick a thread, the tool `cd`s back to the original cwd and then
either launches the native resume command for the target app or creates a
Markdown import brief and starts a fresh cross-app handoff session.

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
