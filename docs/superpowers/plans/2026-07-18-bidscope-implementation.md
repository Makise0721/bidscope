# BidScope P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy an evidence-first tender intelligence Agent that imports auditable official-source snapshots, runs a recoverable LangGraph query workflow, produces cited Web and DOCX reports, schedules incremental subscriptions, and publishes reproducible quality metrics.

**Architecture:** BidScope is a Python/FastAPI application with a bounded LangGraph workflow, PostgreSQL/pgvector persistence, explicit snapshot adapters, and a React workbench. Interactive runs never fetch public tender sites: source bundles are imported through a validated administrative CLI, while Agent runs operate on immutable notice versions and evidence spans. Model, embedding, object-storage, clock, and snapshot-adapter boundaries are injected so the full system can run deterministically in tests and public demo mode.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic, LangGraph, SQLAlchemy/Alembic, PostgreSQL/pgvector, APScheduler, DeepSeek through an OpenAI-compatible client, React 19, TypeScript, Vite, TanStack Query, Vitest, Playwright, Docker Compose, MinIO/S3, pytest, Ruff, mypy.

---

## File Structure

```text
bidscope/
├── pyproject.toml                         # Python package, commands, lint/type/test config
├── uv.lock                               # Locked Python dependencies
├── package.json                          # Root scripts delegating to Web and E2E
├── package-lock.json                     # Locked Node dependencies
├── .env.example                          # Non-secret configuration contract
├── .gitignore
├── compose.yaml                          # PostgreSQL/pgvector and MinIO for development
├── Dockerfile                            # Multi-stage Web + Python production image
├── alembic.ini
├── migrations/
│   ├── env.py
│   └── versions/0001_initial.py           # P0 relational schema and pgvector extensions
├── backend/
│   ├── src/bidscope/
│   │   ├── __init__.py
│   │   ├── cli.py                        # db, checkpoint, snapshot, evaluation commands
│   │   ├── config.py                     # Pydantic settings and startup validation
│   │   ├── clock.py                      # Injectable system/fixed clock
│   │   ├── db.py                         # Async SQLAlchemy engine/session lifecycle
│   │   ├── main.py                       # FastAPI app factory and static SPA mount
│   │   ├── api/
│   │   │   ├── dependencies.py           # Session, services, optional admin-token dependency
│   │   │   └── routes/
│   │   │       ├── health.py
│   │   │       ├── runs.py               # create, inspect, confirm, retry, SSE events
│   │   │       ├── reports.py            # report JSON and DOCX download
│   │   │       ├── subscriptions.py
│   │   │       ├── sources.py
│   │   │       ├── evaluations.py
│   │   │       └── test_controls.py       # Registered only in app_mode=test
│   │   ├── domain/
│   │   │   ├── enums.py                  # Source, capture, run, import, inbox enums
│   │   │   ├── snapshots.py              # Manifest and adapter contracts
│   │   │   ├── notices.py                # Normalized notice, version, evidence contracts
│   │   │   ├── intents.py                # SearchIntent and confirmation contracts
│   │   │   ├── reports.py                # Report, claim, citation contracts
│   │   │   └── runs.py                   # Run event and serializable error contracts
│   │   ├── persistence/
│   │   │   ├── models.py                 # SQLAlchemy models only
│   │   │   ├── repositories.py           # Notice, run, report, subscription repositories
│   │   │   └── unit_of_work.py            # Transaction boundary
│   │   ├── snapshots/
│   │   │   ├── adapters.py               # Adapter registry and shared helpers
│   │   │   ├── ccgp.py                   # 中国政府采购网 fixture parser
│   │   │   ├── ggzy.py                   # 全国公共资源交易平台 fixture parser
│   │   │   ├── demo.py                   # Explicit synthetic-demo JSON adapter
│   │   │   └── importer.py               # Integrity checks and idempotent imports
│   │   ├── retrieval/
│   │   │   ├── embeddings.py             # Deterministic and OpenAI-compatible providers
│   │   │   ├── search.py                 # Structured + trigram + vector retrieval
│   │   │   └── deduplication.py           # Exact candidates and bounded semantic decisions
│   │   ├── llm/
│   │   │   ├── ports.py                  # Intent, duplicate, and report model protocols
│   │   │   ├── fake.py                   # Deterministic public-demo/test implementation
│   │   │   └── deepseek.py               # Structured OpenAI-compatible implementation
│   │   ├── graph/
│   │   │   ├── state.py                  # Versioned Pydantic RunState
│   │   │   ├── nodes.py                  # Ten bounded graph nodes
│   │   │   ├── builder.py                # Edges, checkpointer, recursion limit
│   │   │   └── executor.py               # Stream persistence, resume, retry, startup recovery
│   │   ├── evidence/
│   │   │   ├── extractor.py              # Immutable evidence-span construction
│   │   │   └── validator.py              # Claim/citation/version validation
│   │   ├── delivery/
│   │   │   ├── docx.py                   # DOCX renderer from typed Report
│   │   │   └── objects.py                # Local and S3-compatible object-store ports
│   │   ├── subscriptions/
│   │   │   ├── scheduler.py              # APScheduler process role
│   │   │   └── service.py                # Advisory lock, seen set, inbox events
│   │   └── evaluation/
│   │       ├── datasets.py                # Versioned JSONL schemas and loading
│   │       ├── metrics.py                 # EM/F1/Recall/nDCG/citation/success metrics
│   │       └── runner.py                  # Deterministic and live-model evaluation runs
│   └── tests/
│       ├── unit/                          # Pure domain and graph tests
│       ├── contract/                      # Snapshot adapter fixture tests
│       └── integration/                   # Real PostgreSQL/checkpoint/API tests
├── data/
│   ├── snapshots/                         # One audited excerpt bundle per official source
│   └── demo/                              # Explicit synthetic Batch 1 and Batch 2 bundles
├── eval/
│   ├── corpus/                            # Synthetic notices used only by evaluation
│   ├── data/                              # Versioned intent/retrieval/dedup/claim/e2e JSONL
│   └── results/.gitkeep                   # Generated result location; result JSON ignored
├── scripts/
│   └── build_eval_data.py                 # Deterministic minimum-size dataset builder
├── web/
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── app/App.tsx                    # Router and top-level error/loading states
│   │   ├── api/client.ts                  # Typed fetch and SSE client
│   │   ├── features/workbench/            # Query, intent, report, evidence, timeline
│   │   ├── features/runs/                 # Run history and retry
│   │   ├── features/subscriptions/        # Schedules and inbox
│   │   ├── features/sources/              # Snapshot provenance and freshness
│   │   ├── features/evaluation/           # Quality/latency/cost view
│   │   └── styles/                        # Tokens, layout, responsive states
│   └── tests/
├── e2e/
│   ├── playwright.config.ts
│   └── specs/main-flow.spec.ts
├── docs/
│   ├── source-policy.md                   # Verified source facts and snapshot-only boundary
│   ├── evaluation.md                      # Dataset and metric methodology
│   ├── deployment.md
│   └── superpowers/                       # Approved design and this plan
└── README.md
```

## Milestone 1: Reproducible Foundation

### Task 1: Bootstrap the Repository and Health Slice

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `compose.yaml`
- Create: `backend/src/bidscope/__init__.py`
- Create: `backend/src/bidscope/config.py`
- Create: `backend/src/bidscope/clock.py`
- Create: `backend/src/bidscope/main.py`
- Create: `backend/tests/unit/test_clock.py`
- Create: `backend/tests/unit/test_health.py`

- [ ] **Step 1: Write the failing health test**

```python
from fastapi.testclient import TestClient
from bidscope.main import create_app


def test_health_reports_demo_mode() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "demo"}
```

Also write `test_clock.py` to assert `FixedClock(datetime(2026, 7, 18, tzinfo=UTC)).now()` always returns that exact value and `SystemClock.now()` is timezone-aware.

- [ ] **Step 2: Create the Python project and lock dependencies**

Define Python `>=3.12,<3.13`, package path `backend/src`, and dependencies for FastAPI, uvicorn, Pydantic settings, SQLAlchemy async, asyncpg, psycopg pool, Alembic, pgvector, LangGraph, LangGraph PostgreSQL checkpoint, langchain-openai, HTTPX, selectolax, APScheduler 3.x, python-docx, boto3, RapidFuzz, Typer, and sse-starlette. Define development groups for pytest, pytest-asyncio, pytest-cov, Ruff, mypy, and testcontainers. Run:

```bash
uv lock
uv sync --all-groups
uv run pytest backend/tests/unit/test_clock.py backend/tests/unit/test_health.py -q
```

Expected: first test run fails because `bidscope.main` does not exist; after the minimal app factory is added it reports `1 passed`.

- [ ] **Step 3: Implement settings, injectable clock, and the minimal app factory**

```python
# backend/src/bidscope/config.py
from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIDSCOPE_", env_file=".env", extra="ignore")
    app_mode: Literal["demo", "development", "production", "test"] = "demo"
    database_url: str = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope"
    checkpoint_database_url: str = "postgresql://bidscope:bidscope@localhost:5432/bidscope"
    real_model_enabled: bool = False
    admin_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# backend/src/bidscope/clock.py
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("fixed clock requires a timezone-aware value")
        self.value = value

    def now(self) -> datetime:
        return self.value
```

All later services receive a `Clock`; application code must not call `datetime.now()` directly.

```python
# backend/src/bidscope/main.py
from fastapi import FastAPI
from bidscope.config import get_settings


def create_app() -> FastAPI:
    app = FastAPI(title="BidScope", version="0.1.0")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": get_settings().app_mode}

    return app


app = create_app()
```

- [ ] **Step 4: Add local services and quality commands**

`compose.yaml` must define `pgvector/pgvector:pg17` with healthcheck and `minio/minio` with a persistent volume. Configure Ruff at line length 100, mypy strict mode for `bidscope`, and pytest `asyncio_mode = "auto"`. Run:

```bash
uv run ruff check backend
uv run mypy backend/src/bidscope
uv run pytest backend/tests/unit/test_clock.py backend/tests/unit/test_health.py -q
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit the foundation**

```bash
git add pyproject.toml uv.lock .env.example .gitignore compose.yaml backend
git commit -m "chore: bootstrap BidScope backend"
```

### Task 2: Define Typed Domain Contracts

**Files:**
- Create: `backend/src/bidscope/domain/enums.py`
- Create: `backend/src/bidscope/domain/snapshots.py`
- Create: `backend/src/bidscope/domain/notices.py`
- Create: `backend/src/bidscope/domain/intents.py`
- Create: `backend/src/bidscope/domain/reports.py`
- Create: `backend/src/bidscope/domain/runs.py`
- Create: `backend/tests/unit/domain/test_contracts.py`

- [ ] **Step 1: Write contract tests for money, manifests, intent, and citations**

```python
from datetime import UTC, datetime
from pydantic import ValidationError
from bidscope.domain.enums import CaptureKind, SourceName
from bidscope.domain.snapshots import SnapshotManifest


def valid_manifest() -> dict:
    return {
        "schema_version": 1,
        "bundle_id": "ccgp-central-20260718",
        "source": SourceName.CCGP,
        "capture_kind": CaptureKind.CURATED_PUBLIC_EXCERPT,
        "source_urls": ["https://www.ccgp.gov.cn/cggg/zygg/gkzb/202607/example.htm"],
        "retrieved_at": datetime(2026, 7, 18, tzinfo=UTC),
        "retrieval_outcome": "waf_blocked_after_public_verification",
        "parser_version": "ccgp-v1",
        "files": {"detail.html": "a" * 64, "expected.json": "b" * 64},
    }


def test_manifest_rejects_non_https_source() -> None:
    data = valid_manifest() | {"source_urls": ["http://example.com"]}
    try:
        SnapshotManifest.model_validate(data)
    except ValidationError as error:
        assert "https" in str(error).lower()
    else:
        raise AssertionError("manifest accepted a non-HTTPS source")
```

Add tests that money uses integer minor units, `SearchIntent` rejects inverted dates and budgets, and a `ReportClaim` cannot omit citation IDs.

- [ ] **Step 2: Run tests and verify the contracts are missing**

```bash
uv run pytest backend/tests/unit/domain/test_contracts.py -q
```

Expected: collection fails with `ModuleNotFoundError: bidscope.domain`.

- [ ] **Step 3: Implement the Pydantic contracts**

Use string enums for `SourceName`, `CaptureKind`, `RunStatus`, `ImportStatus`, and `InboxEventType`; include `SourceName.SYNTHETIC_DEMO` and `CaptureKind.SYNTHETIC_DEMO` so provenance cannot be collapsed into an official source. Implement `SnapshotManifest` with an HTTPS validator, `Money(minor_units, currency, raw_text)`, `NormalizedNotice`, `NoticeEvidence`, `SearchIntent`, `RetrievalPlan`, `ReportCitation`, `ReportClaim`, `ReportItem`, `Report`, `RunEvent`, and the serializable error union. All timestamps must be timezone-aware; unknown source fields remain `None` and retain `raw_fields`.

- [ ] **Step 4: Run contract and static checks**

```bash
uv run pytest backend/tests/unit/domain/test_contracts.py -q
uv run ruff check backend/src/bidscope/domain backend/tests/unit/domain
uv run mypy backend/src/bidscope/domain
```

Expected: all commands exit `0`.

- [ ] **Step 5: Commit domain contracts**

```bash
git add backend/src/bidscope/domain backend/tests/unit/domain
git commit -m "feat: define BidScope domain contracts"
```

### Task 3: Add PostgreSQL Schema and Transaction Boundary

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_initial.py`
- Create: `backend/src/bidscope/db.py`
- Create: `backend/src/bidscope/persistence/models.py`
- Create: `backend/src/bidscope/persistence/unit_of_work.py`
- Create: `backend/tests/integration/conftest.py`
- Create: `backend/tests/integration/test_migrations.py`
- Create: `backend/tests/integration/test_unit_of_work.py`

- [ ] **Step 1: Write a migration smoke test**

```python
import sqlalchemy as sa


async def test_initial_migration_creates_core_tables(db_engine) -> None:
    async with db_engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: sa.inspect(sync).get_table_names()
        )
    assert {
        "snapshot_bundles", "snapshot_imports", "source_notices", "notice_versions",
        "canonical_notices", "notice_evidence", "query_runs", "run_events",
        "reports", "report_items", "subscriptions", "subscription_seen_items",
        "inbox_events", "eval_runs",
    } <= set(names)
```

- [ ] **Step 2: Start PostgreSQL and verify the test fails before migration**

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run pytest backend/tests/integration/test_migrations.py -q
```

Expected: the first pre-implementation attempt fails because Alembic configuration is absent; after migration implementation the test passes.

- [ ] **Step 3: Implement models and the initial migration**

Use UUID primary keys, timezone-aware timestamps, JSONB for bounded source-specific fields, `VECTOR(1024)` for notice embeddings, unique constraints on `(source, external_id)`, `(source_notice_id, content_hash)`, import idempotency key, run idempotency key, subscription trigger key, and report export key. Enable `vector` and `pg_trgm` in the migration. `UnitOfWork` must commit only on successful exit and roll back on exceptions.

- [ ] **Step 4: Test rollback and uniqueness**

Add integration tests proving a failed transaction leaves no partial snapshot import and importing the same idempotency key raises a repository-level conflict instead of duplicating rows. Run:

```bash
uv run pytest backend/tests/integration/test_migrations.py backend/tests/integration/test_unit_of_work.py -q
```

Expected: all tests pass against the Compose PostgreSQL instance.

- [ ] **Step 5: Commit persistence foundation**

```bash
git add alembic.ini migrations backend/src/bidscope/db.py backend/src/bidscope/persistence backend/tests/integration
git commit -m "feat: add PostgreSQL persistence foundation"
```

## Milestone 2: Auditable Snapshot Ingestion

### Task 4: Implement Snapshot Bundle Integrity and Object Storage

**Files:**
- Create: `backend/src/bidscope/delivery/objects.py`
- Create: `backend/src/bidscope/snapshots/adapters.py`
- Create: `backend/tests/unit/snapshots/test_bundle_integrity.py`
- Create: `backend/tests/unit/delivery/test_objects.py`

- [ ] **Step 1: Write failing hash and provenance tests**

```python
import hashlib
from pathlib import Path
from bidscope.snapshots.adapters import inspect_bundle


def test_inspect_bundle_rejects_modified_file(snapshot_bundle: Path) -> None:
    detail = snapshot_bundle / "detail.html"
    detail.write_text("changed", encoding="utf-8")
    inspection = inspect_bundle(snapshot_bundle)
    assert inspection.valid is False
    assert inspection.errors[0].code == "snapshot_integrity_error"


def test_fixture_hash_is_sha256(snapshot_bundle: Path) -> None:
    payload = (snapshot_bundle / "detail.html").read_bytes()
    assert hashlib.sha256(payload).hexdigest() == inspect_bundle(snapshot_bundle).actual_hashes["detail.html"]
```

- [ ] **Step 2: Run tests and verify missing implementation**

```bash
uv run pytest backend/tests/unit/snapshots/test_bundle_integrity.py backend/tests/unit/delivery/test_objects.py -q
```

Expected: tests fail because the bundle inspector and object stores do not exist.

- [ ] **Step 3: Implement bundle inspection and object-store ports**

`inspect_bundle(path)` must parse `manifest.json`, reject paths outside the bundle, verify every declared SHA-256, reject undeclared payload files, and return typed diagnostics. Official capture kinds allow only `{www.ccgp.gov.cn, search.ccgp.gov.cn, www.ggzy.gov.cn}`; `synthetic_demo` allows only `example.invalid` and requires `source=synthetic_demo`. Implement `LocalObjectStore` with atomic temp-file rename and `S3ObjectStore` using configured bucket/key names; both expose `put_bytes`, `get_bytes`, and `exists`.

- [ ] **Step 4: Verify integrity and object parity**

```bash
uv run pytest backend/tests/unit/snapshots backend/tests/unit/delivery/test_objects.py -q
uv run ruff check backend/src/bidscope/snapshots backend/src/bidscope/delivery
```

Expected: all tests pass.

- [ ] **Step 5: Commit snapshot integrity support**

```bash
git add backend/src/bidscope/snapshots backend/src/bidscope/delivery/objects.py backend/tests/unit/snapshots backend/tests/unit/delivery
git commit -m "feat: validate snapshot bundles and object storage"
```

### Task 5: Add Official and Synthetic Snapshot Adapters

**Files:**
- Create: `backend/src/bidscope/snapshots/ccgp.py`
- Create: `backend/src/bidscope/snapshots/ggzy.py`
- Create: `backend/src/bidscope/snapshots/demo.py`
- Create: `data/snapshots/ccgp/2026-07-18-central-open/*`
- Create: `data/snapshots/ggzy/2026-07-18-construction/*`
- Create: `data/demo/batch-1/*`
- Create: `data/demo/batch-2/*`
- Create: `backend/tests/contract/test_ccgp_adapter.py`
- Create: `backend/tests/contract/test_ggzy_adapter.py`
- Create: `backend/tests/contract/test_demo_adapter.py`
- Create: `docs/source-policy.md`

- [ ] **Step 1: Create explicit curated-excerpt manifests and expected records**

Each official manifest declares `capture_kind: curated_public_excerpt`, its verified official URL, retrieval date, `retrieval_outcome`, parser version, payload hashes, and no credentials. The CCGP bundle contains the one publicly verified central open-tender sample; the GGZY bundle contains the one publicly verified list/detail sample. Their expected JSON covers only fields confirmed during source research.

The separate demo manifests declare `capture_kind: synthetic_demo`, `source: synthetic_demo`, reserved `demo-*` IDs, and `https://example.invalid/` URLs. Records may include a `synthetic_channel` such as `channel_a` or `channel_b` to exercise cross-channel deduplication, but that field cannot change their source identity. Batch 1 includes at least twelve notices so the representative query returns matched and non-matched records. Batch 2 includes at least two new notices, two materially changed notices, and two unchanged notices for subscription tests. The demo adapter parses normalized JSON rather than imitating official HTML. Every UI/report path labels these records as synthetic.

- [ ] **Step 2: Write adapter contract tests**

```python
from pathlib import Path
from bidscope.snapshots.ccgp import CcgpSnapshotAdapter


def test_ccgp_fixture_matches_human_reviewed_record(project_root: Path) -> None:
    bundle = project_root / "data/snapshots/ccgp/2026-07-18-central-open"
    adapter = CcgpSnapshotAdapter()
    actual = adapter.parse(bundle)
    expected = adapter.load_expected(bundle)
    assert [item.model_dump(mode="json") for item in actual] == expected
    assert actual[0].source_url.host == "www.ccgp.gov.cn"
```

Write the equivalent GGZY test and a drift test where a required title element is removed and a `ParseDrift` diagnostic is returned. Add a demo-adapter test that rejects any non-`example.invalid` URL or ID without the `demo-` prefix and proves Batch 2 contains the required new, materially changed, and unchanged cases.

- [ ] **Step 3: Implement deterministic parsers**

Use selectolax for HTML and standard `json` for list data. Parse only observed fields; keep missing values as `None`; store unrecognized labels in `raw_fields`; normalize whitespace, timezone, amount, and URLs in shared helpers. Do not request any URL in adapters or tests.

- [ ] **Step 4: Run offline contract tests with network disabled**

```bash
uv run pytest backend/tests/contract -q
```

Expected: all tests pass without PostgreSQL, model keys, or Internet access.

- [ ] **Step 5: Document and commit source provenance**

`docs/source-policy.md` must record the official entry URLs, observed page/interface behavior, CCGP WAF response, GGZY CAPTCHA/result cap, unknown robots permission, snapshot evidence levels, and prohibition on automated live fetching in P0.

```bash
git add backend/src/bidscope/snapshots data/snapshots data/demo backend/tests/contract docs/source-policy.md
git commit -m "feat: add audited tender source snapshot adapters"
```

### Task 6: Import Snapshots Idempotently and Preserve Versions

**Files:**
- Create: `backend/src/bidscope/persistence/repositories.py`
- Create: `backend/src/bidscope/snapshots/importer.py`
- Create: `backend/src/bidscope/cli.py`
- Create: `backend/tests/integration/test_snapshot_import.py`

- [ ] **Step 1: Write an end-to-end import test**

```python
async def test_reimport_is_idempotent(importer, ccgp_bundle, session) -> None:
    first = await importer.import_bundle(ccgp_bundle)
    second = await importer.import_bundle(ccgp_bundle)
    assert first.import_id == second.import_id
    assert await count_rows(session, "source_notices") == 1
    assert await count_rows(session, "notice_versions") == 1
```

Add a second-batch test proving a changed content hash creates a new immutable version while keeping one logical `source_notice`.

- [ ] **Step 2: Run the import test and observe failure**

```bash
uv run pytest backend/tests/integration/test_snapshot_import.py -q
```

Expected: failure because `SnapshotImporter` is missing.

- [ ] **Step 3: Implement repositories, import transaction, and CLI**

The importer must inspect the bundle before opening writes, persist payload objects, create or reuse the source notice, append only changed versions, create evidence rows from normalized source fields, and mark the import successful only after all records commit. Expose:

```bash
uv run bidscope snapshots inspect data/snapshots/ccgp/2026-07-18-central-open
uv run bidscope snapshots import data/snapshots/ccgp/2026-07-18-central-open
```

Both commands print machine-readable JSON when `--json` is supplied.

- [ ] **Step 4: Verify rollback, idempotency, and CLI behavior**

```bash
uv run pytest backend/tests/integration/test_snapshot_import.py -q
uv run bidscope snapshots inspect data/snapshots/ccgp/2026-07-18-central-open --json
```

Expected: tests pass and CLI output includes `"valid": true` and the bundle ID.

- [ ] **Step 5: Commit snapshot ingestion**

```bash
git add backend/src/bidscope/persistence/repositories.py backend/src/bidscope/snapshots/importer.py backend/src/bidscope/cli.py backend/tests/integration/test_snapshot_import.py
git commit -m "feat: import versioned tender snapshots"
```

## Milestone 3: Retrieval, Deduplication, and Model Ports

### Task 7: Implement Structured, Lexical, and Vector Retrieval

**Files:**
- Create: `backend/src/bidscope/retrieval/embeddings.py`
- Create: `backend/src/bidscope/retrieval/search.py`
- Create: `backend/tests/unit/retrieval/test_embeddings.py`
- Create: `backend/tests/integration/test_hybrid_search.py`

- [ ] **Step 1: Write deterministic embedding and ranking tests**

```python
from bidscope.retrieval.embeddings import HashEmbeddingProvider


async def test_hash_embeddings_are_stable_and_normalized() -> None:
    provider = HashEmbeddingProvider(dimension=1024)
    first = await provider.embed(["GPU 服务器采购"])
    second = await provider.embed(["GPU 服务器采购"])
    assert first == second
    assert len(first[0]) == 1024
    assert abs(sum(value * value for value in first[0]) - 1.0) < 1e-6
```

The integration test inserts notices with different regions, budgets, dates, and vectors, then asserts Sichuan/Chongqing and CNY 5,000,000 filters are applied before reciprocal-rank fusion.

- [ ] **Step 2: Run tests and verify failure**

```bash
uv run pytest backend/tests/unit/retrieval backend/tests/integration/test_hybrid_search.py -q
```

Expected: missing retrieval modules.

- [ ] **Step 3: Implement providers and hybrid search**

Implement `HashEmbeddingProvider` for tests/public demo and `OpenAICompatibleEmbeddingProvider` for configured deployments. PostgreSQL retrieval uses deterministic filters, `pg_trgm` title/body similarity, pgvector cosine distance, and reciprocal-rank fusion with configured weights. If embedding fails or is disabled, return lexical results with `degraded_modes=["vector_unavailable"]`.

- [ ] **Step 4: Verify retrieval and degradation**

```bash
uv run pytest backend/tests/unit/retrieval backend/tests/integration/test_hybrid_search.py -q
```

Expected: filtered hybrid ranking and lexical-only degradation tests pass.

- [ ] **Step 5: Commit retrieval**

```bash
git add backend/src/bidscope/retrieval backend/tests/unit/retrieval backend/tests/integration/test_hybrid_search.py
git commit -m "feat: add hybrid tender retrieval"
```

### Task 8: Add Deterministic Deduplication and Material-Change Detection

**Files:**
- Create: `backend/src/bidscope/retrieval/deduplication.py`
- Create: `backend/tests/unit/retrieval/test_deduplication.py`
- Create: `backend/tests/unit/retrieval/test_material_changes.py`

- [ ] **Step 1: Write failing exact, ambiguous, and change tests**

```python
def test_same_project_number_is_exact_duplicate() -> None:
    decision = classify_exact_pair(notice(project_number="SC-2026-9"), notice(project_number="SC-2026-9"))
    assert decision.kind == "exact"


def test_formatting_only_change_is_not_material() -> None:
    assert detect_material_changes(old_notice(), old_notice(title="  Same title  ")) == []


def test_deadline_change_is_material() -> None:
    changes = detect_material_changes(old_notice(), old_notice(deadline="2026-08-10T09:00:00+08:00"))
    assert [change.field for change in changes] == ["deadline"]
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest backend/tests/unit/retrieval/test_deduplication.py backend/tests/unit/retrieval/test_material_changes.py -q
```

Expected: missing functions.

- [ ] **Step 3: Implement bounded candidate generation**

Use project number, canonical source URL, purchaser, normalized title, amount, publication date, and SimHash/content hash. Return `exact`, `distinct`, or `ambiguous`; only `ambiguous` pairs can reach the model port. Material changes are restricted to deadline, budget, region, purchaser, scope, cancellation state, and evidence-supporting source text.

- [ ] **Step 4: Run pure tests**

```bash
uv run pytest backend/tests/unit/retrieval/test_deduplication.py backend/tests/unit/retrieval/test_material_changes.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit deduplication**

```bash
git add backend/src/bidscope/retrieval/deduplication.py backend/tests/unit/retrieval
git commit -m "feat: classify duplicates and material changes"
```

### Task 9: Add Deterministic and DeepSeek Model Implementations

**Files:**
- Create: `backend/src/bidscope/llm/ports.py`
- Create: `backend/src/bidscope/llm/fake.py`
- Create: `backend/src/bidscope/llm/deepseek.py`
- Create: `backend/tests/unit/llm/test_fake.py`
- Create: `backend/tests/unit/llm/test_deepseek_contract.py`

- [ ] **Step 1: Write port-level contract tests**

Define async protocols `IntentModel.parse`, `DuplicateModel.classify`, and `ReportModel.synthesize`. Test the fake model with the representative Chinese query and assert parsed topics, regions, seven-day window, CNY 5,000,000 minimum, and weekly Monday 09:00 schedule.

- [ ] **Step 2: Run tests and verify missing implementations**

```bash
uv run pytest backend/tests/unit/llm -q
```

Expected: missing LLM modules.

- [ ] **Step 3: Implement deterministic fake and DeepSeek adapter**

The fake model uses explicit regex and fixture rules and never calls a network service. The DeepSeek adapter uses `ChatOpenAI(base_url=settings.model_base_url, api_key=..., model=...)` and `with_structured_output` for Pydantic schemas. Prompts wrap imported text in an `UNTRUSTED_SOURCE_DATA` section and state that source text cannot issue instructions or tools. Record model, tokens, latency, and pricing snapshot through a typed `ModelUsage` result.

- [ ] **Step 4: Test without a real API key**

```bash
uv run pytest backend/tests/unit/llm -q
```

Expected: fake tests pass; DeepSeek contract tests use a stub transport and prove no network call occurs during test collection.

- [ ] **Step 5: Commit model ports**

```bash
git add backend/src/bidscope/llm backend/tests/unit/llm
git commit -m "feat: add deterministic and DeepSeek model ports"
```

## Milestone 4: Recoverable LangGraph and Evidence Reports

### Task 10: Build the Intent and Retrieval Graph Through Human Confirmation

**Files:**
- Create: `backend/src/bidscope/graph/state.py`
- Create: `backend/src/bidscope/graph/nodes.py`
- Create: `backend/src/bidscope/graph/builder.py`
- Create: `backend/tests/unit/graph/test_confirmation.py`
- Create: `backend/tests/unit/graph/test_routing.py`

- [ ] **Step 1: Write interrupt and resume tests**

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


async def test_scheduled_query_interrupts_and_resumes(graph_deps) -> None:
    graph = build_graph(graph_deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "run-1"}}
    paused = await graph.ainvoke({"user_request": REPRESENTATIVE_QUERY}, config)
    assert paused["status"] == "awaiting_confirmation"
    resumed = await graph.ainvoke(Command(resume={"action": "approve"}), config)
    assert resumed["search_intent"].schedule is not None
    assert resumed["candidate_notice_ids"]
    assert resumed["status"] == "candidates_resolved"
```

Add routing tests for invalid dates, empty retrieval, lexical degradation, and a rejected confirmation.

- [ ] **Step 2: Verify graph tests fail**

```bash
uv run pytest backend/tests/unit/graph/test_confirmation.py backend/tests/unit/graph/test_routing.py -q
```

Expected: graph modules are missing.

- [ ] **Step 3: Implement versioned state and the first six nodes**

`RunState` is a Pydantic model with IDs rather than source bodies. Implement `parse_intent`, `validate_intent`, `confirm_intent`, `build_retrieval_plan`, `retrieve_candidates`, and `resolve_duplicates`. `confirm_intent` calls `interrupt()` for every scheduled query and any low-confidence/conflicting required field. Compile with a supplied checkpointer and `recursion_limit=16`.

- [ ] **Step 4: Run graph tests**

```bash
uv run pytest backend/tests/unit/graph/test_confirmation.py backend/tests/unit/graph/test_routing.py -q
```

Expected: all graph routing tests pass with fake ports.

- [ ] **Step 5: Commit the recoverable graph slice**

```bash
git add backend/src/bidscope/graph backend/tests/unit/graph
git commit -m "feat: add confirmable LangGraph query workflow"
```

### Task 11: Extract Evidence, Synthesize Reports, and Reject Unsupported Claims

**Files:**
- Create: `backend/src/bidscope/evidence/extractor.py`
- Create: `backend/src/bidscope/evidence/validator.py`
- Modify: `backend/src/bidscope/graph/nodes.py`
- Modify: `backend/src/bidscope/graph/builder.py`
- Create: `backend/tests/unit/evidence/test_validator.py`
- Create: `backend/tests/unit/graph/test_report_retry.py`

- [ ] **Step 1: Write claim validation tests**

```python
def test_claim_must_reference_same_notice_version() -> None:
    result = validate_claim(
        claim=claim(citation_ids=["evidence-old"]),
        item_version_id="version-new",
        evidence_by_id={"evidence-old": evidence(version_id="version-old")},
    )
    assert result.valid is False
    assert result.errors == ["citation_version_mismatch"]
```

Add tests for missing evidence, invalid character offsets, changed span hashes, and a report retry that invokes synthesis once more but does not repeat retrieval.

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest backend/tests/unit/evidence backend/tests/unit/graph/test_report_retry.py -q
```

Expected: evidence modules are missing.

- [ ] **Step 3: Implement the final four graph nodes**

Implement immutable evidence extraction, `verify_evidence`, `synthesize_report`, `validate_report`, and `persist_and_deliver`. Unsupported fields become unknown or are removed. Validation checks evidence existence, notice-version equality, offsets, span hash, source URL, and all claim citations. A validation failure routes once to synthesis; a second failure returns `EvidenceInsufficient` without an unsupported report.

- [ ] **Step 4: Verify evidence and retry behavior**

```bash
uv run pytest backend/tests/unit/evidence backend/tests/unit/graph -q
```

Expected: all tests pass and the retry test proves retrieval call count remains one.

- [ ] **Step 5: Commit evidence-first reporting**

```bash
git add backend/src/bidscope/evidence backend/src/bidscope/graph backend/tests/unit/evidence backend/tests/unit/graph
git commit -m "feat: enforce evidence-backed Agent reports"
```

### Task 12: Add PostgreSQL Checkpoints and Run Event Persistence

**Files:**
- Create: `backend/src/bidscope/graph/executor.py`
- Modify: `backend/src/bidscope/persistence/repositories.py`
- Create: `backend/tests/integration/test_graph_persistence.py`
- Create: `backend/tests/integration/test_run_recovery.py`

- [ ] **Step 1: Write persistence and recovery tests**

Create a run, execute to `interrupt`, dispose the first graph/checkpointer context, construct a second `AsyncPostgresSaver`, and resume with `Command(resume=...)` using `thread_id=run_id`. Assert successful upstream node events are not duplicated and an injected synthesis failure resumes from synthesis.

- [ ] **Step 2: Run the tests and verify failure**

```bash
uv run pytest backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py -q
```

Expected: executor and persistent checkpointer wiring are missing.

- [ ] **Step 3: Implement executor and checkpoint setup**

Use `AsyncPostgresSaver.from_conn_string(settings.checkpoint_database_url)`, run `await checkpointer.setup()` only from `uv run bidscope checkpoints setup`, and compile with `thread_id=str(run_id)`. Persist bounded custom/update events in sequence order. On startup, mark stale `running` rows as `retryable`; keep checkpoints intact for explicit retry.

- [ ] **Step 4: Verify cross-instance resume**

```bash
uv run bidscope checkpoints setup
uv run pytest backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py -q
```

Expected: both tests pass against PostgreSQL.

- [ ] **Step 5: Commit durable execution**

```bash
git add backend/src/bidscope/graph/executor.py backend/src/bidscope/persistence/repositories.py backend/tests/integration/test_graph_persistence.py backend/tests/integration/test_run_recovery.py
git commit -m "feat: persist Agent checkpoints and run events"
```

### Task 13: Render and Store DOCX Reports Idempotently

**Files:**
- Create: `backend/src/bidscope/delivery/docx.py`
- Create: `backend/tests/unit/delivery/test_docx.py`
- Create: `backend/tests/integration/test_report_delivery.py`

- [ ] **Step 1: Write DOCX parity and idempotency tests**

Render a known `Report`, reopen it with python-docx, and assert query conditions, every item title, unknown-field marker, source URL, evidence label, and completeness warning are present. Export the same report twice and assert one logical export record and one object key.

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest backend/tests/unit/delivery/test_docx.py backend/tests/integration/test_report_delivery.py -q
```

Expected: DOCX renderer is missing.

- [ ] **Step 3: Implement rendering and delivery**

Render only from the typed `Report`; never re-prompt the model. Use deterministic heading order, tables for conditions and opportunities, numbered evidence references, source/version appendix, and sanitized filename `bidscope-{report_id}.docx`. Store bytes through `ObjectStore` and derive the idempotency key from report ID plus renderer version.

- [ ] **Step 4: Verify output**

```bash
uv run pytest backend/tests/unit/delivery backend/tests/integration/test_report_delivery.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit DOCX delivery**

```bash
git add backend/src/bidscope/delivery/docx.py backend/tests/unit/delivery backend/tests/integration/test_report_delivery.py
git commit -m "feat: render idempotent evidence reports"
```

## Milestone 5: API, Scheduling, and Web Product

### Task 14: Expose Run, Report, and SSE APIs

**Files:**
- Create: `backend/src/bidscope/api/dependencies.py`
- Create: `backend/src/bidscope/api/routes/runs.py`
- Create: `backend/src/bidscope/api/routes/reports.py`
- Create: `backend/src/bidscope/api/routes/test_controls.py`
- Modify: `backend/src/bidscope/main.py`
- Create: `backend/tests/integration/api/test_runs.py`
- Create: `backend/tests/integration/api/test_sse.py`

- [ ] **Step 1: Write API contract tests**

Test `POST /api/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/confirm`, `POST /api/runs/{id}/retry`, `GET /api/runs/{id}/events`, `GET /api/reports/{id}`, and `GET /api/reports/{id}/docx`. Confirm returns HTTP 409 unless the run is awaiting confirmation; retry returns 409 unless retryable.

- [ ] **Step 2: Verify routes are absent**

```bash
uv run pytest backend/tests/integration/api/test_runs.py backend/tests/integration/api/test_sse.py -q
```

Expected: endpoint requests return 404.

- [ ] **Step 3: Implement app lifespan, executor tasks, and SSE**

Use an async lifespan to initialize repositories and close engine/object clients. Creating a run stores `pending` before scheduling an executor task. SSE reads ordered database events, emits `id`, `event`, and JSON `data`, sends heartbeats every 15 seconds, honors `Last-Event-ID`, and ends after the terminal event. Public demo mode always injects fake model and hash embeddings; real model mode requires server configuration and `X-Admin-Token`. Register `/api/test-controls/*` only when `app_mode=test`; require a separate test token and expose bounded controls for one-shot node failure and Batch 2 import. Tests must assert these routes return 404 in demo, development, and production modes.

- [ ] **Step 4: Run API integration tests**

```bash
uv run pytest backend/tests/integration/api -q
```

Expected: all endpoints and reconnectable SSE tests pass.

- [ ] **Step 5: Commit API slice**

```bash
git add backend/src/bidscope/api backend/src/bidscope/main.py backend/tests/integration/api
git commit -m "feat: expose Agent runs and reports over API"
```

### Task 15: Add Subscriptions, Advisory Locks, and Inbox Events

**Files:**
- Create: `backend/src/bidscope/subscriptions/service.py`
- Create: `backend/src/bidscope/subscriptions/scheduler.py`
- Create: `backend/src/bidscope/api/routes/subscriptions.py`
- Create: `backend/tests/integration/test_subscriptions.py`
- Create: `backend/tests/integration/test_scheduler_lock.py`

- [ ] **Step 1: Write subscription lifecycle tests**

Test explicit confirmed-intent creation, next run time, second snapshot batch producing `new_notice` and `material_change` inbox events, unchanged items producing no event, and three consecutive failures pausing the subscription.

- [ ] **Step 2: Write a two-worker lock test**

Start two concurrent trigger attempts for the same subscription/time bucket and assert exactly one query run and one set of inbox events are committed.

- [ ] **Step 3: Implement scheduler service and process role**

Use APScheduler with a one-minute tick, database-stored schedules, IANA timezone, and PostgreSQL advisory lock derived from subscription UUID and scheduled timestamp. Advance `subscription_seen_items` only after the report commits. Expose list/create/pause/resume endpoints and `uv run bidscope scheduler`.

- [ ] **Step 4: Verify schedule correctness**

```bash
uv run pytest backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py -q
```

Expected: all tests pass, including concurrent trigger test.

- [ ] **Step 5: Commit subscriptions**

```bash
git add backend/src/bidscope/subscriptions backend/src/bidscope/api/routes/subscriptions.py backend/tests/integration/test_subscriptions.py backend/tests/integration/test_scheduler_lock.py
git commit -m "feat: schedule incremental tender subscriptions"
```

### Task 16: Bootstrap the React Workbench and Main Flow

**Files:**
- Create: `package.json`
- Create: `package-lock.json`
- Create: `web/package.json`
- Create: `web/vite.config.ts`
- Create: `web/src/app/App.tsx`
- Create: `web/src/api/client.ts`
- Create: `web/src/features/workbench/*`
- Create: `web/src/styles/*`
- Create: `web/tests/workbench.test.tsx`

- [ ] **Step 1: Scaffold Vite and install locked dependencies**

Use React, TypeScript, React Router, TanStack Query, lucide-react, Vitest, Testing Library, and MSW. Root scripts must expose `npm run dev:web`, `npm run test:web`, `npm run build:web`, and `npm run test:e2e`.

- [ ] **Step 2: Write the main workbench test**

Mock create-run, SSE events, intent confirmation, and report response. Assert the user can enter the representative query, review editable chips, approve, observe ordered node events, open evidence, see snapshot provenance, and invoke DOCX download.

- [ ] **Step 3: Run the test and observe failure**

```bash
npm install
npm run test:web -- --run web/tests/workbench.test.tsx
```

Expected: test fails because the workbench components are missing.

- [ ] **Step 4: Implement the responsive workbench**

Build the confirmed three-column desktop layout and a drawer for trace/evidence below 1050px. Use fixed icon-button dimensions, lucide icons with tooltips, explicit loading/empty/partial/error/awaiting-confirmation states, accessible labels, and no nested cards. The report visibly distinguishes `raw_response`, `curated_public_excerpt`, and `synthetic_demo`; synthetic records use a persistent “合成演示数据” label and show their non-resolving URL as plain text rather than a clickable link, plus retrieval time, hash prefix, and freshness.

- [ ] **Step 5: Verify and commit the main Web flow**

```bash
npm run test:web -- --run
npm run build:web
npm run lint --if-present

git add package.json package-lock.json web
git commit -m "feat: add BidScope evidence workbench"
```

Expected: tests and production build pass.

### Task 17: Add Operational Web Views

**Files:**
- Create: `backend/src/bidscope/api/routes/sources.py`
- Create: `backend/src/bidscope/api/routes/evaluations.py`
- Create: `web/src/features/runs/*`
- Create: `web/src/features/subscriptions/*`
- Create: `web/src/features/sources/*`
- Create: `web/src/features/evaluation/*`
- Create: `web/tests/operations.test.tsx`

- [ ] **Step 1: Write operations view tests**

Test run filtering and retry eligibility, subscription pause/resume, inbox event states, source bundle provenance and stale/invalid warnings, and evaluation metric cards that distinguish target from measured value.

- [ ] **Step 2: Implement source and evaluation read APIs**

Return bounded DTOs only: no raw HTML or full prompts. Source status includes latest valid bundle, capture kind, retrieval time, hash, parser version, age, and validation warnings. Evaluation output includes dataset version, model, pricing date, environment, measured metrics, and targets.

- [ ] **Step 3: Implement quiet operational pages**

Use tables, tabs, status icons, filters, and side drawers rather than marketing sections. Preserve responsive behavior and keyboard access.

- [ ] **Step 4: Run Web and API tests**

```bash
uv run pytest backend/tests/integration/api -q
npm run test:web -- --run
npm run build:web
```

Expected: all commands pass.

- [ ] **Step 5: Commit operational views**

```bash
git add backend/src/bidscope/api/routes web/src/features web/tests/operations.test.tsx
git commit -m "feat: add BidScope operational views"
```

## Milestone 6: Evaluation, Hardening, and Deployment

### Task 18: Build Versioned Evaluation Datasets and Runner

**Files:**
- Create: `scripts/build_eval_data.py`
- Create: `eval/corpus/synthetic-notices-v1.jsonl`
- Create: `eval/data/intent-v1.jsonl`
- Create: `eval/data/retrieval-v1.jsonl`
- Create: `eval/data/dedup-v1.jsonl`
- Create: `eval/data/claims-v1.jsonl`
- Create: `eval/data/e2e-v1.jsonl`
- Create: `backend/src/bidscope/evaluation/datasets.py`
- Create: `backend/src/bidscope/evaluation/metrics.py`
- Create: `backend/src/bidscope/evaluation/runner.py`
- Create: `backend/tests/unit/evaluation/test_metrics.py`

- [ ] **Step 1: Write metric unit tests**

Cover field Exact Match/Macro F1, Recall@10, nDCG@10, binary precision/recall/F1, citation coverage/correctness, task success, percentile latency, tokens, and CNY cost. Include zero-positive and empty-result cases.

- [ ] **Step 2: Implement deterministic dataset generation**

`build_eval_data.py` must create a separately labeled synthetic notice corpus and at least 100 intent cases from fixed Chinese templates plus explicit ambiguity/error cases, 30 retrieval tasks over that synthetic corpus, 100 labeled synthetic notice pairs, 50 labeled synthetic report claims, and 30 end-to-end scenarios. Synthetic records use reserved `eval-*` IDs and non-resolving `https://example.invalid/` URLs so they cannot be confused with official-source snapshots. It validates exact minimum counts, stable IDs, no duplicate IDs, and expected-schema validity before writing JSONL. The generated data is committed and reviewed; the runner never silently regenerates it.

- [ ] **Step 3: Implement runner and machine-readable result**

Expose:

```bash
uv run bidscope eval run --mode deterministic --output eval/results/deterministic.json
```

The result records git commit, dataset hashes, model/provider, pricing snapshot date, database fixture version, environment, every metric, P50/P95 latency, tokens, cost, and pass/fail against targets. It exits nonzero only for data/schema/execution failure; target misses are reported as metrics, not hidden as command failures.

- [ ] **Step 4: Run metric and deterministic evaluation tests**

```bash
uv run pytest backend/tests/unit/evaluation -q
uv run bidscope eval run --mode deterministic --output eval/results/deterministic.json
```

Expected: tests pass and output contains all metric keys and provenance fields.

- [ ] **Step 5: Commit evaluation system**

```bash
git add scripts/build_eval_data.py eval/corpus eval/data eval/results/.gitkeep backend/src/bidscope/evaluation backend/tests/unit/evaluation
git commit -m "feat: add reproducible BidScope evaluation"
```

### Task 19: Add Security, Degradation, and Failure Regression Tests

**Files:**
- Create: `backend/tests/security/test_snapshot_urls.py`
- Create: `backend/tests/security/test_prompt_injection.py`
- Create: `backend/tests/integration/test_partial_sources.py`
- Create: `backend/tests/integration/test_idempotency.py`
- Create: `backend/tests/integration/test_failure_recovery.py`

- [ ] **Step 1: Write the security matrix**

Reject non-HTTPS URLs, lookalike and user-info hosts, undeclared files, path traversal, changed hashes, CAPTCHA/session artifacts, unsafe DOCX filenames, raw HTML rendering, arbitrary tool names, SQL-like query plans, and imported source text that asks the model to ignore instructions or call tools.

- [ ] **Step 2: Write failure and degradation scenarios**

Cover one stale source plus one valid source, one parse-invalid source plus one valid source, vector provider failure, model transient failure with two retries, evidence validation failure with one synthesis retry, DOCX failure after online report success, and startup recovery of stale running jobs.

- [ ] **Step 3: Implement only the guards exposed by failing tests**

Keep public demo mode network-free. Ensure all errors serialize to the bounded error union and all partial reports contain source-completeness warnings.

- [ ] **Step 4: Run the hardening suite**

```bash
uv run pytest backend/tests/security backend/tests/integration/test_partial_sources.py backend/tests/integration/test_idempotency.py backend/tests/integration/test_failure_recovery.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit hardening coverage**

```bash
git add backend/tests/security backend/tests/integration backend/src/bidscope
git commit -m "test: harden BidScope trust and recovery boundaries"
```

### Task 20: Package, Deploy, and Verify the Complete Product

**Files:**
- Create: `Dockerfile`
- Modify: `compose.yaml`
- Create: `e2e/playwright.config.ts`
- Create: `e2e/specs/main-flow.spec.ts`
- Create: `docs/evaluation.md`
- Create: `docs/deployment.md`
- Create: `README.md`
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Build the production image and process roles**

The multi-stage Dockerfile builds `web/dist`, installs the locked Python project, copies the SPA into the backend static directory, runs as a non-root user, and supports commands `bidscope api` and `bidscope scheduler`. Compose defines postgres, minio, api, and exactly one scheduler service with health checks and persistent volumes. The PostgreSQL initialization script creates both `bidscope` and `bidscope_e2e`; Playwright runs migrations, checkpoint setup, and Batch 1 import against `bidscope_e2e`, then starts an isolated test-mode API on port `8001`.

- [ ] **Step 2: Write six Playwright flows**

Cover: new query and structured intent, human confirmation, report and evidence inspection, retry from a one-shot failure configured through the test-only control route, subscription creation plus Batch 2 import through the test-only control route, and DOCX download. Playwright starts the application with `BIDSCOPE_APP_MODE=test` and a random test token; add an API test proving the control routes are not registered in demo or production. Add desktop `1440x900` and mobile `390x844` overlap assertions and screenshot baselines for idle, awaiting confirmation, completed report, and partial-source warning.

- [ ] **Step 3: Document reproducibility and measured claims**

README must include architecture, snapshot-only source policy, setup, fixture import, demo flow, test commands, evaluation methodology, measured deterministic results, live-model evaluation command, security boundaries, deployment, limitations, and project narrative. `docs/evaluation.md` separates targets from measured values and names dataset/model/pricing/environment. Do not write resume metrics until a real evaluation result exists.

- [ ] **Step 4: Run the full verification gate**

```bash
uv sync --frozen --all-groups
npm ci

docker compose up -d postgres minio
uv run alembic upgrade head
uv run bidscope checkpoints setup
uv run bidscope snapshots import data/snapshots/ccgp/2026-07-18-central-open
uv run bidscope snapshots import data/snapshots/ggzy/2026-07-18-construction
uv run bidscope snapshots import data/demo/batch-1
uv run ruff check backend scripts
uv run mypy backend/src/bidscope
uv run pytest backend/tests --cov=bidscope --cov-report=term-missing
npm run test:web -- --run
npm run build:web
docker build -t bidscope:local .
docker compose up -d api scheduler
npm run test:e2e
uv run bidscope eval run --mode deterministic --output eval/results/deterministic.json
git diff --check
```

Expected: every command exits `0`; API and scheduler are healthy; all six E2E flows pass; evaluation output includes complete provenance and measured metrics.

- [ ] **Step 5: Commit the verified release candidate**

```bash
git add Dockerfile compose.yaml e2e docs README.md .github package.json package-lock.json
git commit -m "chore: package and verify BidScope P0"
```

## Completion Gate

Before claiming P0 complete:

- Run the full Task 20 verification gate from a clean checkout.
- Review every measured metric against the design targets without rewriting failed metrics as successes.
- Confirm the deployed UI distinguishes raw responses, curated public excerpts, and synthetic demo records, with a persistent synthetic-data label on every synthetic report item.
- Confirm no code path in the deployed runtime fetches a public tender website.
- Record final commands, environment, model, pricing date, dataset hashes, and Git commit in the evaluation result.
- Request code review with findings prioritized by correctness, evidence integrity, recovery, source policy, and missing tests.
