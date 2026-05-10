# Changelog

All notable changes to claude-browse will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
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

[Unreleased]: https://github.com/fortytwode/claude-browse/compare/v1.2.2...HEAD
[1.2.2]: https://github.com/fortytwode/claude-browse/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/fortytwode/claude-browse/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/fortytwode/claude-browse/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/fortytwode/claude-browse/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/fortytwode/claude-browse/compare/v0.2.0...v1.0.0
[0.2.0]: https://github.com/fortytwode/claude-browse/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/fortytwode/claude-browse/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/fortytwode/claude-browse/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/fortytwode/claude-browse/releases/tag/v0.1.0
