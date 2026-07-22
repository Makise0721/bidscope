# BidScope Tasks 19–20 Handoff

**Date:** 2026-07-22  
**Purpose:** hand off the clean final-task worktree after completing Task 17 and Task 18.  
**Status:** Task 17 and Task 18 complete on this branch; Task 19 and Task 20 have not started.

## 1. Repository and worktree

- Repository: `C:\Users\29913\zcode_workspace\bidscope`
- Worktree: `C:\Users\29913\zcode_workspace\bidscope\.worktrees\bidscope-final-17-20`
- Branch: `feat/bidscope-final-17-20`
- Required baseline: `058d285aa0f56eaac2d9035d23f6fea75608dcb2`
- Final code HEAD at handoff: `84915b24c79182a3d75a07d1886cd76dca5330f8`
- `058d285` remains an ancestor of HEAD.
- Worktree was clean after the final verification.
- `main` was not modified, and nothing was pushed.
- The old `feat/bidscope-p0` worktree was not reused.

The final local commit sequence includes the independently committed Task 17 operational-view slices and the Task 18 evaluation slices. The fixed commit messages are preserved:

- `feat: add BidScope operational views`
- `feat: add reproducible BidScope evaluation`

## 2. Fixture and source-policy invariants

Every fresh checkout/worktree must run the manifest check before touching Tasks 19–20:

```bash
uv run python -c "import hashlib,json,sys; from pathlib import Path; mismatches=[]; manifests=sorted(Path('data').rglob('manifest.json')); [mismatches.append(f'{p}: {name}') for p in manifests for name,expected in json.loads(p.read_text(encoding='utf-8')).get('files',{}).items() if hashlib.sha256((p.parent/name).read_bytes()).hexdigest()!=expected]; print(f'manifests_scanned={len(manifests)}'); print(f'hash_mismatches={len(mismatches)}'); print(*mismatches,sep='\\n'); sys.exit(1 if mismatches else 0)"
```

Handoff result: `manifests_scanned=4`, `hash_mismatches=0`.

Do not reformat, rewrite, or line-ending-convert anything under `data/**`. Keep `.gitattributes` unchanged. P0 remains snapshot-only:

- No public tender website was accessed, fetched, probed, or published against.
- Synthetic evaluation records use `source=synthetic_demo`, `eval-*` IDs, and `https://example.invalid/` URLs.
- Official-source fixtures remain immutable byte artifacts.
- No migrations, schema changes, graph execution changes, or scheduler semantic changes were made in Tasks 17–18.

## 3. Task 17 completion summary

Operational backend routes and frontend views are implemented and tested:

- Runs history, server-side status filtering, bounded request previews, retry eligibility.
- Subscription pause/resume state guards, inbox event states and bounded messages.
- Source provenance, capture kind, retrieval time, age, hash/file identity, parser status, stale/invalid warnings.
- Evaluation cards that distinguish measured/target fields, with explicit fixture provenance.
- Strict frontend URL rendering: only exact HTTPS allowlisted official hosts become links; synthetic, lookalike, malformed, non-HTTPS, non-default-port, and userinfo URLs remain plain text.
- Existing workbench confirmation now sends the backend confirmation request before navigation.
- Report delivery rows preserve `run_id`.

Task 17 checks completed during the final pass:

```text
backend/tests/integration/api: 16 passed
web tests: 11 passed
web production build: passed
Ruff: passed
mypy: passed
fixture hashes: 4 manifests, 0 mismatches
git diff --check: passed
```

## 4. Task 18 completion summary

Committed evaluation system and deterministic offline runner:

- `scripts/build_eval_data.py`
- `backend/src/bidscope/evaluation/datasets.py`
- `backend/src/bidscope/evaluation/metrics.py`
- `backend/src/bidscope/evaluation/runner.py`
- CLI command:

```bash
uv run --offline bidscope eval run --mode deterministic --output eval/results/deterministic.json
```

Committed dataset counts:

| Dataset | Count |
|---|---:|
| `synthetic-notices-v1` corpus | 120 |
| `intent-v1` | 120 |
| `retrieval-v1` | 30 |
| `dedup-v1` | 120 |
| `claims-v1` | 60 |
| `e2e-v1` | 30 |

The evaluation package now has:

- LF-canonical JSONL bytes and pinned SHA-256 hashes.
- Checkout/package-resource fallback with byte parity.
- Wheel inclusion of all six approved JSONL resources.
- Closed-world top-level and nested schema validation.
- Integer minor-unit and token bounds.
- Timezone-aware ISO-8601 deadlines.
- Referential checks for corpus notices and case-scoped evidence/citations.
- A deliberate negative claim case: `eval-claim-060` uses `eval-evidence-missing` and `expected_supported=false`.
- Deterministic duplicate-aware Recall/nDCG and multiclass dedup metrics.
- Safe operation from a running event loop through the worker bridge.
- No implicit dataset regeneration and no network access.

Final Task 18 checks:

```text
backend/tests/unit/evaluation: 60 passed
related evaluation/graph/llm/retrieval/subscription suites: 131 passed
wheel packaging: passed
builder + approved-hash verification: passed
CLI deterministic run: completed, target_pass=true
Ruff: passed
mypy backend/src scripts: passed
git diff --check: passed
worktree: clean
```

The final deterministic result records:

- `git_commit=84915b24c79182a3d75a07d1886cd76dca5330f8`
- all dataset hashes/counts
- `provider=offline`, `model=fake-deterministic`
- pricing snapshot date `2026-07-18`
- database fixture version `synthetic-notices-v1`
- `network=disabled`
- targets, target results, P50/P95, tokens, cost
- `target_pass=true`

**Important evaluation disclosure:** every metric family is marked `fixture_consistency`. Intent, retrieval, dedup, claims, E2E, latency, tokens, and cost are not live production measurements. In particular, the deterministic labels are synthetic and some are generated to exercise known contracts. Do not present the result as measured production quality, live latency, or real model cost. The zero-cost result is an offline pricing fixture, not a billing measurement.

`eval/results/deterministic.json` is generated/ignored output and is not a committed release artifact. Re-run the CLI when provenance for a new HEAD is required.

## 5. Environment notes

Docker Desktop is currently available. The worktree PostgreSQL service was started successfully and was healthy for the Task 17 API gate. A `docker compose up -d postgres minio` attempt hit a host-port conflict on MinIO port `9000`; PostgreSQL was then started alone. Do not alter compose or port semantics as a shortcut. Before Task 20, explicitly resolve or document the MinIO port conflict.

The full backend suite is not a valid clean signal under the default shell environment because integration tests fail closed unless:

```bash
export BIDSCOPE_APP_MODE=test
export BIDSCOPE_DATABASE_URL=postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope_test
export BIDSCOPE_CHECKPOINT_DATABASE_URL=postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope_test
```

Use that environment for integration gates. Existing deprecation warnings from LangGraph, Starlette/httpx, and React Router are visible but were not hidden or treated as product failures.

## 6. Task 19 start instructions

Do not begin Task 20 early. Start Task 19 with a clean worktree and only a Task 19 RED. Read:

- `docs/superpowers/specs/2026-07-18-bidscope-design.md`
- `docs/superpowers/plans/2026-07-18-bidscope-implementation.md` (Task 19, lines 1123–1157)
- `docs/source-policy.md`
- this handoff

Task 19 approved scope:

- `backend/tests/security/test_snapshot_urls.py`
- `backend/tests/security/test_prompt_injection.py`
- `backend/tests/integration/test_partial_sources.py`
- `backend/tests/integration/test_idempotency.py`
- `backend/tests/integration/test_failure_recovery.py`
- only the minimal guards exposed by valid failing tests.

Required coverage includes:

- HTTPS/host/userinfo/lookalike/traversal/changed-hash/undeclared-file checks.
- CAPTCHA/session artifacts, unsafe DOCX filenames, raw HTML rendering, arbitrary tools, SQL-like plans, and prompt injection.
- One stale source plus one valid source; one parse-invalid plus one valid source.
- Vector-provider degradation, model transient retry, evidence retry, DOCX failure after online success, stale-running startup recovery.
- Bounded error union and completeness warnings for partial reports.
- Demo mode remains network-free.

Task 19 target gate:

```bash
uv run pytest backend/tests/security \
  backend/tests/integration/test_partial_sources.py \
  backend/tests/integration/test_idempotency.py \
  backend/tests/integration/test_failure_recovery.py -q
```

Follow the required RED → minimal GREEN → target gate → specification review → code-quality review → independent commit protocol. Do not modify `data/**`, migrations, scheduler semantics, or graph semantics unless a Task 19 failing test proves an additive guard is necessary.

## 7. Task 20 start instructions

Task 20 must begin only after Task 19 is independently committed and clean. It is the final integration/deployment task:

- `Dockerfile`
- `compose.yaml` additions only as approved by the plan
- Playwright config and six flows
- `docs/evaluation.md`
- `docs/deployment.md`
- `README.md`
- `.github/workflows/ci.yml`

Before Task 20, re-run the stable Task 1–16 baseline gates and confirm the Task 17–19 worktrees are clean. The current root `package.json` still contains an intentionally disabled E2E placeholder; Task 20 must replace/implement that path rather than treating the placeholder as E2E evidence.

Task 20 must preserve:

- exactly one scheduler service;
- non-root image execution;
- separate `bidscope api` and `bidscope scheduler` roles;
- `bidscope` and `bidscope_e2e` database initialization;
- test-only controls unavailable in demo/development/production;
- snapshot-only/no-public-site-fetch boundary;
- persistent synthetic-data labeling.

## 8. Stop conditions

Stop and report rather than guessing if:

- any manifest hash mismatches;
- a plan/spec conflict appears;
- Docker or Playwright is unavailable;
- continuing requires public-site access;
- a migration/schema change appears necessary;
- a fixture must be reformatted or rewritten;
- a Task 19/20 test exposes a frozen graph/scheduler semantic conflict.

P0 is **not** complete at this handoff. Tasks 19 and 20 remain outstanding.
