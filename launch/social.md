# X / LinkedIn threads

Personal-network posts are softer than HN/PH but often drive higher-intent
traffic (people who know you). Post these once the HN submission is live so
you can link to the discussion.

---

## X (Twitter) thread

**Tweet 1 (hook):**
```
Shipped claude-browse — an fzf-powered session picker for Claude Code.

Find and resume any past conversation across every project folder you've
ever used, with a preview pane so you pick the right one.

~500 lines of Python. MIT. No accounts.

https://github.com/fortytwode/claude-browse
```

Attach the demo GIF. GIFs dramatically outperform static images on X for
this kind of content.

**Tweet 2 (why):**
```
Why? My ~/.claude/projects/ folder got out of control. `claude --resume`
gives you the last handful of sessions in the current directory.

I had 20 project folders × weeks of sessions. I'd spend 30 seconds
hunting for the right one every time.

claude-browse: type any word from any past thread → find it in 1 sec.
```

**Tweet 3 (cross-machine feature):**
```
Quietly cool detail: it canonicalizes Mac (/Users/...) and Linux
(/home/...) paths.

If you sync ~/.claude/projects/ between machines (I use Syncthing), the
same project would otherwise show up twice — once per machine's home
layout. claude-browse normalizes both sides automatically.
```

**Tweet 4 (roadmap + CTA):**
```
Next (separate tools, not built yet):

→ claude-sync: encrypted cross-device session sync
→ claude-browse-web: mobile-friendly view + AI search across all your
  sessions

Star the repo if that sounds interesting; I'll announce when they land.

https://github.com/fortytwode/claude-browse
```

---

## LinkedIn post (single, long-form)

LinkedIn rewards longer, narrative posts. One post, 1200–1500 chars, no
images in the body (algorithm punishes them right now — use the link
preview).

```
Shipped a small open-source tool this week that I wish someone else had
built: claude-browse.

If you use Claude Code from the terminal and have more than a handful of
projects, you've felt this — `claude --resume` only shows a short recent
list, only for your current directory, and doesn't preview what's in
each session. You end up picking the wrong thread or giving up and
starting fresh.

claude-browse is an fzf-powered picker over ~/.claude/projects/. Type
any word from any past conversation — a function name, a bug you fixed,
a client — and it fuzzy-filters every session you've ever run. The
preview pane shows the last 20 messages so you can tell which thread is
which. Enter resumes in the original working directory.

It's stdlib Python + one external dependency (fzf). No accounts, no
telemetry, nothing leaves your machine. MIT licensed. Works on Mac,
Linux, WSL.

One feature I'm quietly proud of: if you sync sessions between machines
(Mac and a Linux VM, in my case), it canonicalizes the home directory
paths so a single project doesn't show up twice.

Next up — separate tools, because scope creep kills good open-source:
- claude-sync, for encrypted cross-device session sync
- claude-browse-web, for mobile browsing and semantic search across all
  your sessions

claude-browse itself stays free and local forever.

Code + roadmap: https://github.com/fortytwode/claude-browse
Landing page: https://rocketshiphq.com/claude-browse/

Feedback welcome, especially from anyone else living in Claude Code.
```

---

## Tagging

Tag sparingly — too many mentions triggers spam flags. For the X thread:

- **First tweet:** no tags. Let it fly organically first.
- **A few hours in, if traction is real:** reply-tag @fzf_finder (the fzf
  maintainers' account if one exists — verify first), or @AnthropicAI for
  visibility. Don't tag unless you have something substantive to say.
- **LinkedIn:** no tags in the body. If it does well, share it with 3–5
  specific colleagues in DMs who'd genuinely care.

## What not to do

- Don't post the same exact text on X and LinkedIn — the overlap audience
  will see it twice and unfollow
- Don't ask for upvotes. "Would appreciate a star if you try it" is the
  ceiling on direct asks
- Don't reply to criticism defensively. "Fair, here's why I chose this
  path" beats "actually..." every time
