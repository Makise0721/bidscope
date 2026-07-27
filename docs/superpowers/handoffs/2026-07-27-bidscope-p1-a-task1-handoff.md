# P1-A Task 1 Handoff

**Date:** 2026-07-27
**Baseline before this task:** `4b67fe5 fix: harden test environment URL validation`

## Verified Scope

Task 1 hardened configuration and deployment boundaries only. It did not implement Task 2 authentication/middleware work, audit events, readiness, backups, frontend authentication, or a user-visible cancellation endpoint.

- `Settings` stores `database_url` and `checkpoint_database_url` as `SecretStr` values. Runtime consumers unwrap them only through `database_dsn()` and `checkpoint_database_dsn()` at SQLAlchemy, Alembic, LangGraph checkpoint, scheduler, and integration-driver boundaries.
- Production Settings rejects the built-in demo DSNs and rejects DSNs with a wrong driver prefix, missing authority/host, missing database path, query string, fragment, or invalid port. It accepts external database hosts; the integration guard's loopback restriction is not applied to production Settings.
- Settings string/repr, `model_dump()` representation, and JSON-mode dump tests verify that DSN passwords are masked. Failed validation rebuilds `ctx['error']` as a plain `ValueError` without the original traceback.
- API and scheduler CLI startup commands emit the fixed `BidScope startup configuration is invalid.` marker rather than rendering settings validation details.
- The integration environment guard rejects query strings and fragments for either DSN, retains strict scheme/authority/loopback/port/database matching, and emits a fixed failure marker without host, database, or password data.
- API integration fixtures and direct API integration Settings now derive DSNs from guarded `get_settings()`, so a dedicated non-default test port/database is not split from engines, migrations, or checkpoint consumers.

## Production Configuration and Compose Constraints

- Production Compose passes `BIDSCOPE_REAL_MODEL_ENABLED` to both API and scheduler with a `false` default, and passes `BIDSCOPE_MODEL_API_KEY` as blank when unset. Settings rejects enabled real-model mode without a non-empty key.
- PostgreSQL and MinIO expose no host `ports`. API binds only `127.0.0.1:8000:8000` for a local reverse proxy.
- Scheduler has `healthcheck.disable: true`; API retains its existing `/healthz` healthcheck.
- `.env.production.example` explicitly includes database, PostgreSQL, S3, model, admin-token, origin, and host variables. Its active origin/host entries are syntactically valid examples, and its model API key is blank.
- `docs/deployment.md` documents the current Compose sequence, pre-encoded DSN restriction, internal PostgreSQL/MinIO networking, no `minioadmin` fallback, and scheduler healthcheck behavior.

## Verified Commands

All commands below were run from `C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-final-17-20`.

| Command | Result |
|---|---|
| `uv run pytest backend/tests/security/test_settings_secret_boundary.py backend/tests/security/test_env_guard.py backend/tests/security/test_cli_configuration_boundary.py -q` | `59 passed` |
| `uv run pytest backend/tests/unit/test_integration_api_fixture.py backend/tests/unit/deployment/test_deployment_contract.py backend/tests/security -q` | `135 passed, 1 skipped` |
| `uv run pytest backend/tests/unit backend/tests/contract backend/tests/security -q` | `470 passed, 2 skipped` |
| `uv run ruff check backend scripts` | passed |
| `uv run mypy backend/src/bidscope` | `Success: no issues found in 66 source files` |
| Production Compose `docker compose ... config -q` with complete test-only variable values | passed |
| Direct Settings secret-boundary probe | passed |
| `git diff --cached --check` | passed before commit |
| Staged credential-signature scan | no matches before commit |

The pytest gates emitted two pre-existing dependency warnings from LangGraph and Starlette/httpx.

## Environment-Limited Gates

- PostgreSQL integration tests were not run after a direct TCP probe to `127.0.0.1:5432` timed out. No Compose services were started for this task.
- Docker was available and Compose rendering was validated, including the rendered port/healthcheck deployment-contract test. Docker runtime startup/smoke against PostgreSQL and MinIO was not run.

## Residual Non-Blocking Items

- The full integration suite still needs an environment with a reachable dedicated guarded `*_test` or `*_e2e` PostgreSQL target.
- Deployment docs use `.example.test` as active syntactically valid template values; operators must replace them with real public origin and host values before production deployment.

## Task 2 Entry Point

Task 2 is not implemented. Start from `docs/superpowers/plans/2026-07-26-bidscope-p1-a-security-configuration-plan.md`, Task 2: harden the Admin Token dependency and route matrix. Its explicitly scoped work is `backend/src/bidscope/api/auth.py`, protected route boundaries, production CORS/TrustedHost setup, and corresponding security tests. Do not fold it into further Task 1 configuration changes.
