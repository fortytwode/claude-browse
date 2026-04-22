# Hacker News — "Show HN" submission

**Title** (80-char limit, keep under 70 for readability):
> Show HN: claude-browse – fzf picker for past Claude Code sessions

**URL:**
> https://github.com/fortytwode/claude-browse

**Submission body (optional, often skipped for Show HN with a URL):**
> Leave blank. HN prefers the URL to speak for itself.

---

## First comment (post immediately after submitting)

Post this as the **first comment** from the submitter account. HN readers read
the first comment nearly as often as the submission itself.

```
I kept losing track of Claude Code sessions across ~15 different project
folders. `claude --resume` pops up a short list of recent sessions, but it
doesn't fuzzy-search and it doesn't preview, so I'd pick the wrong one or
give up and start over.

claude-browse is ~500 lines of stdlib Python over fzf that reads
~/.claude/projects/ and gives you a proper picker: type any word from any
past conversation, preview the last 20 messages, Enter to resume in the
original cwd.

A few things I ended up caring about that might be non-obvious:

- Latest messages in the preview, not earliest. You want to remember where
  the thread ended up, not where it started.

- Yolo-by-default (Enter → --dangerously-skip-permissions). Ctrl-S opts into
  safe mode. If you're already running past sessions you've reviewed, the
  extra permission prompts are noise.

- Path canonicalization for Mac ↔ Linux sync. If you use Syncthing or similar
  to share ~/.claude/projects between a laptop and a VM, Mac's /Users/<name>
  and Linux's /home/<name> would otherwise make every project appear twice.

No network, no telemetry, no accounts. Pure local file reads. MIT.

Roadmap (separate repos, not built yet): claude-sync for encrypted
cross-device session sync, and claude-browse-web for a mobile-friendly view.
Happy to talk about the paid-product-on-free-wedge model if anyone's curious.
```

---

## Follow-up comment templates

When people ask "why not just use `claude --resume`?":

```
`claude --resume` is great for the last 5 sessions in your current cwd. This
is the "I have sessions from 6 months and 20 projects and I remember one
phrase" use case. Different tool, not a replacement.
```

When people ask about cross-device:

```
Good question. This tool is strictly local-read-only; that's why there's no
sync built in. The plan is a separate tool, `claude-sync`, that wraps
Syncthing (or rclone/git/a managed backend as optional alternatives) for
people who want their sessions to follow them across machines. Deliberately
separate so you can adopt one without the other.
```

When people ask about privacy / what ends up in sessions:

```
Very fair — session files can contain API keys, pasted credentials, client
data. That's why claude-browse stays purely local; no content leaves your
machine. For the future sync tool, redaction-before-upload will be opt-in,
and the managed cloud option will be end-to-end encrypted. For now, if
you're worried about what's in there, `jq` over the jsonl is your friend.
```

---

## Best time to post

- **Day:** Tuesday – Thursday
- **Time:** 7–9 AM Pacific (hits US morning + EU afternoon + Asia evening)
- **Avoid:** Mondays (weekend backlog crowds you out), Fridays after noon,
  any day a major tech company is doing a product launch.

## Success metrics

- 50+ points in first 4 hours → front page, expect ~5k–20k visits that day
- 15–30 points → deep page 2, ~500–2k visits
- Under 10 points → "show and forget"; don't resubmit for a few weeks

## Related past Show HNs for reference

- Any fzf-adjacent tool does well on HN
- Terminal-UI tools are a perennial favorite
- AI-tooling-adjacent is trending in 2026 — lean into that in the
  conversation, not the title
