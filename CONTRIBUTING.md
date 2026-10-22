# Contributing to Harnessa

Thanks for contributing. Harnessa is an active project; start with a small, focused issue or discussion and check the relevant [milestone prompt](planning/README.md) before changing a product boundary.

## Before you commit

Install `uv` (with Python 3.12 available), Node.js, pnpm, and [`pre-commit`](https://pre-commit.com/). In the repository, install and run the pre-commit checks:

```sh
uv tool install pre-commit==4.3.0
pre-commit install
pre-commit run --all-files
```

The hook scans staged files with Yelp `detect-secrets`, runs the full lint, type-check, and test commands, and builds the docs in strict mode. Integration cases run when disposable `TEST_DATABASE_URL` and `TEST_REDIS_URL` values are configured; otherwise they skip with the suite's explicit prerequisite message. If the scanner finds a likely secret, remove it or rotate it; never add real credentials to the baseline. The scanner is a heuristic, so review changes carefully and do not treat a passing scan as proof that no secret is present.

## Development

Use the supported Python 3.12 environment and the root `uv.lock`. Use the Node and pnpm versions recorded by the project; do not hand-edit generated contract files.

```sh
uv sync --all-groups
uv run ruff check apps/api tests
uv run mypy apps/api/harnessa
uv run pytest
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm test
pnpm --filter @harnessa/web build
```

Integration tests require disposable PostgreSQL and Redis services. They truncate their configured test database and flush Redis database 1. Use test-only service URLs and never point them at development or production data. See the [getting started guide](https://docs.harnessa.cloud/getting-started/) and [architecture decisions](planning/architecture-decisions.md) for details.

## Pull requests

- Explain the user impact and keep each change focused.
- Include or update documentation and focused tests for behavior changes.
- Report the checks you ran and distinguish local results from hosted or live-service results.
- Do not include credentials, customer data, generated build output, or unrelated worktree changes.
- Target `main`. Changes merge through a pull request and the repository's protected-branch rules.

By submitting a contribution, you agree that it is provided under the repository's [Apache-2.0 license](LICENSE).
