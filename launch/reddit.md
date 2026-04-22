# Reddit submissions

Three subs, three slightly different framings. Don't cross-post the same
text — Reddit mods (and the algorithm) penalize it. Submit within a few
hours of each other, not simultaneously.

---

## r/commandline

**Title:**
> [Tool] claude-browse — fzf picker for past Claude Code sessions with preview pane

**Body:**
```
If you use Claude Code from the CLI and have sessions spread across more
than a handful of project folders, `claude --resume` starts feeling thin —
no fuzzy search, no preview, and only the most recent handful shown.

I wrote a small tool to fix that: claude-browse. It's fzf over
~/.claude/projects/ with a preview pane showing the last 20 messages of
whatever session you're hovering. Enter to resume in the original cwd,
Ctrl-S to resume in safe mode.

Features worth calling out for this sub:

- Pure stdlib Python + one external dependency (fzf). No runtime pip deps.
- Works the same on macOS, Linux, WSL.
- Path canonicalization so Mac /Users/<name> and Linux /home/<name>
  sessions don't duplicate when you sync them between machines.
- Configurable short folder names via env var (handy for monorepos).
- MIT, no telemetry, no network.

GitHub: https://github.com/fortytwode/claude-browse

Happy to take feedback — this is v1.0, things like shell completions and a
--stats mode are on the list but not implemented yet.
```

---

## r/ClaudeAI

**Title:**
> Built a fzf-based session browser for Claude Code — finds and resumes past conversations

**Body:**
```
I've been using Claude Code heavily for the last few months and my
~/.claude/projects/ folder has grown to dozens of sessions across ~20
project folders. The built-in `claude --resume` shows a short recent list
but doesn't let you search across everything, and doesn't show a preview.

claude-browse is a small open-source tool that fixes both. Type any keyword
from any past conversation — a function name, a bug description, a client
name — and it fuzzy-filters every session you've ever run. The preview
pane shows the last 20 user messages so you can tell which thread is which.

GitHub: https://github.com/fortytwode/claude-browse

Use case that matters to me: I'll be on a call, remember "I worked through
this exact thing with Claude 3 weeks ago in some project", and now I can
actually find and resume that conversation in 10 seconds.

Two quality-of-life flags:
- `--here` scopes to the current directory
- `--all` goes past the default 100-session cap (for the "everything
  forever" search)

Feedback and issues welcome. Personal roadmap is a sync-across-devices
companion (`claude-sync`) and a mobile-friendly web view
(`claude-browse-web`) for people who want to see their history from a
phone — but those are separate future projects, claude-browse itself is
done.
```

---

## r/programming

**Title:**
> Show: claude-browse (Python CLI, ~500 LoC) — fuzzy-search and resume past Claude Code sessions

Note: r/programming is stricter about self-promo. Only submit if your HN post
did well; skip otherwise. If you do submit, lean heavily on the *technical*
angle, not the product.

**Body:**
```
Small tool I built, might be useful to anyone here using Claude Code from
the terminal. Source: https://github.com/fortytwode/claude-browse

The interesting bits (for this sub):

1. Path canonicalization for cross-machine session sync. Mac's
   /Users/<name> and Linux's /home/<name> both encode into
   ~/.claude/projects/ as different directory names, so a single project
   synced between two machines shows up twice. I normalize at the
   display/dedup layer and honor a CLAUDE_BROWSE_PATH_ALIASES env var for
   custom mappings (devcontainers, mounted drives).

2. The fzf preview pane is driven by an on-the-fly Python helper script
   written to /tmp and passed via --preview. fzf passes the currently
   hovered line, the helper parses its own mapping table (embedded as a
   JSON literal at generation time) and renders the preview. Keeps startup
   fast and avoids fzf needing to know anything about session files.

3. Single-file scripts that double as a pip-installable package. The
   standalone `claude-browse` and `claude-resume` scripts are thin shims
   that resolve symlinks, insert the repo dir on sys.path, and delegate to
   the `claude_browse` package. `pyproject.toml` registers entry points so
   `pip install` users get the same commands without needing the shims.

MIT, stdlib-only at runtime (fzf is external), 27 unit tests, CI across
Python 3.9–3.13 on both Mac and Linux. v1.0 just shipped.

Happy to answer questions about any of it.
```

---

## Timing

- Post all three within a 2–4 hour window
- Best day: Tuesday or Wednesday
- Best time: 8–10am Pacific (overlaps US working hours + early evening EU)
- Avoid weekends for these subs — less engagement
