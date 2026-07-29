# BidScope P1 Productization Handoff

**Date:** 2026-07-29
**Implementation baseline:** `90c962664d878cad560e2071727177df4a0c6f30`
**Gate status:** latest GitHub Actions CI is green (confirmed by the project owner).

## Purpose

P1 is complete. The next workstream is productization: turn a secure,
single-operator, snapshot-based tender-intelligence system into a service that can
be evaluated and operated with authorized real-world data. Do not treat the
deterministic demo as evidence of market performance.

## What Is Delivered

- FastAPI backend, React/Vite SPA, PostgreSQL/pgvector, S3-compatible object
  storage, and a separate APScheduler process role.
- Snapshot inspection/import, provenance validation, immutable evidence spans,
  hybrid retrieval, deduplication, cited reports, DOCX export, and subscriptions.
- P1 controls: fail-closed production configuration, single Admin Token,
  Trusted Host/CORS policy, redacted audit records, protected Prometheus metrics,
  request IDs, readiness/liveness endpoints, capacity bounds, retry/recovery,
  Docker smoke checks, backup/restore, and a clean-host recovery drill.
- CI runs Ruff, strict mypy, backend unit/contract/security/integration suites,
  deterministic evaluation, web tests/build, Playwright E2E, Docker smoke, and
  recovery evidence collection.

## Non-Negotiable Boundaries

1. The current product is **snapshot-only**. It must not crawl, scrape, bypass
   anti-automation, solve captchas, or probe procurement sites. Any live source
   integration needs separate written authorization and an approved design.
2. Preserve the evidence contract: source version and cited span are immutable;
   manifests and payload hashes must validate before data is searchable.
3. Keep synthetic data visibly synthetic (`synthetic_demo`, `example.invalid`)
   and separate it from authorized public excerpts or raw responses.
4. Production remains single-tenant and Admin-Token based. Do not add accounts,
   cookies, OAuth, or RBAC as incidental changes; choose the tenancy model first.
5. Keep production credentials outside Git, preserve guarded `*_test`/`*_e2e`
   database targets, and never point test or restore commands at live data.

## Productization Priorities

### 1. Data Governance and Authorized Ingestion

Define the commercial target users, covered regions/categories, data owner, legal
basis, retention period, update SLA, and correction/takedown process. Then design
an authorized ingestion boundary (provider feed, licensed export, or reviewed
manual import), including source contracts, rate/cost limits, quarantine, schema
versioning, and provenance labels. Keep it independent from interactive runs.

**Done when:** an approved source agreement and data contract exist; one real
dataset is imported through the normal manifest/provenance path; failure and
reprocessing behavior are tested without weakening snapshot-only safeguards.

### 2. Real-Data and Model Evaluation

Create a versioned, access-controlled evaluation set with annotation guidance.
Measure retrieval quality, deduplication, citation support, latency, cost, and
human usefulness separately from the synthetic fixture-consistency gate. Add a
staging-only live-model evaluation with provider, model version, prompts,
pricing date, sample size, and failure policy recorded.

**Done when:** the release decision cites real, reproducible evaluation evidence,
not `target_pass=true` from the deterministic fixture alone.

### 3. Product Access Model

Make an explicit decision: remain a single organization console, or introduce
multi-tenant organizations, users, roles, sessions, audit attribution, tenant
data isolation, quotas, and export permissions. Write an ADR before schema or
authentication changes. The current Admin Token is an operations control, not a
customer identity system.

### 4. Operating Model

Add environment separation (development/staging/production), managed secret
rotation, alert routing and dashboards, SLOs, incident ownership, usage/cost
budgets, and tested external backup replication. Run load and recovery exercises
using representative authorized data before claiming availability targets.

## Recommended First Milestone

Start with a short product-discovery/specification milestone, not code:

1. Select the initial customer and one authorized data source.
2. Write a data-governance and tenancy ADR.
3. Define measurable launch metrics and a staging acceptance plan.
4. Break the approved design into small vertical slices: ingestion, evaluation,
   access control, then operations.

The first implementation PR should be an isolated data-contract/provenance
extension with fixtures and tests—not a live scraper or a broad authentication
rewrite.

## Key Files and Commands

| Need | Location / command |
|---|---|
| Architecture and local setup | `README.md` |
| Source rules | `docs/source-policy.md` |
| Deployment and recovery runbooks | `docs/deployment.md`, `docs/runbooks/bidscope-production.md` |
| Deterministic evaluation limits | `docs/evaluation.md` |
| Application settings and security policy | `backend/src/bidscope/config.py`, `backend/src/bidscope/api/auth.py` |
| Snapshot/provenance boundary | `backend/src/bidscope/snapshots/`, `backend/src/bidscope/domain/provenance.py` |
| CI contract | `.github/workflows/ci.yml` |
| Backend checks | `uv run ruff check backend scripts && uv run mypy backend/src/bidscope` |
| Backend tests | `uv run pytest backend/tests/unit backend/tests/contract backend/tests/security -q` |
| Web checks | `npm run test:web && npm run build:web` |
| Recovery gate | `bash scripts/backup_restore_smoke.sh` |

## Residual Limits to Carry Forward

- No real procurement corpus, live-source quality evidence, or production
  performance benchmark is committed.
- External backup replication is disabled until separately configured.
- There is no customer account, RBAC, tenant isolation, billing, or self-service
  administration.
- The fake deterministic model and synthetic data keep CI reproducible but do
  not validate live-model behavior, provider outages, prompt drift, or cost.

## Suggested New-Window Brief

> Continue BidScope from the P1 baseline. First produce a productization design
> for authorized data ingestion, real-data evaluation, and the chosen access
> model. Preserve snapshot-only operation and immutable provenance until a
> source-specific authorization and ADR are approved. Do not implement live
> scraping or multi-tenant auth before those decisions. Deliver a scoped plan,
> acceptance criteria, and the smallest safe first vertical slice.
