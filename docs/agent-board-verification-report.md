# Agent Board — Build, Review & Verification Report

**Feature:** Agent thread status board for Claude Code sessions
**Branch:** `feat/agent-thread-status-board` (10 commits on top of `main`)
**Plan:** [`docs/plans/2026-07-03-002-feat-agent-thread-status-board-plan.md`](plans/2026-07-03-002-feat-agent-thread-status-board-plan.md)
**Date:** 2026-07-03
**Status:** implemented, code-reviewed, fixes applied, live-verified against real infrastructure

This document exists so the work can be independently reviewed without re-reading the whole build session. It records what was built, how it was verified, every bug found (by code review *and* by live testing), and what was deliberately left as accepted residual. "Verified" in this document means checked against real running infrastructure — the real Firestore project, the real Slack workspace, real Claude Code sessions on this machine — not just unit tests against mocks. Where a claim rests only on mocked tests, it says so explicitly.

---

## 1. What this is

Turns every Claude Code session into a tracked, auto-named thread with a live state (`working` / `idle` / `needs-input` / `gone` / `ended`), visible in the statusline, pushed as a native macOS notification (with sound) on completion or when blocked, and mirrored to Firestore + a private Slack channel (`#agent-status`) so sessions across multiple machines are visible in one place with a copy-paste resume command each.

Full requirements, scenarios, and design rationale are in the plan doc linked above. This report covers only the build/review/verification trail.

---

## 2. Architecture (one line per module)

| File | Role |
|---|---|
| `claude_browse/board/store.py` | Local SQLite state store (`~/.claude/agent-board/state.db`), WAL mode, cached connection per process, schema migration for existing DBs |
| `claude_browse/board/hook.py` | Claude Code hook dispatcher (`SessionStart`/`UserPromptSubmit`/`Stop`/`Notification`/`SessionEnd`) — purely local/sqlite, never touches network, always exits 0 |
| `claude_browse/board/notify.py` | Native macOS notifications via `osascript`, with sound |
| `claude_browse/board/statusline.py` | Statusline renderer; also the primary liveness heartbeat source |
| `claude_browse/board/naming.py` | Auto-namer: reuses Claude Code's own title for short sessions, re-synthesizes from recent activity via Haiku once a session has grown |
| `claude_browse/board/cli.py` | `agent-board board` / `aj` — local terminal glance at all active sessions with resume commands |
| `claude_browse/board/sync.py` | Out-of-band: mirrors state to Firestore, renders/updates the Slack board, posts distinct alert messages for important transitions |
| `agent-board` | Entry script dispatching `hook` / `statusline` / `board` / `sync` subcommands |
| `shell/agent-board.zsh` | `work <name>` (tmux attach-or-create) and `aj` shell functions |
| `scripts/install_agent_board.py` | Idempotent `~/.claude/settings.json` wiring, called from `install.sh` |

---

## 3. Build log (10 commits, unit by unit)

Each unit was built test-first where practical, then verified live before moving to the next. "Live verification" column states what was actually run against real state, not just what the unit tests cover.

| # | Commit | Unit | Live verification performed |
|---|---|---|---|
| 1 | `6c58836` | U1 — SQLite store | Manual `upsert`/`get` round-trip, cross-checked with a raw `sqlite3` query against the real DB file |
| 2 | `5892a87` | U2 — Hook dispatcher + notifications | Piped real JSON through the *exact* command string later registered in `settings.json`; separately fired a real `osascript` call and got user confirmation a banner appeared |
| 3 | `82fbd81` | U3 — Statusline + heartbeat | Wired into `settings.json`, ran the exact registered command against this session's real `cwd` |
| 4 | `76312b5` | U4 — Auto-namer | Ran `naming.compute_name()` against this session's **real** jsonl transcript — confirmed it correctly located the file and returned the real existing title with zero API calls |
| 5 | `9596cf1` | U5 — `aj` CLI + tmux | Rendered the board against **three other real, independently-running Claude Code sessions** on this machine (discovered live, not staged) that the hooks were already tracking; separately verified real tmux attach-or-create semantics (no duplicate session, clean `kill-session`) |
| 6 | `a6522ac` | U6 — Firestore sync | Real write→read→delete round-trip against project `team-projects-480520`; then a full `push()` through the exact wired async-hook command, confirmed the doc landed |
| 7 | `fdc1ae5` | U7 — Slack board | Created the real private `#agent-status` channel (confirmed it didn't exist first via a live channel-list check; asked the user before creating); posted and then updated the same message in place, confirmed via matching `ts` that it was `chat.update`, not a duplicate post |
| 8 | `25d58ef` | U8 — Install + rollout | Ran `install.sh` for real on this machine; separately tested from-scratch wiring in an isolated fake `$HOME`, confirming a pre-existing unrelated hook survived untouched and a second run was a true no-op (zero new backups either way) |
| 9 | `2fadf25` | Code-review fixes | See §4 below |
| 10 | `f62da9b` | Naming refresh + Slack alerts + efficiency | See §5 below |

---

## 4. Code review (commit `2fadf25`)

Ran an 8-angle review against the full branch diff: 3 correctness angles (line-by-line, removed-behavior, cross-file trace), reuse, simplification, efficiency, altitude, and CLAUDE.md conventions. 10 findings survived verification and were fixed; a further set of lower-severity efficiency/reuse findings were explicitly deferred (see §6).

### Findings fixed

1. **CRITICAL — `notify.py` emoji/AppleScript bug (CONFIRMED by direct repro).** `json.dumps()` was used to quote AppleScript string literals. `json.dumps("✅ done")` renders as a `✅`-style escape, which AppleScript's own string syntax cannot parse — every real `notify()` call (both of `hook.py`'s actual titles use emoji) failed with a syntax error, silently swallowed by `subprocess.run(..., check=False)`. **Native notifications had never once fired in this entire build before this fix.** Reproduced directly with `osascript`, fixed with proper AppleScript quoting (escape only backslash/quote, keep unicode literal). `notify.py` had zero test coverage before this fix — a real test file was added, including a non-mocked `osascript` repro of the exact bug.
2. **`naming.py` structurally unreachable without the optional sync venv.** `naming.maybe_name()` is only ever invoked from `sync.push()`, which was only wired via a hardcoded `.venv/bin/python` path that didn't exist yet on a fresh install. Fixed: the sync command falls back to system `python3` when the venv is absent, auto-upgrading once the venv exists on a later `install.sh` run. Also added the missing `anthropic` dependency to the `board-sync` pyproject extra (it was referenced in code but never declared).
3. **`sync.py`'s Slack board never showed a resume command**, contradicting the plan's own R6 requirement and its S4 scenario. Fixed using the same `get_provider().native_resume_cmd()` path `cli.py` already uses.
4. **`hook.py`'s needs-input trigger was a closed allowlist**, meaning any `notification_type` a future Claude Code version introduces would silently no-op instead of alerting. Inverted to a fail-safe denylist (ignore known-safe types, default everything else to needs-input).
5. **`sync.py`'s `.env` parser didn't strip inline `# comment` suffixes** on unquoted values, so a line like `SLACK_BOT_TOKEN=xoxb-...  # rotated` would corrupt the token. Fixed with quote-aware, comment-aware parsing.
6. **`store.py`'s `display_state()` used unguarded `row["state"]`** — safe for local SQLite rows, unsafe for Firestore-sourced dicts with no schema guarantee. Fixed to use `.get()` throughout.
7. **`host=_hostname()` was repeated at every hook.py call site** instead of centralized — the exact pattern that caused a real bug found earlier in this same session (see §7). Centralized into a single `_set_state()` helper so a future new event handler can't reintroduce it.
8. **Liveness (`gone`) depended entirely on the statusline's refresh cadence**, unverified for a detached/backgrounded pane — flagged as plausible risk in the review. **This became a confirmed, live bug during re-verification** — see §5.
9. **The state→icon/order mapping was duplicated 3×** across `cli.py` and `sync.py`. Consolidated into one definition in `store.py` (`STATE_ORDER`, `STATE_ICON`), imported by both renderers.
10. **`naming.py`'s `_find_jsonl_path()` reinvented a narrower glob** than `providers/claude.py`'s existing `list_session_files()`, which already recovers sessions whose project directory was renamed or moved. Fixed to reuse the existing function.

Every fix above was accompanied by a new or updated regression test, and the critical ones (#1, #2, #3) were additionally re-verified against real infrastructure (real `osascript`, real Firestore project, real Slack channel) after the fix, not just re-run against mocks.

---

## 5. Post-review live-testing bugs (commit `f62da9b`)

Re-verifying the review's fixes against real data surfaced two further real bugs the static review couldn't have caught, plus two feature requests from the user that turned into real fixes:

1. **Confirmed live: this session showed `gone` on the real Slack board while actively being worked on.** The plausible risk flagged in §4 item 8 was real — the statusline's refresh cadence during a long tool-heavy sequence was less reliable than assumed. Fixed: `Stop` (which fires reliably on every turn per the verified hook contract) now also refreshes the heartbeat, giving liveness a second, more dependable source. Verified by healing this exact session's live `gone` state through the real wired `Stop` command and confirming it returned to `idle`.
2. **Found live while testing the naming fix: `anthropic` was declared in `pyproject.toml` (from fix #2 above) but never actually installed into this machine's venv.** The Haiku namer had been silently failing and falling back to the stale title the entire time since that "fix" landed. Caught by deliberately bypassing the try/except to surface the real `ModuleNotFoundError`, then `pip install -e ".[board-sync]"` to actually install it.
3. **Naming redesign (user-reported): the name was frozen from the session's opening prompt forever**, even as the topic drifted enormously over hundreds of turns. Redesigned: a session now re-synthesizes its name from its most **recent** turns (not the opening prompt) once it has grown by ≥20 messages since it was last named (tracked via a new `named_at_msg_count` column, with a schema migration so this applies to already-existing databases without data loss). Verified live: this session (881+ messages at the time) went from "Continue CodeX session context import" to "installing anthropic dependency and setting api key"; three other real sessions on this machine were refreshed the same way, from stale import-derived titles to their actual current topics (e.g. "tiktok oauth authorization in progress").
4. **Slack notification-on-change gap (user-reported, confirmed real): `chat.update` does not trigger a fresh Slack notification** for channel members — only the very first post ever did. Fixed with a `pending_alert` marker: `hook.py` sets it at the exact moment it fires a local notification (single source of truth for "this matters"), and `sync.py` posts a genuinely **new** message (`chat.postMessage`, not `chat.update`) for that transition, then clears the marker. Verified for real: the exact wired commands produced a new alert message in the real channel — and incidentally caught a **second, independent real alert** that had already fired on its own from another live session's long-run completion, which is stronger evidence than a staged test could provide.
5. **Efficiency residuals fixed (deferred from §4's review):** `store.get_conn()` now caches one SQLite connection per process (a single hook invocation used to open 2–3 separate connections for one logical operation); `sync._firestore_client()` now caches one Firestore client per process (`push()` alone used to construct up to 4).

A further, related discovery during this pass: the user reported "I see codex but no name" on the mobile/cloud Slack view. Investigated live by dumping the real Firestore collection — confirmed **every row is a genuine Claude Code session on this same host**; three of them simply had names containing the word "CodeX" because they were originally started by importing a prior CodeX conversation (identical to this session's own origin). Not a provider bug — clarified, and those three rows were refreshed as part of item 3 above. Confirmed as an explicit, deliberate scope boundary (not an oversight): this build has no integration with the native CodeX CLI's own session/hook mechanism at all.

---

## 6. Explicitly accepted residuals (not fixed, and why)

| Item | Why deferred |
|---|---|
| `sync.render_slack_body()` does a full Firestore collection fetch on every push | Real but lower severity; a caching fix risks trading correctness (staleness) for a speed gain that hasn't mattered at this scale in practice |
| `sync.check()`'s Firestore/Slack connectivity probes run sequentially, not in parallel | Manual diagnostic command only, not a hot path |
| A few minor reuse opportunities flagged by the review's reuse angle (e.g. `sync._log()` could reuse `search_log.py`'s logger; `cli.py`'s resume-command doesn't handle a moved/stale `cwd` the way `browse.py`'s own resume flow does) | Real but correctness-neutral; lower priority than what was fixed |
| No native CodeX CLI integration | Explicit scope boundary from the original plan (`claude-browse` supports CodeX as a browse/resume provider; this feature only instruments Claude Code's own hook system, which CodeX has no equivalent of that's been investigated yet) |
| Slack Canvas (considered during brainstorming, not built) | Deliberate: a regular message + `chat.update` was chosen in planning (KTD6); user explicitly deferred re-considering Canvas to a later conversation |

---

## 7. Timeline of every real bug found (chronological, for audit purposes)

1. **`host` field left `NULL`/`unknown-host`** for this session's own real Firestore doc — root cause: `Stop`/`Notification`/`SessionEnd` only ever called `store.set_state()`, which never accepted or backfilled `host`; only `SessionStart`/`UserPromptSubmit` did, and `SessionStart` never fires for a session that was already running when the hooks were wired. Found by inspecting this session's own live Firestore doc mid-build. Fixed in commit `fdc1ae5`; generalized (centralized into one `_set_state()` helper) in the code review, commit `2fadf25`.
2. **Orphaned Firestore doc under a stale `host` key**, a direct consequence of fix #1 (the doc ID includes `host`, so healing `host` created a second doc under the old key). Found and cleaned up in the same session, commit `fdc1ae5`.
3. **`notify.py`'s emoji/AppleScript bug** — see §4 item 1. The single most severe finding in this entire report: native notifications silently never worked until this fix.
4. **`anthropic` declared but not installed** — see §5 item 2.
5. **`ANTHROPIC_API_KEY` not in the hook's inherited environment** — investigated as a suspected second gap, but confirmed the existing `_load_env_fallback()` (built for `SLACK_BOT_TOKEN`) already generically covers any missing env var, so no code change was needed here — just verification.
6. **Slack `chat.update` doesn't notify** — see §5 item 4.
7. **Statusline heartbeat gap under long tool-heavy sequences** — see §5 item 1, confirmed live on this exact session.
8. **Naming frozen on the opening prompt forever** — see §5 item 3, the most consequential design gap found via real usage rather than review.

---

## 8. Test suite

| Point in the build | Test count |
|---|---|
| After U1–U8 (initial build) | 342 |
| After code-review fixes (`2fadf25`) | 354 |
| After naming redesign + Slack alerts + efficiency (`f62da9b`) | **366** |

All 366 pass as of this report (`./.venv/bin/python -m pytest -q`). Coverage includes: every hook event and its notification-gating logic (including the fail-safe-denylist regression test and the host-backfill regression test), the SQLite store including a simulated pre-existing-older-schema migration test, the naming refresh threshold logic, the Slack alert mechanism including a real (non-mocked) `osascript` repro, and an end-to-end subprocess test that pipes real JSON through the actual `agent-board hook` entry script.

---

## 9. How to independently re-verify

```bash
cd ~/claude-browse
./.venv/bin/python -m pytest -q                     # 366 tests, all green

# Real connectivity (Firestore + Slack), reports creds source + live status
./.venv/bin/python agent-board sync check

# Real board render from actual local + Firestore state
./.venv/bin/python agent-board board
./.venv/bin/python -c "from claude_browse.board import sync; print(sync.render_slack_body())"

# Re-run install idempotently (should report "already wired", zero new backups)
./install.sh
ls ~/.claude/settings.json.bak-* | wc -l            # compare before/after
```

---

## 10. Commit reference

```
f62da9b feat(board): naming reflects current activity, Slack alerts on transitions, efficiency
2fadf25 fix(board): code-review fixes -- critical notify bug, R2/R6 gaps, safety hardening
25d58ef feat(board): U8 - install wiring, native-overlap check, rollout docs
fdc1ae5 feat(board): U7 - Slack #agent-status board renderer
a6522ac feat(board): U6 - Firestore cross-laptop sync
9596cf1 feat(board): U5 - jobs board CLI + tmux work/aj aliases
76312b5 feat(board): U4 - auto-namer (existing title reuse + Haiku fallback)
82fbd81 feat(board): U3 - statusline renderer + liveness heartbeat
5892a87 feat(board): U2 - hook dispatcher and native macOS notifications
6c58836 feat(board): U1 - local SQLite session-state store
```

23 files changed, 2975 insertions across the branch (`git diff main...HEAD --stat`).
