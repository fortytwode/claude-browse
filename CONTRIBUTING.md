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

## Cutting a release

1. Update `[Unreleased]` in `CHANGELOG.md` to a real version heading with
   today's date, then add a new empty `[Unreleased]` section above it.
   Update the diff link footers.
2. Bump `version` in `pyproject.toml`.
3. Commit, tag, push:
   ```bash
   git commit -am "Release vX.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```
4. Create a GitHub release for the tag (`gh release create vX.Y.Z --title ... --notes ...`).
5. Publishing to PyPI happens automatically: the `Publish to PyPI` workflow
   triggers on `release: published`, builds the sdist + wheel, and uploads
   via Trusted Publishing (no API token).

### One-time PyPI setup (already done for this project)

Trusted Publishing was configured at https://pypi.org/manage/account/publishing/
with these values, matching `.github/workflows/publish.yml`:

- PyPI project name: `claude-browse`
- Owner: `fortytwode`
- Repository name: `claude-browse`
- Workflow name: `publish.yml`
- Environment name: `release`

A `release` environment exists in repo Settings -> Environments. Adding a
required reviewer to that environment is recommended (one click of approval
before each upload), but optional.

If the package didn't exist on PyPI yet at first publish, the trusted
publisher was registered as a "pending publisher" before the first run.

## License

By contributing, you agree that your contributions are licensed under the MIT
License, same as the rest of the project.
