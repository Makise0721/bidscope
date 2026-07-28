# P1-A Task 1 Follow-up Handoff

**Date:** 2026-07-27
**Follow-up baseline:** `fec4a7ba98fe5e0ac319ea1ba4c5d7d83d0f17a9`

## Verified Scope

This follow-up remains limited to Task 1 configuration, deployment, CLI startup, and integration-fixture boundaries. It does not implement Task 2 authentication/middleware, audit events, readiness, backups, frontend authentication, or cancellation work.

## Production DSN Contract

- `database_url` accepts only `postgresql+asyncpg://`; `checkpoint_database_url` accepts only `postgresql+psycopg://`.
- Both production DSNs require an authority/host, non-empty database path, valid explicit port when supplied, and explicit non-empty username and password. This prevents driver fallback to ambient PostgreSQL credentials.
- Percent-encoded credentials remain valid. Production DSNs are not restricted to loopback hosts; that restriction remains exclusive to the integration environment guard.
- The only accepted TLS query keys are `ssl=require` for asyncpg and `sslmode=require` for psycopg. An installed SQLAlchemy dialect probe verified that the former becomes asyncpg's `ssl` connection argument and the latter becomes psycopg's `sslmode` connection argument.
- Target and credential overrides (`host`, `port`, `database`, `dbname`, `service`, `user`, `password`, `passfile`), duplicate or empty parameters, unknown parameters, fragments, malformed URLs, wrong schemes, and invalid ports are rejected.
- Any validation error input at a DSN or secret field is rendered as the fixed `**********` mask. This applies to malformed URLs, wrong schemes, query/fragment data, and secret-field type errors. Sanitized `ctx['error']` is a plain `ValueError` without a traceback; structured errors, string/repr/JSON renderings, exception chains, and retained validator frames do not expose tested DSN or secret values.
- Successful `Settings` string/repr and Python/JSON `model_dump()` paths retain masked DSNs through `SecretStr`.

## Guarded Test Targets and CLI Boundaries

- `backend/src/bidscope/testing/env_guard.py` remains fail-closed: it still requires the allowed PostgreSQL schemes, loopback host, valid port, matching host/port/database, and a `*_test` or `*_e2e` database. It still rejects every query, fragment, and target override with the fixed failure marker.
- The API integration fixtures plus the graph/checkpointer helpers in `test_subscriptions.py`, `test_scheduler_lock.py`, and `test_completed_run_delivery.py` now derive both DSNs from guarded `get_settings()`. The migration URL probe in `test_migrations.py` also derives its unreachable-port DSN from the guarded checkpoint URL before changing only the test port. A custom guarded localhost port/database therefore reaches engines, migrations, and the checkpointer consistently.
- `checkpoints setup`, `snapshots inspect`, and `snapshots import` all validate settings before constructing databases, object stores, or checkpoint setup. Invalid production settings produce the startup marker rather than Pydantic detail or DSN data.

## Compose and Template Contract

- The shared production Compose environment passes `BIDSCOPE_MODEL_BASE_URL` and `BIDSCOPE_MODEL_NAME` to both API and scheduler. It explicitly defaults to `https://api.deepseek.com` and `deepseek-chat`, matching Settings.
- A rendered Compose JSON probe with `https://model.example.test/v1` and `model-sentinel` confirmed those exact values in both service environments.
- `.env.production.example` uses safe explicit defaults: `BIDSCOPE_ADMIN_TOKEN_MIN_LENGTH=32`, `BIDSCOPE_S3_REGION=us-east-1`, `BIDSCOPE_MODEL_BASE_URL=https://api.deepseek.com`, and `BIDSCOPE_MODEL_NAME=deepseek-chat`. No real credentials were added.

## Verification

All commands were run from `C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-final-17-20` after the follow-up implementation.

| Command | Result |
|---|---|
| Focused red: new production DSN security regressions before implementation | failed, reproducing missing credentials, no TLS-query support, and incomplete DSN sanitization |
| Focused red: CLI configuration boundary regressions before implementation | 3 failed, reproducing missing startup checks for checkpoint and snapshot commands |
| Focused red: fixture/Compose regressions before implementation | 5 failed, reproducing fixed integration targets and missing model pass-through |
| `uv run pytest backend/tests/security/test_settings_secret_boundary.py -q` | `41 passed` |
| `uv run pytest backend/tests/security/test_cli_configuration_boundary.py -q` | `4 passed` |
| `uv run pytest backend/tests/unit/test_integration_api_fixture.py backend/tests/unit/deployment/test_deployment_contract.py -q` | `15 passed, 2 warnings` |
| `uv run pytest backend/tests/security/test_env_guard.py backend/tests/unit/test_integration_api_fixture.py backend/tests/unit/deployment/test_deployment_contract.py -q` | `62 passed, 2 warnings` |
| `uv run pytest backend/tests/security/test_settings_secret_boundary.py backend/tests/unit/test_cli.py -q` | `105 passed, 1 warning` |
| `uv run pytest backend/tests/unit backend/tests/contract backend/tests/security -q` | `506 passed, 2 skipped, 2 warnings` |
| `uv run ruff check backend scripts` | passed |
| `uv run mypy backend/src/bidscope` | `Success: no issues found in 66 source files` |
| Production Compose `config -q` with TLS DSNs and sentinel model values | passed |
| Production DSN direct probe | accepted both allowlisted TLS queries; rejected malformed `postgresql+asyncpg://bare-secret`, wrong-scheme MySQL URL, and query-password override |
| `git diff --check` | passed before commit |
| Changed-diff credential signature scan | no credential signatures found |

The pytest runs emitted the existing LangGraph and Starlette/httpx dependency deprecation warnings.

## Environment Limitations

- A direct TCP probe to `127.0.0.1:5432` timed out. No PostgreSQL-backed runtime integration tests were run in this follow-up.
- Docker Compose rendering was available and verified, but Docker services were not started. No PostgreSQL/MinIO runtime smoke test was performed.
- The full integration suite remains for an environment with a reachable guarded `*_test` or `*_e2e` PostgreSQL target.
