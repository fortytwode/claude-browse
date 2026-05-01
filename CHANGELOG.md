# Changelog

All notable changes to claude-browse will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/fortytwode/claude-browse/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/fortytwode/claude-browse/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/fortytwode/claude-browse/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/fortytwode/claude-browse/compare/v0.2.0...v1.0.0
[0.2.0]: https://github.com/fortytwode/claude-browse/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/fortytwode/claude-browse/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/fortytwode/claude-browse/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/fortytwode/claude-browse/releases/tag/v0.1.0
