# Changelog

All notable changes to claude-browse will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/fortytwode/claude-browse/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/fortytwode/claude-browse/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/fortytwode/claude-browse/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/fortytwode/claude-browse/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/fortytwode/claude-browse/releases/tag/v0.1.0
