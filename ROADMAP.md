# Roadmap

This document captures where claude-browse is today, what v1.0 requires before a
public launch, and the direction for paid companion products. It's the source
of truth for scope decisions — if something isn't listed here, it isn't
planned.

## Product strategy in one paragraph

**claude-browse is the free wedge for a paid session-sync product.** The CLI
itself stays free, open-source, and local-only forever — that's what earns
adoption and trust. The revenue comes from a hosted backend (working title:
claude-sync + claude-browse-web) that adds encrypted cross-device sync,
mobile-friendly web browsing, and AI-powered search across sessions. Standard
open-core pattern: Tailscale, Raycast, Warp, Supabase all use it.

---

## Current state (April 2026)

- `claude-browse` — fzf TUI over `~/.claude/projects/`. Fuzzy search, preview
  pane, resume in original cwd. Default mode is `--dangerously-skip-permissions`
  (yolo); Ctrl-S opts into safe mode.
- `claude-resume` — keyword-based quick resume without the full TUI.
- `install.sh` — symlinks the scripts into `~/.local/bin/`.
- 4 commits, ~400 LoC of Python, no tests, no CI, no packaging.
- Distribution: `git clone` + `./install.sh`. Mac-first (install.sh recommends
  `brew install fzf` only).

## v1.0 polish checklist

The bar for a credible public launch. Everything below is landing in one release
tagged v1.0.0.

### Packaging & install
- [ ] `pyproject.toml` with entry points so `pip install claude-browse` works
- [ ] Cross-platform `install.sh` (detect Linux, suggest apt/dnf/pacman for fzf)
- [ ] Publish to PyPI under `claude-browse` (reserve the name early)
- [ ] Homebrew tap: `fortytwode/tap/claude-browse` (post-launch; nice-to-have)

### Docs
- [ ] README rewrite: clearer value prop, demo GIF, one-line install,
      troubleshooting, FAQ, link to ROADMAP
- [ ] **Demo GIF** (single highest-leverage artifact — terminal tools without one
      die in submission queues). 10–15 seconds: open claude-browse, fuzzy-filter,
      preview, resume. Record with asciinema or terminalizer.
- [ ] CHANGELOG.md (Keep-a-Changelog format)
- [ ] CONTRIBUTING.md (how to run tests, code style, issue/PR guidelines)
- [ ] LICENSE (MIT)

### Code quality
- [ ] Path canonicalization: treat `/Users/<name>` and `/home/<name>` as
      equivalent so synced sessions don't show duplicates. Opt-in config.
- [ ] Tests: session parsing, date formatting, folder-name extraction, path
      canonicalization. Pytest, no network, runs in <1s.
- [ ] GitHub Actions CI: Mac + Linux, Python 3.9–3.13, import check + pytest
- [ ] Edge cases: empty sessions dir, malformed JSONL, sessions with no user
      messages, missing cwd, future schema drift
- [ ] Graceful fallback when `fzf` isn't installed (print install instructions
      instead of a stack trace)

### Launch assets
- [ ] Landing page (GitHub Pages): hero GIF, install, link to paid-product
      waitlist
- [ ] Launch post drafts: HN, ProductHunt, /r/commandline
- [ ] Waitlist form for claude-browse-cloud (lead-capture before any paid-product
      code exists — gate decision to build on waitlist signal)

---

## v1.1 and beyond (nice-to-have, not blocking launch)

- `--stats` mode: time spent across sessions, top folders, message volume
- Export session to markdown / JSON
- Delete old sessions interactively (with confirmation)
- Config file (`~/.config/claude-browse/config.toml`) for defaults
- Shell completions (bash, zsh, fish)
- Color theming
- Plugin hook so community can add custom columns/filters

---

## Paid product direction (separate repos, later)

### claude-sync (Phase 2, post-v1.0 of claude-browse)
**Scope**: a file-level sync tool for `~/.claude/projects/`. Pluggable backends.
Published as a separate OSS tool so adoption isn't gated on the paid product.

**Backends**:
1. Syncthing (default, P2P, free, encrypted)
2. rclone (any cloud — GCS/S3/Dropbox/Drive)
3. Git (private repo)
4. **claude-browse-cloud** (the paid backend — end-to-end encrypted, managed)

Users pick their backend; `claude-sync` wraps the setup so they don't have to
configure each tool by hand.

**Known hard problems to solve**:
- **Concurrent edits**: resuming the same session on two machines at once
  corrupts the JSONL. Options: (a) warn in docs, (b) file-lock during active
  claude session, (c) merge tool for conflict files. Probably (a) + (b).
- **Secret scrubbing**: transcripts contain API keys, pasted credentials. Offer
  opt-in redaction pass before sync, or at least document the risk clearly.
- **Cross-machine path encoding** (handled at the claude-browse layer, see
  above).

### claude-browse-cloud (Phase 3, the monetization layer)
**Scope**: hosted backend for claude-sync + web UI for mobile browsing.

**Architecture sketch**:
- Sync client: daemon watches `~/.claude/projects/`, encrypts client-side,
  uploads deltas to blob storage
- Backend: S3/R2 for blobs + Postgres for metadata + a small API
- Web UI: mobile-first, read + resume-pointer (can't actually launch claude in
  a browser, but can deep-link to `claude --resume <id>` on the user's
  machine via claude-sync daemon)
- Auth: magic-link email to start, SSO for teams later
- Billing: Stripe
- Search: embeddings pipeline (Anthropic/OpenAI), pgvector

**Pricing tiers** (see notes below for rationale):

| Tier           | Price          | Features |
| -------------- | -------------- | -------- |
| Free (OSS)     | $0             | claude-browse + claude-sync with self-hosted backends |
| Personal Pro   | ~$8/mo         | Managed sync, web UI, semantic search, unlimited retention |
| Team           | ~$15/seat/mo   | Shared session library, team search, role-based access |
| Enterprise     | Custom         | SSO, audit logs, on-prem option, compliance (SOC 2, etc.) |

**What makes Personal Pro worth $8**: not the sync (people will self-host) but
the AI-enhanced search ("find where I debugged Redis"), auto-summaries ("what
did I learn this week?"), and auto-tagging. That's where value compounds
beyond plain file storage.

---

## Risks

### Platform risk: Anthropic ships native cross-device session sync
The single biggest risk. Anthropic already has CLI + desktop + web Claude Code.
Unifying session state is a natural next step. If they ship it before
claude-browse-cloud hits meaningful scale, the moat evaporates.

**Mitigations**:
- Move fast, own the terminal-power-user niche early, build brand recognition
- Build features Anthropic won't:
  - **Cross-vendor**: support Aider, Cursor, Codex CLI, Gemini CLI, not just
    Claude Code. The moment the product spans tools, it's outside Anthropic's
    roadmap.
  - **Team features**: shared libraries, role-based access
  - **Compliance**: audit logs, retention policies, SOC 2
- Don't over-invest upfront. Ship MVP, get to 100 paid users, then decide.

### Privacy risk: leaked transcripts
Sessions contain real secrets. Any sync setup can leak them if misconfigured.

**Mitigations**:
- claude-sync offers opt-in secret-scrubbing before upload
- End-to-end encryption for claude-browse-cloud (server never sees plaintext)
- Clear README warnings about what's in session files

### Adoption risk: small addressable market
Claude Code CLI users who'd pay for cross-device sync is a niche. Probably
thousands to tens of thousands globally. A nice indie-SaaS scale business
(<$100K MRR), not a unicorn.

**Mitigations**:
- Accept the scale. Indie-SaaS is a valid outcome.
- Expand addressable market by going cross-vendor (see above)

---

## Decision gates

Gate decisions so we don't build the backend before validating demand:

1. **Ship claude-browse v1.0** → publish on HN/PH/Reddit, count stars/downloads
2. **Collect emails** on claude-browse-cloud waitlist for 4 weeks after launch
3. **Gate**: ≥500 waitlist emails → build claude-sync OSS tool next
4. **Gate**: ≥100 active claude-sync users → build claude-browse-cloud backend
5. **Gate**: ≥50 people on "I'd pay for this" list → launch paid tier

If any gate fails, stop or pivot. Don't sunk-cost through.

---

## Non-goals

Things explicitly not in scope, so nobody wastes time asking:

- Editing session content (claude-browse is read + resume only)
- Integrating with editors/IDEs (out of scope; different product)
- Running claude itself in a browser (Anthropic's problem, not ours)
- Multi-user chat / collaboration on live sessions (complex, small value)
- Session analytics dashboards beyond basic stats (save for a separate tool)

---

## Changelog

- 2026-04-22: First roadmap draft. v1.0 scope frozen.
