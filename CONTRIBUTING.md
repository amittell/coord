# Contributing

Thanks for taking the time to look at `coord`. This document captures the conventions a PR needs to clear before maintainers will merge it.

## Dev setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The `[dev]` extra installs `pytest`, `pytest-asyncio`, `ruff`, `mypy`, and the optional `tree-sitter` parsers used by the symbol-level claim tests.

## Test conventions

`make check` runs the same three things CI runs (`ruff check .`, `mypy coordination`, `pytest -q`). It finishes in ~30 seconds on a warm cache. Run it before pushing.

```bash
make check         # full local CI mirror
make test-fast     # skip the real-process integration tests
pytest -q -k some  # run a single test by substring
```

Tests live under `./tests/`. Integration tests that spawn real processes are marked with `@pytest.mark.integration` and can be deselected with `pytest -m "not integration"`.

If you touched the container path, also run `make docker-smoke` (builds the image, boots it on port 18099, probes `/readyz`, tears down). `make verify` chains `check` and `docker-smoke` together as the full local pre-push gate.

## Coding style

`ruff` is configured in `pyproject.toml` (`line-length = 100`, `target-version = "py311"`). `mypy` runs non-strict but with `check_untyped_defs` and `no_implicit_optional` on, so new code should carry type hints.

Patterns to match the existing code:

- `from __future__ import annotations` at the top of every module
- explicit type hints on public functions
- `async` db helpers in `coordination/db.py`; service-layer orchestration in `coordination/service.py`
- small focused tests that exercise one code path per file

## Commit messages

Short imperative subject, longer body explaining the why (not just what). When relevant, name the files / functions / line numbers that changed. The prefixes this repo uses:

- `feat:` user-visible new capability
- `fix:` bug fix
- `release:` version bump + CHANGELOG entry (maintainers only)
- `docs:` docs/ or README/CHANGELOG-only change
- `ci:` workflow, hook, or pinning change

## PR norms

- One feature per PR. Refactors land separately from behaviour changes.
- Tests required for any new code path. A test that fails on `main` and passes on the branch is the easiest review.
- `make check` must pass. CI will reject otherwise.
- Third-party GitHub Actions are pinned by full commit SHA with a trailing `# vX.Y.Z` comment. Dependabot bumps both together. Don't downgrade to a tag-only reference.
- Update `CHANGELOG.md` under `## [Unreleased]` for any user-visible change.

## Pre-push hook

The repo ships a local pre-push hook that mirrors CI's cheap jobs. Install it once:

```bash
ln -sf ../../scripts/git-hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

After that, `git push` runs `make check` and aborts the push if anything fails.

## Coordination protocol

This repo eats its own dog food. When working in this codebase via an agent session, follow the protocol in `./CLAUDE.md`:

1. `list_claims` at task start.
2. `claim_files` before editing.
3. `release_claims` (or `release_session`) when done.

External contributors who aren't running a `coord` instance can skip this; the protocol is for agent sessions inside this repo, not a hard PR gate.

## Scope

Maintainers reserve the right to defer PRs that would add complexity without proportionate value. If you're unsure whether something fits, open an issue first and we can scope it together.
