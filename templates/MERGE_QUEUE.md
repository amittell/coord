# Integration pipeline (merge queue + stacked PRs)

This complements the coordination service: **claims reduce file collisions**; **merge queue + CI reduce semantic collisions**.

## Merge queue (GitHub)

1. Enable branch protection on `main` with **Require merge queue**.
2. Require status checks to pass before merging.

## Stacked PRs

For cross-cutting work, prefer stacked PRs (Graphite / `git-spice` / `ghstack`):

1. PR1: contracts/types only
2. PR2/PR3: parallel implementation PRs depending on PR1
3. PR4: integration

## CI checks (semantic)

Copy `templates/github-coordination-semantic.yml` into `.github/workflows/` and customize install/typecheck/test steps for your stack.

Suggested additions:

- DB migration collision checks (duplicate versions)
- API contract tests between modules
- Full-repo typecheck
