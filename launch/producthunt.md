# ProductHunt launch

ProductHunt is less critical for a pure CLI tool — the audience skews
consumer-product — but it still drives long-tail discovery and lives
forever on search. Post only after HN + Reddit have run.

---

## Fields

**Name:**
> claude-browse

**Tagline (60 chars max):**
> Find & resume past Claude Code sessions from the terminal

**Description (short):**
> An fzf-powered session picker for Claude Code with a preview pane,
> fuzzy search across every session you've ever run, and one-key resume
> in the original working directory. Free, open-source, no accounts.

**Topics:**
- Developer Tools
- Artificial Intelligence
- Open Source
- Terminal
- Productivity

**Website / main link:**
> https://rocketshiphq.com/claude-browse/

**GitHub:**
> https://github.com/fortytwode/claude-browse

**Gallery assets needed:**
1. Hero image (1270×760) — terminal screenshot with the fzf picker open,
   dark theme, brand colors visible.
2. Demo GIF — same one from the README / landing page.
3. 2–3 additional screenshots showing: preview pane, search typing, resume
   flow. Mock them if needed.

---

## First comment (maker comment)

```
Hey PH! 👋

I built claude-browse because my ~/.claude/projects/ folder got out of
hand — I had dozens of Claude Code sessions across ~20 project folders
and `claude --resume` just wasn't cutting it.

What claude-browse does that the built-in doesn't:

→ Fuzzy search across EVERY past session (not just recent ones in your
  current cwd)
→ Preview pane showing the last 20 messages — so you can tell "oh yeah,
  THAT thread" before hitting Enter
→ Resume in the original working directory automatically
→ Cross-machine friendly — if you sync sessions between Mac and Linux, it
  normalizes paths so the same project doesn't appear twice

It's stdlib Python + fzf. No accounts, no telemetry, no network calls.
MIT licensed.

Roadmap: two separate future products — claude-sync (encrypted
cross-device sync, so your sessions follow you) and claude-browse-web
(mobile-friendly browser with AI search across all your sessions). Those
will eventually be the paid side; claude-browse itself stays free forever.

Would love feedback, especially from anyone else living in the Claude
Code CLI.
```

---

## Tips

- Launch early in the week (Mon–Wed) for max daily engagement
- Launch at 12:01 AM Pacific (00:01) — this gives you the full 24-hour
  window on PH's daily ranking
- Line up 2–3 friends to upvote + comment in the first 4 hours (PH's
  ranking weights early velocity heavily)
- Respond to every maker question in the first day
- Make sure the hero image works as a thumbnail at 256×256 — details
  inside the terminal get lost at that size, so text labels matter more
  than typography
