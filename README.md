# Claude Session Tools

Find and resume past Claude Code conversations from the terminal.

## Tools

**`claude-browse`** - Interactive session browser with preview pane. Type to filter, arrow keys to navigate, Enter to resume. No keywords needed.

**`claude-resume`** - Quick resume by keyword. `claude-resume gravy pass` finds matching sessions, lets you pick one, and resumes it in the original directory.

## Install

```bash
git clone <this-repo>
cd claude-session-tools
./install.sh
```

Requires: Python 3, [fzf](https://github.com/junegunn/fzf) (`brew install fzf`)

## Usage

```bash
# Browse recent sessions visually
claude-browse

# Browse all sessions (not just recent 100)
claude-browse --all

# Only sessions from current folder
claude-browse --here

# Resume with --dangerously-skip-permissions
claude-browse --yolo

# Quick resume by keyword
claude-resume gravy pass
claude-resume --last          # pick from 10 most recent
claude-resume --yolo aditi    # resume with skip permissions
```

## How it works

Claude Code stores all sessions as `.jsonl` files in `~/.claude/projects/`. These tools index those files, extract metadata (date, folder, first message, message count), and present them in a searchable interface via fzf.

No data leaves your machine. No API calls. Pure local file reads.
