# Changelog

All notable changes to claude-browse will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **ClickUp-style project and priority navigation.** Work now has a responsive
  persistent project sidebar, project paths/counts/local descriptions, row
  transcript previews, four fixed priorities, and Priority or Terminal-state
  grouping. Drag handles and keyboard controls persist same-project task and
  project order, with atomic priority drops, search locking, terminal-state
  rejection, closed-view boundaries, accessible announcements, and rollback
  on failed mutations.
- **Automatic terminal-thread work board.** Every hook-observed Claude or
  CodeX session now becomes one row without Add or Save. The dense local Work
  list opens to Active and includes Today, By Project, and Done & Archived,
  with separate Work status and Terminal state columns, persistent names and
  due dates, and Git-origin/folder grouping that retains the exact launch cwd.
  Done soft-reopens only on a new prompt; Archived requires manual restoration.
  Read-only Thread History remains available beside Work.
- **Guarded provider launches from Work and History.** One Full access toggle,
  on by default, controls Claude and CodeX; turning it off uses safe mode.
  Same-provider actions use native guarded resume, while cross-provider actions
  create a recent-context continuation and a new captured row. Missing local
  prerequisites disable only the affected action. Host checks, JSON-only
  writes, request-token protection, CSP, server-built commands, and argv-safe
  AppleScript remain enforced.
- **Local-only planning metadata.** Work status, due dates, archive state, and
  transcripts stay on the Mac. Cross-Mac Work metadata and Mission Control
  rendering remain deferred; the existing optional sync continues to project
  live status only.
- **Dedicated Agent Board notification identity.** `./install.sh` builds a
  small native `Agent Board Notifier.app`, allowing just Agent Board through a
  macOS Focus instead of allowing every Script Editor alert. The existing
  AppleScript path remains a fail-open fallback when the helper is unavailable.
- **Codex sessions on the Agent Board.** Codex's hooks engine is
  Claude-compatible (same events, same stdin envelope), so the installer now
  writes `~/.codex/hooks.json` registering `agent-board hook --provider
  codex`; `PermissionRequest` maps to `needs-input`. The provider is stored
  per row and every surface (statusline, `aj`, Slack board, alerts) builds
  the resume command from it, so Codex threads get `codex resume <id>`.
- **Unattended completions.** Every completed turn marks the session
  *finished, not picked up* (`done_at`) until you come back: a new prompt
  clears it, or `agent-board ack <id|name>`. Ending the session does not
  (the thread can always be resumed, so it stays listed).
  `AGENT_BOARD_UNATTENDED_MIN_TURN_S` (default 0) sets an optional length
  floor. `aj` and the Slack board lead
  with that list. The Firestore doc carries `provider`, `folder`, `done_at`,
  `done_turn_s`, `acked_at`, `resume_command` and is written with
  `merge=True` so a downstream sweep can own its own fields on the same doc.
- `python3 scripts/install_agent_board.py --check` audits hook wiring
  without writing (exit 1 on drift).

### Changed
- A plain "done" no longer posts an immediate Slack message (it could not
  distinguish a run you walked away from and a turn you watched; an
  interactive evening produced ~15 alerts from 3 threads). Local banner and
  `needs-input` alerts are unchanged. `AGENT_BOARD_IMMEDIATE_DONE_ALERT=1`
  restores the old behaviour.

### Fixed
- **Every Slack alert posted twice, and session names flip-flopped.** The
  sync hook command embeds the interpreter path, which changes from system
  python to `.venv/bin/python` once the board-sync venv exists; the
  installer only ever appended, so both variants fired on every Stop. Two
  concurrent pushes then raced the Haiku namer, and the loser fell back to
  Claude Code's own session title, so one session alternated between two
  names seconds apart. The installer now replaces stale variants and
  collapses exact duplicates, in both `settings.json` and `hooks.json`.
- **Agent Board lifecycle updates could arrive out of order or disappear.**
  Local state now commits before detached publication, dirty revisions retry
  without unbounded worker queues, and versioned alerts cannot erase a newer
  transition. Unacknowledged completions remain visible until resumed or
  acknowledged, including after `SessionEnd`.
- **Codex interruption and hook installation now follow the current contract.**
  `Interrupt` returns a turn to `idle` without recording a completion, hook
  definitions are canonical and matcherless, and the installer detects
  disabled hooks conservatively on every supported Python version.

### Fixed
- **Fresh sessions could disappear when one CodeX fork poisoned the shared
  refresh.** Forked rollout files repeat ancestor metadata, and the indexer
  incorrectly let the oldest ancestor overwrite the current child ID. That
  created a duplicate-path database error before newly discovered Claude
  sessions were committed. The first metadata record now owns fork identity,
  stale parent rows migrate safely, and detached refresh failures are logged.
  Multi-gigabyte CodeX content enrichment is newest-first and resumable within
  a shared 64 MiB budget; oversized individual records are skipped with
  truthful partial-coverage status instead of hanging the picker.
- **Absolute timestamps were printed in UTC but labelled as local.** The
  preview rendered `Started:` / `Last activity:` by slicing the stored ISO
  string and swapping the `T`, with no timezone conversion, so every
  absolute time was off by the viewer's whole UTC offset (5h30m in IST).
  A thread last touched at 19:06 local read as `13:36`. Timestamps now
  convert to local time before rendering, as does the "Began ... before
  last activity" span line.
- **A thread being actively written showed a stale age.** The picker paints
  from the existing index and refreshes in a detached child, so the "40m
  ago" column reported the age as of the last completed index pass, not
  reality; a large, still-growing session could sit tens of minutes behind
  and sort as though it were idle. The age now takes whichever is newer,
  the indexed timestamp or the file's mtime, which costs a stat() and is
  always current.

### Added
- **Open one thread in two terminals and let them diverge.** A thread is
  single-writer: resuming one that is already open elsewhere failed with
  `thread ... already has an active writer (code -32600)`, and because
  resume is an `os.execvp` the error surfaced raw after claude-browse had
  already replaced itself with the provider CLI, so it could neither
  explain nor recover. Resume now detects the collision *first* (a live
  process whose argv[0] is the provider binary and which carries the
  session id) and branches into a new thread seeded from the same history,
  reporting the terminal that holds the original. CodeX uses `codex fork`,
  Claude uses `claude --resume <id> --fork-session`. `--fork` always
  branches; `--no-fork` restores the old attach-anyway behavior. Providers
  without a fork primitive (Gemini, Copilot, Cursor) now fail with the
  holding terminal named instead of a JSON-RPC error code.

## [1.3.0] - 2026-08-10

### Added
- **`--web` local transcript viewer**: `claude-browse --web` opens a
  local-only (127.0.0.1-bound, Host-header-validated, zero-dependency)
  browser page for actually *reading* full past conversations -- sidebar of
  sessions (current folder first, searchable, "this folder only" toggle,
  honors `--here` and `--all`) and a scrollable rendered transcript with
  preserved line breaks, fenced code blocks, and in-thread search. An
  optional add-on; the fzf picker stays the fast resume path.

- **Agent Board**: every Claude Code session is now tracked as a live,
  auto-named thread. State (`working` / `idle` / `needs-input` / `gone` /
  `ended`) is driven by Claude Code hooks and shown in the statusline; a
  native macOS notification (with sound) fires on completion of a long run
  or when a session needs input. A session's name is derived from its own
  Claude Code title when the session is still short, and re-synthesized
  from its most *recent* activity (not the opening prompt) once it has
  grown enough that its topic may have drifted. Optional cross-laptop sync
  mirrors state to Firestore and a private Slack `#agent-status` channel,
  which shows every session across machines with a copy-paste resume
  command each, and posts a distinct alert message (not just a board
  update) when a session needs attention. `work <name>` (tmux
  attach-or-create) and `aj` (board glance) shell functions round out local
  ergonomics. Run `./install.sh` to wire it up (idempotent, backs up
  `~/.claude/settings.json` first). See
  [`docs/agent-board-verification-report.md`](docs/agent-board-verification-report.md)
  for the full build, code-review, and live-verification trail, and the
  README's "Agent Board" section for setup and rollout.
- Local JSONL search diagnostics now write to
  `~/.claude/cache/claude-browse-search.log.jsonl`, recording query
  interpretation, ranker, result counts, top visible matches, and selection
  events for future troubleshooting.
- The provider registry now understands source-vs-target capability and can
  load **experimental** external provider modules from
  `CLAUDE_BROWSE_PROVIDER_MODULES`. This is the first public-ish extension seam
  for providers, but it is intentionally not a stable marketplace API yet.
- Experimental external providers can now also be discovered from local
  directories via `CLAUDE_BROWSE_PROVIDER_DIRS`, and can optionally declare a
  `PROVIDER_API_VERSION` for loader compatibility checks.
- Gemini is now a built-in provider. `gemini-browse` opens everything in
  Gemini by default, `--target gemini` routes the shared browser entrypoint
  there, and the adapter registry now indexes Gemini sessions from
  `~/.gemini/tmp/` alongside Claude and CodeX.
- Cursor is now a built-in **target-only** provider. `cursor-browse` opens any
  indexed thread in Cursor, but the product does not yet claim to index
  Cursor-origin CLI sessions.
- Copilot is now a built-in source+target provider. `copilot-browse` opens
  everything in Copilot by default and indexes local session state from
  `~/.copilot/session-state/`.

### Changed
- **The search index shrank by a third (schema v9, measured live:
  1.1 GB -> 741 MB): semantic postings now intern terms as integer
  ids.** v8 stored every posting's term string twice -- once in the
  table, once in its primary-key autoindex, 606 MB across ~10M
  postings. Postings are now a `WITHOUT ROWID` table clustered on
  `(term_id, window_id)` referencing `semantic_terms`, which removes
  the autoindex outright and halves the postings B-tree. The version
  bump triggers a one-time rebuild (~2 min for 386 sessions), and the
  drop now VACUUMs so the file actually returns the space instead of
  holding a full-size file of free pages.

### Fixed
- **Live session activity no longer stays stale while a large transcript is
  being indexed.** The browser refreshes recency from the transcript tail
  before and after full-text indexing, so a long parse cannot show an old
  "minutes ago" value or overwrite newer activity.
- **A title match can no longer be buried by the code-reference penalty.**
  A single-entity query (e.g. `maxrewards`) demoted any thread whose
  matched snippet mentioned the entity inside a backticked file path --
  even a thread titled with the entity ("Upload MaxRewards testing tasks
  to Frame.io" ranked 33/39). The penalty now exempts threads whose title
  carries the anchor; genuine path-only mentions stay penalized.
- **Board sync's Firestore target is now configurable** via
  `CLAUDE_BROWSE_BOARD_PROJECT` / `_DATABASE` / `_COLLECTION` env vars
  instead of a hardcoded project (defaults preserve existing installs).
- **Current-folder sessions are now guaranteed visible.** The session list
  previously re-sorted only the globally most-recent slice, so a folder
  whose sessions had aged out of that slice showed nothing to float --
  and `--here` could wrongly report "No sessions found" even when real
  sessions existed for the folder. Current-folder sessions are now fetched
  and floated with their own guaranteed query (capped so a folder with a
  long history can't evict all recent cross-project activity), and
  `--here`'s typed-query filter is boundary-aware (scoping to `app` no
  longer matches `app-legacy`).
- **A failed index refresh no longer nukes a healthy index.** The
  recovery path used to treat every non-lock `sqlite3.DatabaseError` as
  file corruption and respond with quarantine + full rebuild -- so an
  application-level `IntegrityError` (an indexing bug) would destroy a
  good 1 GB index and mask the bug behind rebuild churn. Recovery is now
  gated: rebuild only when the exception carries a real corruption code
  (`SQLITE_CORRUPT`/`SQLITE_NOTADB`) or the file fails a full
  `PRAGMA integrity_check`; otherwise the existing index is kept and the
  failure is surfaced as the bug it is. (Diagnosed live: a "corrupt"
  quarantined copy from 2026-07-07 passed a full integrity check -- the
  file had been healthy all along.)
- **Concurrent launches no longer fight over (or corrupt) the search
  index.** Previously every launch ran a read-write reindex, so N windows
  opened at once meant N contending SQLite writers: one ground through a
  slow rebuild while the others either died silently (cold start) or
  served a stale index, and mid-write kills under that contention
  corrupted the database twice in one week. A flock-based single-writer
  election now picks exactly one reindexer per launch wave; other windows
  proceed instantly on the existing index, and a cold-start window waits
  visibly for the builder (taking over automatically if it is killed,
  since the lock dies with its process). Corruption recovery runs under
  the same lock with an inode check, so two windows can no longer both
  quarantine and mass-rebuild.
- **Warm launches are fast again: unchanged sessions are stat()ed, never
  parsed.** Despite the old docstring's claim, every launch fully
  JSON-parsed every session file on disk. Providers now stat-gate against
  the stored mtimes; CodeX freshness switched from global file mtimes
  (which made every codex thread look changed after any codex activity)
  to per-thread `updated_at`/history timestamps. First launch after
  upgrade re-parses once, then steady state is a stat per file.
- **The write-ahead log is bounded.** Nothing ever checkpointed the index
  WAL and the fzf search helper leaked a read connection that pinned
  passive checkpoints -- the WAL grew to ~1 GB. Reindex now ends with a
  best-effort `wal_checkpoint(TRUNCATE)`, write connections use
  WAL-crash-safe `synchronous=NORMAL`, and the helper closes its
  connection per keystroke.
- **`semantic_terms` updates are incremental.** A one-session change used
  to rewrite the whole term table via a corpus-wide GROUP BY (also the
  statement where the corruption surfaced); df counts now ride the same
  transaction as their postings as exact per-session deltas.
- **The index cache is per-host** (`claude-browse-index.<hostname>.db`).
  If `~/.claude/cache` is ever file-synced between machines, a shared
  SQLite file corrupts regardless of local locking. Legacy un-suffixed
  index files (including >1 GB quarantine copies) are reclaimed on first
  open, and future quarantines are pruned (newest generation, 7 days).
- Diagnostic-row suppression no longer hides whole sessions that merely
  *mention* the tool. The old filter branded any session with
  "claude-browse"/"codex-browse" (or other self-referential cues) anywhere in
  its title/first/last message as search-diagnostic noise and silently
  excluded it from every query — 6 of 317 indexed sessions were unfindable,
  including a 1,550-message real work thread that was invisible to the exact
  terms it contained hundreds of times. Suppression now keys on the **match
  evidence** (the snippet the query actually hit) instead of session
  identity: only rows whose matched context is itself search-tool echo get
  suppressed.

### Removed
- `claude-resume` is no longer a supported command surface. The paired browser
  entrypoints (`claude-browse` and `codex-browse`) are now the only reopen
  flows the product documents, installs, and tests.

### Changed
- The browser UX now scales by target app, not by a two-provider toggle.
  `Enter` opens in the launched browser's app in yolo mode, `Ctrl-S` opens in
  safe mode, and cross-provider picks become seeded handoffs into that target
  app. The old `Ctrl-X` "open in the other app" shortcut is gone because
  "other" stops being well-defined once Gemini is added.
- Target-provider defaults now generalize to any target-capable provider whose
  shim name ends in `-browse`. Source-provider indexing remains capability-
  filtered so target-only providers do not get queried for local session data.
- Cross-provider handoff no longer assumes every target can read a temp file
  via an add-dir flag. Providers like Cursor can now receive the import brief
  inline in the launch prompt instead.
- The browser empty-state copy now derives its source-provider list dynamically
  instead of hard-coding Claude, CodeX, and Gemini.
- `claude-browse --list-providers` now prints built-in and external provider
  metadata without requiring `fzf`, making the experimental extension seam
  inspectable from the CLI.
- Search now ranks by relevance, not just recency. Previous behavior was
  "filter by FTS5 token match, then sort by last activity," which meant
  any session that mentioned a term once (a Toggl rollup, a passing
  mention in a status doc) could outrank a session that was actually
  *about* that term. New ranker (`fts.search_ranked`) blends multi-column
  weighted BM25 with exponential-decay recency.
- Schema bumped to v3. The single `corpus` column in `sessions_fts` is
  now six fielded columns (`cwd`, `title`, `first_msg`, `user_text`,
  `asst_text`, `boilerplate`) so each can carry a different BM25 weight.
  cwd is the strongest topic anchor (weight 10); assistant text is
  weakest (0.3); Toggl-style rollup lines like `- musopia: 1.0h` are
  routed to a low-weight `boilerplate` column so they're still
  retrievable but stop dominating client-name queries.
- Recency contribution is `alpha * exp(-age_days / half_life)` with
  `alpha=3`, `half_life=30d`. A 30-day-old strong topical match can beat
  a 1-day-old weak mention, but recent strong matches still win.
- First launch after upgrade rebuilds the index from JSONL (~10s for
  ~4000 sessions). Subsequent launches use the existing per-file mtime
  fast-path.

### Added
- `eval/` directory: pluggable ranker registry, MRR / P@1 / NDCG@5 /
  Recall@10 metrics, interactive labeler. Per-user labeled query set
  lives outside the repo at `~/.claude/cache/claude-browse-eval/queries.json`
  (override with `$CLAUDE_BROWSE_EVAL_QUERIES`).
- `CLAUDE_BROWSE_RANKER=current` escape hatch reverts the picker to the
  pre-v3 recency-only ranker without uninstalling.

## [1.3.0] - 2026-05-05

### Changed
- The list view (and the empty-query default) now sorts by **last activity**
  instead of session start time. A thread you started weeks ago and resumed
  today floats to the top, instead of being buried under newer-but-dormant
  sessions. The displayed date column also reflects last activity.
- Preview pane now shows both `Started:` and `Last activity:` lines (the
  latter only when it differs from the start time).
- Schema bumped to v2 (added `last_timestamp` column). The cache is pure
  derived state, so `claude-browse` drops and rebuilds the index on first
  run after upgrade. Takes a few seconds for hundreds of sessions.

## [1.2.2] - 2026-05-03

### Added
- Preview pane now highlights query matches. When you type a query, each
  occurrence of the search terms in the preview is wrapped in bold yellow,
  matching the row-snippet treatment. fzf's `{q}` is now passed to the
  preview script alongside the row.
- When a query is active, the preview prefers user messages that contain
  a match. If no match lands in the latest 20 messages, the preview falls
  back to the matched messages from earlier in the conversation, so you
  can see *why* the session matched without scrolling. Header label
  switches from "Messages (latest first)" to "Messages (matches first)".
- New `core.extract_query_terms` and `core.highlight_terms` helpers
  (10 unit tests), reused by the preview script via the package import
  path the search script already uses.

### Fixed
- Initial list ordering regressed in 1.2.0. The SQLite migration switched
  the empty-query sort from session start time (`timestamp DESC`) to file
  mtime (`mtime DESC`), so resuming an old session bumped it above sessions
  that were actually started later. Restored "newest started first" as the
  default, with mtime kept only as a tiebreaker.

### Changed
- Search results are now also ordered reverse-chronologically by session
  start time, not by FTS5's BM25 relevance. Users already know what they
  searched for; what they want is "the newest session that mentions X,"
  not "the session where X scored best."

## [1.2.0] - 2026-05-01

### Added
- SQLite FTS5 backed search. fzf is now a pure picker (`--disabled`); each
  keystroke runs an FTS5 query against an index at
  `~/.claude/cache/claude-browse-index.db`. The index is rebuilt
  incrementally on every launch (one stat() per file in steady state).
- Real search semantics:
  - `runna` — exact token match, no character-level fuzzy flood
  - `runna sca2` — implicit AND across both tokens
  - `"runna sca2"` — phrase match (adjacent in order)
  - `runna*` — prefix match
- Matched-context snippet in each row. When a search is active, the row
  shows the line in the conversation where the match occurred, with the
  matched terms highlighted.

### Fixed
- Body search regression introduced in 1.1.0. The `--with-nth=1` +
  `--nth=1,3,4` combo silently never searched the hidden corpus field
  because `--with-nth` collapses each row to its selected fields *before*
  `--nth` runs. Title search kept working; body search did not. Replaced
  with FTS5.
- Short queries like `sca2` no longer match nearly every session via fzf's
  fuzzy scoring. FTS5 is token-based, so 4 chars in arbitrary order across
  a session no longer trigger a hit.

### Changed
- `--no-canonicalize` is now a no-op (accepted for compat). Path
  canonicalization happens at index time, so the display always shows
  unified paths.

## [1.1.0] - 2026-05-01

### Added
- Modern title support: read `ai-title` (Claude Code's auto-generated)
  and `custom-title` (manual via `/name`) events. Older sessions used
  `summary` only, so most modern sessions had no title surfaced before.
- Topic-drift suffix in the list view: when a session's most recent
  substantive user message describes a different topic from the title,
  it's appended in dim ANSI. Claude Code's auto-title locks on the first
  message and never updates, so a long-running session that pivots looks
  misleading without this hint.
- Search across the full conversation: assistant text is now indexed
  alongside user messages, so a query for an acronym or named concept
  the assistant introduced (e.g. "BANP", "search corpus") still finds
  the session. Surfaced via a hidden 4th fzf field, `--nth=1,3,4`.
- 7 new unit tests covering modern title parsing, last-message
  extraction, confirmation filtering, and the expanded corpus.

### Changed
- `extract_user_text` renamed to `extract_search_corpus` to reflect the
  new semantics. The old name remains as an alias so existing callers
  keep working without changes.
- Pure confirmations ("yes", "go ahead please", "sounds good") are now
  filtered out of the topic-drift suffix; they're not topic signal.
- Auto-compaction preambles ("This session is being continued from a
  previous conversation…") are filtered out of both the snippet and the
  search corpus; they're machinery, not user voice.

## [1.0.0] - 2026-04-22

### Added
- ROADMAP.md documenting v1.0 scope and paid-product direction
- LICENSE (MIT)
- CHANGELOG.md
- CONTRIBUTING.md
- `pyproject.toml` so `pip install claude-browse` works
- Path canonicalization — treat `/Users/<name>` and `/home/<name>` as
  equivalent so synced sessions don't show duplicates
- GitHub Actions CI running on Mac + Linux across Python 3.9–3.13
- Unit tests for session parsing, date formatting, folder-name extraction,
  and path canonicalization
- Graceful error when `fzf` isn't installed (prints install instructions
  instead of a stack trace)

### Changed
- `install.sh` now detects Linux and suggests the right package manager for
  `fzf` (apt / dnf / pacman / zypper) instead of only recommending Homebrew
- README rewritten with clearer value prop, demo placeholder, troubleshooting,
  and FAQ

## [0.2.0] - 2026-04-21

### Changed
- Yolo (`--dangerously-skip-permissions`) is now the default. Ctrl-S opts into
  safe mode.

## [0.1.2] - 2026-04-21

### Changed
- Better folder display in narrow terminals
- Ctrl-Y for yolo resume
- Preview shows latest messages first

## [0.1.1] - 2026-04-21

### Fixed
- Folder search for compact display on narrow terminals

## [0.1.0] - 2026-04-21

### Added
- Initial release: `claude-browse` (interactive TUI) and `claude-resume`
  (keyword resume)
- `install.sh` symlinks scripts into `~/.local/bin/`

[Unreleased]: https://github.com/fortytwode/claude-browse/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/fortytwode/claude-browse/compare/v1.2.2...v1.3.0
[1.2.2]: https://github.com/fortytwode/claude-browse/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/fortytwode/claude-browse/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/fortytwode/claude-browse/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/fortytwode/claude-browse/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/fortytwode/claude-browse/compare/v0.2.0...v1.0.0
[0.2.0]: https://github.com/fortytwode/claude-browse/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/fortytwode/claude-browse/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/fortytwode/claude-browse/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/fortytwode/claude-browse/releases/tag/v0.1.0
