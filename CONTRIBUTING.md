# Contributing

Thanks for considering a contribution. claude-browse is a small, focused tool
— scope is deliberately narrow. See [ROADMAP.md](ROADMAP.md) for what's in and
out of scope.

## Quick start

```bash
git clone https://github.com/fortytwode/claude-browse.git
cd claude-browse
./install.sh
pip install -e '.[dev]'
pytest
```

## Running tests

```bash
pytest                # all tests
pytest -k parsing     # filter by name
pytest -v             # verbose
```

Tests don't make network calls and don't require a real Claude Code install —
they use fixture JSONL files in `tests/fixtures/`.

## Code style

- Python 3.9+ compatible
- Standard library only for the core CLI (no runtime deps beyond `fzf` which
  is an external binary)
- Keep the two scripts (`claude-browse`, `claude-resume`) runnable as plain
  shebang scripts — don't require a package install to use them
- Prefer clarity over cleverness. This is a tool people will read to trust.

## Opening an issue

- **Bug**: include OS, Python version, fzf version, a minimal repro, and the
  error output
- **Feature**: check ROADMAP.md first. If it's listed as non-goal or
  out-of-scope, the PR will likely be declined
- **Question**: GitHub Discussions is better than Issues for usage questions

## Opening a PR

- One logical change per PR
- Add tests for behavior changes
- Update CHANGELOG.md under `[Unreleased]`
- Small PRs merge fast; large refactors should come with an issue first to
  align on scope

## What this project is not

To save your time, these won't be merged:

- Features that require a network connection or external service in the free
  CLI (those belong in the separate `claude-sync` / `claude-browse-cloud`
  projects — see ROADMAP.md)
- Editor/IDE integrations
- Changes that edit session content (claude-browse is strictly read + resume)

## License

By contributing, you agree that your contributions are licensed under the MIT
License, same as the rest of the project.
