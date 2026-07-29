# Repository Guidelines

## Project Structure & Module Organization

`backend/src/bidscope/` is the Python application: `api/` contains FastAPI routes,
`domain/` models business concepts, `graph/` runs the workflow, and `persistence/`,
`delivery/`, `snapshots/`, and `subscriptions/` own their respective boundaries.
Keep tests under the matching layer in `backend/tests/unit/`, `contract/`,
`integration/`, or `security/`. Database migrations live in `migrations/versions/`.
The React/Vite frontend is in `web/src/`; browser workflows and fixtures are in
`e2e/specs/` and `e2e/fixtures/`. Committed evaluation fixtures live in `eval/`.

## Build, Test, and Development Commands

- `uv sync` installs the pinned Python environment; `npm --prefix web ci` installs web dependencies.
- `uv run bidscope api serve` starts the API; start Postgres and MinIO first with `docker compose up -d postgres minio`.
- `uv run ruff check backend scripts` and `uv run mypy backend/src/bidscope` run the backend quality gates.
- `uv run pytest backend/tests/unit backend/tests/contract backend/tests/security -q` runs database-free checks. Run `uv run pytest backend/tests/integration -q` only with the test database configured and migrated.
- `npm run test:web` runs Vitest; `npm run build:web` type-checks, bundles, and copies the SPA into the backend static directory. `npm run test:e2e` runs Playwright against PostgreSQL.

## Coding Style & Naming Conventions

Use Python 3.12, four-space indentation, complete type annotations, and `snake_case` modules/functions; Ruff enforces a 100-character line limit. Keep FastAPI boundaries thin and put domain logic in the appropriate module. Use TypeScript with two-space indentation, `PascalCase` React components, and `camelCase` hooks and helpers. Do not edit generated `backend/src/bidscope/static/` output directly; rebuild it with `npm run build:web`.

## Testing Guidelines

Name Python tests `test_*.py` and test functions `test_<behavior>`; name component tests `*.test.tsx` and Playwright scenarios `*.spec.ts`. Add a focused regression test for every behavior change and select the lowest suitable test layer. Integration and E2E tests must use synthetic fixtures only; never introduce live procurement traffic or credentials.

## Commit & Pull Request Guidelines

Follow the existing conventional prefixes: `fix:`, `test:`, `docs:`, `ci:`, `style:`, and `chore:`. Keep commits atomic and describe the user-visible intent. PRs should state the problem and approach, link the relevant issue or task, list validation commands, and include screenshots for UI changes. Call out migrations, environment variables, operational effects, and any skipped checks explicitly. Never commit `.env`, tokens, or production data.
