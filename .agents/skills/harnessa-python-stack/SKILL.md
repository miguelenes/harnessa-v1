---
name: harnessa-python-stack
description: Use for Python dependency updates, tests, code quality, refactoring, or application-structure changes in the Harnessa repository.
---

# Harnessa Python stack

Use this project guide to adapt the imported Python skills to Harnessa's existing stack. Read the applicable source files and active milestone prompt before making changes; preserve current boundaries and user edits.

## Project conventions

- Python is `>=3.12,<3.13`. `pyproject.toml` pins direct and development dependencies exactly, and `uv.lock` records the resolved environment. Keep both in sync.
- The API is a modular monolith: `apps/api/harnessa/` separates `api`, `domain`, `services`, `adapters`, and `db`; the worker has a separate entry point. Preserve those boundaries unless the requested change demonstrates a concrete need to move them.
- PostgreSQL owns durable domain state and evidence. Redis is rebuildable transport. Keep external framework types behind the existing adapter ports and preserve the Pydantic/OpenAPI to TypeScript contract-generation path.
- Ruff targets Python 3.12 with a 100-character line length. Mypy is strict. Do not replace or supplement these tools with Black, `ty`, or another checker unless the task calls for that change.
- Pytest is configured at the repository root. Integration tests use disposable PostgreSQL and Redis; they truncate the test database and flush Redis database 1. Never point them at development or production data.

## Working guidance

- For dependency updates, use `uv` and make a focused change. For a direct production dependency, update its exact pin in `pyproject.toml`, resolve `uv.lock`, and review the complete lock diff. Keep spike-only packages in the isolated dependency set required by the active milestone; do not add them to production manifests by default. Avoid blanket upgrades and `pip` workflows. Verify compatibility with the project's Python range and the package's supported API before changing major versions.
- For test design, extend the existing `tests/` layout and fixtures. Keep unit tests deterministic; use the integration marker only when real PostgreSQL or Redis behavior is required. Do not add a test framework or dependency without a concrete requirement.
- For refactors, preserve observable API behavior, database ownership, event/effect semantics, and generated contracts. Prefer a small responsibility-focused change over new layers or patterns without a demonstrated need. Treat rules such as file-size limits and the Rule of Three as heuristics, not gates.
- Use the repository's existing commands when the requested work includes running checks: `uv run ruff check apps/api tests`, `uv run pytest`, `pnpm typecheck:api`, and `pnpm test:integration` (with disposable services and data). Do not infer that integration or live-service coverage ran from a unit-test result.

## Imported skill routing

- Use [uv-package-manager](../uv-package-manager/SKILL.md) for uv command details, but adapt its generic examples to Harnessa's exact pins and avoid its blanket-upgrade examples by default.
- Use [python-testing-patterns](../python-testing-patterns/SKILL.md) for pytest patterns, adapted to the existing fixtures, markers, and service boundaries.
- Use [python-design-patterns](../python-design-patterns/SKILL.md) for refactoring heuristics; keep Harnessa's existing architecture authoritative.
