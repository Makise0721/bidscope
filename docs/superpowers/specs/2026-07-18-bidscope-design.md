# BidScope Design Specification

**Status:** Approved design baseline  
**Date:** 2026-07-18  
**Source topic:** 2026 AI 先锋未来人才大赛，超聚变命题 39  
**Primary goal:** Build a portfolio-grade, evidence-first tender intelligence Agent for AI Agent engineering roles.

## 1. Context

The original topic asks for a runnable API, CLI, or Web UI that accepts a natural-language tender query, gathers information from multiple tender websites, produces a Word report, and either runs immediately or follows a schedule inferred from the request.

BidScope adapts this into a portfolio project rather than a formal competition submission. It preserves the real workflow but prioritizes reliable Agent architecture, traceable evidence, measurable quality, failure recovery, and a stable online demonstration. It does not depend on private enterprise data or competition-specific Feishu access.

The project is designed for one developer working for three to four weeks. The developer is strongest in Python, FastAPI, LangChain, and LangGraph, so the implementation favors a Python-centric backend and a deliberately limited React workbench.

## 2. Product Definition

BidScope is an evidence-first tender intelligence Agent for presales, sales, and tender operations staff who repeatedly search for relevant opportunities.

A representative request is:

> 每周一上午 9 点，汇总近 7 天四川和重庆与“智算中心、服务器”有关、预算 500 万以上的招标信息。

The system converts the request into explicit search and schedule conditions, asks for confirmation when required, retrieves normalized notices, merges cross-source duplicates, verifies claims against source evidence, produces an online report and DOCX, and optionally saves the query as a recurring subscription.

### 2.1 P0 Scope

P0 includes:

1. Parse natural language into topics, expanded terms, regions, publication window, budget range, and schedule.
2. Show parsed conditions for correction and confirmation.
3. Query normalized and versioned notices imported from two official-source snapshot adapters.
4. Apply structured filtering, keyword search, and semantic retrieval.
5. Normalize fields, merge cross-source duplicates, and preserve notice versions.
6. Bind every reported fact to a source notice and evidence span.
7. Generate an online structured report and a downloadable DOCX from the same report model.
8. Save a confirmed recurring query as a subscription and report only new or materially changed notices on later runs.
9. Show run history, node events, failures, retries, latency, model use, and estimated cost.
10. Provide an evaluation view for intent parsing, retrieval, deduplication, citations, end-to-end success, latency, and cost.

### 2.2 Explicit Non-Goals

P0 does not include:

- Automated bidding, bid-document writing, or submission.
- Bypassing authentication, CAPTCHA, paywalls, access controls, or anti-bot measures.
- Crawling logged-in or paid sources.
- Arbitrary user-provided URLs, SQL, shell commands, or network requests.
- Real email, SMS, or Feishu delivery; notifications remain in the in-app inbox.
- Enterprise multi-tenancy, complex role-based access control, billing, or social login.
- Production claims based on synthetic or snapshot data.

## 3. User Experience

### 3.1 Information Architecture

The Web application has five primary views:

- **Workbench:** create a query, confirm parsed intent, stream run events, read a report, inspect evidence, export DOCX, and save a subscription.
- **Run history:** inspect status, timing, errors, retries, node events, and rerun a failed node.
- **Subscriptions and inbox:** manage schedules and review new, changed, or failed-run events.
- **Evaluation:** compare quality, latency, and cost across evaluation runs.
- **Data sources:** inspect snapshot provenance, import status, parser version, freshness, and validation warnings.

The workbench uses a quiet three-column operational layout:

- Left: saved queries, subscriptions, inbox counts, and source health.
- Center: natural-language request, parsed-condition confirmation, result summary, and opportunity list.
- Right: Agent execution timeline and the selected claim's source evidence.

On narrow viewports, the evidence and trace column becomes a drawer instead of competing with the report.

### 3.2 Main Demonstration Flow

1. A user enters the representative weekly Sichuan/Chongqing query.
2. BidScope streams an intent-parsing event and displays structured conditions.
3. The user confirms or edits the conditions.
4. The run streams retrieval, deduplication, evidence verification, and report-validation events.
5. The report displays matched opportunities, relevance, known fields, unknown fields, risks, and citations.
6. Selecting a citation opens the exact evidence span and source version.
7. The user downloads the DOCX.
8. The user saves the confirmed schedule as a subscription.
9. Importing a prepared second snapshot batch demonstrates new and materially changed notices on the next subscription run.
10. A prepared missing, stale, or parser-invalid source snapshot demonstrates partial results, a completeness warning, checkpoint recovery, and an idempotent retry.

## 4. System Architecture

BidScope separates ingestion from user-facing query execution.

### 4.1 Snapshot Ingestion Plane

P0 uses two source-specific snapshot adapters. Each adapter imports an explicitly supplied snapshot bundle, verifies its manifest and SHA-256 hashes, parses source-specific records, and emits the same normalized notice contract. A failed or stale snapshot from one source does not block the other source.

The ingestion pipeline is:

```text
snapshot bundle + provenance manifest
  -> integrity and source-policy validation
  -> source-specific parsing
  -> field normalization
  -> deterministic validation
  -> immutable notice version
  -> exact duplicate candidates
  -> searchable canonical notice
  -> embedding/index update
```

Snapshot import is an explicit CLI or administrative action and is idempotent. User queries read only the controlled, versioned data store. P0 does not fetch public websites in the interactive path or on a background schedule. The adapter boundary remains compatible with a future authorized API or live-source implementation, but no live connector is part of the P0 runtime or acceptance criteria.

### 4.2 Query and Delivery Plane

A FastAPI service accepts commands and exposes Server-Sent Events for run progress. A LangGraph graph owns each query run. PostgreSQL stores application records, subscriptions, run events, and LangGraph checkpoints. pgvector adds semantic retrieval. S3-compatible object storage holds raw snapshots and generated DOCX files.

APScheduler triggers subscriptions. PostgreSQL advisory locks prevent concurrent execution of the same subscription. P0 deliberately avoids Celery and Redis.

### 4.3 Architectural Principles

- **Evidence first:** the model cannot invent factual fields; supported claims point to immutable source evidence.
- **Deterministic first:** code handles date and budget comparisons, exact filtering, exact deduplication, scheduling, and citation integrity.
- **Typed tools only:** Agent nodes call predefined tools with validated schemas.
- **Recoverable execution:** checkpoints, node events, idempotency keys, and typed errors make interrupted work resumable.
- **One graph, clear nodes:** P0 uses a bounded workflow rather than multiple Agents created only for presentation value.

## 5. LangGraph Design

### 5.1 State Contract

`RunState` is a versioned Pydantic model containing:

```text
run_id
user_request
status
search_intent
retrieval_plan
candidate_notice_ids
duplicate_groups
verified_opportunities
report
node_events
token_usage
latency
errors
retry_count
```

`SearchIntent` contains:

```text
topics
expanded_terms
regions
published_from
published_to
min_budget
max_budget
schedule
confidence
assumptions
```

Large notice bodies are never placed in graph state. State stores IDs and bounded evidence references; tools load the required records.

### 5.2 Nodes

1. **`parse_intent`** uses constrained structured output and preserves the original request.
2. **`validate_intent`** deterministically checks date, amount, region, schedule, and conflicting fields.
3. **`confirm_intent`** uses LangGraph `interrupt()` when required. Low-confidence or conflicting required fields must be confirmed. Creating any recurring subscription always requires explicit confirmation.
4. **`build_retrieval_plan`** selects query expansion and a hybrid retrieval strategy from bounded choices.
5. **`retrieve_candidates`** performs structured, keyword, and vector retrieval and returns notice IDs.
6. **`resolve_duplicates`** performs exact merging first and asks the model only about bounded ambiguous pairs.
7. **`verify_evidence`** binds each factual field and summary claim to immutable source spans. Unsupported facts are removed or marked unknown.
8. **`synthesize_report`** creates opportunity summaries, relevance reasons, and risk notes from verified records only.
9. **`validate_report`** checks citation existence, citation-version integrity, links, required fields, and unsupported output. It retries only the relevant upstream node and at most once.
10. **`persist_and_deliver`** stores results, renders the online report and DOCX, and advances a subscription's seen-item cursor only after persistence succeeds.

### 5.3 Model Responsibilities

The language model handles:

- Semantic intent parsing.
- Query-term expansion within configured limits.
- Ambiguous duplicate classification.
- Evidence-grounded summaries and risk descriptions.

Deterministic code handles:

- Validation, filtering, amount and date comparison.
- Exact deduplication.
- Citation and version checks.
- Scheduling, locks, retries, and idempotency.
- Rendering and delivery state transitions.

## 6. Snapshot Adapters and Source Policy

P0 imports snapshot bundles derived from public notices on:

- 中国政府采购网.
- 全国公共资源交易平台.

A snapshot adapter conforms to:

```python
class NoticeSnapshotAdapter(Protocol):
    source: SourceName

    def inspect(self, bundle: SnapshotBundle) -> SnapshotInspection: ...
    def parse(self, bundle: SnapshotBundle) -> list[NormalizedNotice]: ...
```

A bundle declares `capture_kind` as either `raw_response` or `curated_public_excerpt`. A raw-response bundle contains the captured HTML or JSON, source URL, request method and non-secret request parameters, retrieval time, HTTP status, content type and charset, SHA-256 hashes, and parser version. When lawful low-frequency access is blocked, a curated-public-excerpt bundle may contain only publicly verified fields in a source-shaped fixture; it must record the verification URLs and retrieval attempt outcome and must not claim to be the original response. Human-reviewed expected JSON remains test-only metadata. Bundles never contain cookies, session credentials, CAPTCHA tokens or images, or downloaded attachments.

The public URL patterns and page structures are observable, but neither source offers a documented, versioned public API or stable schema. During design research, 中国政府采购网 returned WAF responses including HTTP 403 and a frequent-access warning. 全国公共资源交易平台 exposed an undocumented Web POST used by its Vue page, but it includes CAPTCHA and anti-automation responses, a result cap, and heterogeneous upstream data. Reliable robots directives or an automated-access grant were not available for either source. Public browser visibility is therefore not treated as permission for automated collection.

P0 is snapshot-only. Raw snapshots must be acquired manually or through a separately authorized process; curated public excerpts may be prepared when source access is blocked, but their different provenance must remain visible. Both forms are imported through an explicit CLI or administrative action. The deployed application never performs live page fetching. The UI labels every record as demonstration snapshot data, distinguishes raw responses from curated excerpts, and displays source URL, retrieval time, content hash, and freshness warning. Contract tests parse fixtures and compare them with human-reviewed expected JSON without hitting public services.

A future live adapter requires a separately documented authorization and source contract. It must use a configured HTTPS allowlist, stop at authentication, CAPTCHA, rate limiting, or access denial, and may not weaken the P0 snapshot path.

## 7. Data Model

Core records are:

- **`source_notice`:** source, external ID, canonicalized source URL, first and latest fetch timestamps, and current content hash.
- **`notice_version`:** immutable raw snapshot reference, normalized fields, parser version, and content hash.
- **`canonical_notice`:** the logical notice shared by one or more source records.
- **`notice_evidence`:** notice-version ID, text span, character offsets, and span hash.
- **`snapshot_bundle` / `snapshot_import`:** provenance manifest, object references, hashes, parser version, import status, validation warnings, idempotency key, and metrics.
- **`query_run` / `run_event`:** graph execution status, node events, timing, errors, usage, and checkpoint linkage.
- **`report` / `report_item`:** the structured report and claim-to-evidence references.
- **`subscription` / `subscription_seen_item`:** confirmed schedule, normalized intent, last successful run, and seen notice/version pairs.
- **`inbox_event`:** new opportunity, material change, source completeness warning, or task failure.
- **`eval_case` / `eval_run`:** versioned evaluation inputs, expected outputs, predictions, and metrics.

Money is stored as integer minor units plus currency. Time is stored in UTC while preserving the source timezone. Unreliable fields remain `NULL` and retain their original text. Original notice versions and evidence are never overwritten.

Cross-source candidate grouping uses available project number, purchaser, publication time, money, normalized title, and content fingerprints. Only bounded ambiguous pairs require semantic classification.

A material change is a new notice version that changes a configured business field such as deadline, budget, region, purchaser, procurement scope, cancellation state, or source text supporting a reported claim. Formatting-only changes do not create an inbox alert.

## 8. Reports and Citations

The online view and DOCX share one typed `Report` model. A report includes:

- Query conditions and data-freshness window.
- Source availability and completeness warnings.
- Counts for retrieved, filtered, merged, and reported notices.
- Ranked opportunities with known and unknown fields.
- Evidence-backed summary and relevance reasons.
- Risk or uncertainty notes.
- Source title, URL, version time, and evidence references.

A claim must refer to an evidence record belonging to the cited notice version. If the underlying source receives a later version, older reports remain reproducible and display that a newer source version exists.

## 9. Error Handling and Recovery

Errors are serialized using bounded types:

- `SnapshotIntegrityError`
- `SnapshotStale`
- `ParseDrift`
- `IntentInvalid`
- `RetrievalEmpty`
- `EvidenceInsufficient`
- `ModelTransientError`
- `DeliveryError`

Transient model failures use exponential backoff with jitter and at most two retries. Snapshot integrity failures stop that import before any application records are committed. Parse drift stores bounded parser diagnostics and marks the affected source snapshot invalid. Empty retrieval is a valid result, not a system failure.

Every snapshot import, query run, subscription trigger, and report export has an idempotency key. A retry cannot create duplicate logical notices, inbox events, or report records. A missing, stale, or parser-invalid source produces partial results with a visible completeness warning when at least one source remains usable. A DOCX failure does not roll back the online report and can be retried independently.

Graph checkpoints are written at node boundaries. Recovery resumes at the failed node and reuses persisted upstream outputs. Subscription execution uses PostgreSQL advisory locks, and repeated failures pause the subscription and create an inbox event.

## 10. Security Boundaries

- The P0 deployed runtime has no public-site fetch tool. Snapshot manifests accept source URLs only from an explicit HTTPS host allowlist and reject arbitrary URLs or redirect-derived provenance.
- Snapshot acquisition must not bypass authentication, CAPTCHA, paywalls, access controls, or anti-bot protections.
- Imported content is untrusted data. It cannot override system instructions or request tool execution.
- Agent tools accept validated typed inputs and cannot execute arbitrary SQL, shell commands, or network calls.
- Raw HTML is never rendered directly. Report text is escaped, URLs are validated, and generated filenames are sanitized.
- API keys remain in server-side secrets. Logs exclude secrets, full prompts, and unnecessary user content.
- Source snapshots and generated files use non-guessable object keys and are served through authorized application routes or short-lived signed URLs.

## 11. Observability

Each run records:

- Node start/end events and duration.
- Bounded input/output summaries.
- Tool name, status, item counts, and retry state.
- Typed errors and recovery decisions.
- Model, token use, latency, and estimated cost.
- Snapshot provenance, age, parser status, and source completeness.

The workbench streams run events through SSE. Run history supports inspection by `run_id` and an idempotent retry from an eligible failed node. Raw source bodies and model context are referenced by ID rather than copied into logs.

## 12. Technology Choices

- Python 3.12
- FastAPI, LangGraph, Pydantic
- SQLAlchemy and Alembic
- HTTPX and selectolax; Playwright only when justified by source behavior and policy
- PostgreSQL and pgvector
- APScheduler with PostgreSQL advisory locks
- DeepSeek for structured parsing and evidence-grounded synthesis
- A low-cost embedding API, with keyword retrieval as a functional degradation path
- React, TypeScript, Vite, and TanStack Query
- SSE for run progress
- python-docx for DOCX output
- Docker for local and hosted builds
- S3-compatible object storage for snapshots and generated documents

P0 deploys the API, Web static assets, and scheduler from one application image. The scheduler runs as a separately selectable process role from the same image so hosted environments can ensure a single scheduler instance. PostgreSQL is a managed service in the hosted environment. No provider-specific service is part of the product contract.

## 13. Evaluation and Quality Targets

The repository includes versioned evaluation data:

- At least 100 natural-language intent cases, scored with field-level Exact Match and Macro F1.
- At least 30 retrieval tasks, scored with Recall@10 and nDCG@10.
- At least 100 notice pairs, scored with deduplication precision, recall, and F1.
- At least 50 report claims, scored for citation coverage, citation correctness, and factual support.
- At least 30 end-to-end scenarios, scored for task success, P50/P95 latency, tokens, and estimated cost.

P0 targets are:

- Intent field Macro F1 at least 90%.
- Retrieval Recall@10 at least 85%.
- Deduplication F1 at least 90%.
- Citation coverage exactly 100% and citation correctness at least 95%.
- Cached-data query P95 no more than 15 seconds under the documented test environment.
- Fixed-scenario task success at least 95%.
- Standard-query model cost no more than CNY 0.10 under the documented model and pricing snapshot.

These are acceptance targets, not resume claims. README and resume copy use only measured results and name the test dataset, model, pricing date, and environment.

## 14. Testing Strategy

- **Unit tests:** intent validation, date and money parsing, structured filters, deterministic deduplication, material-change detection, report validation, and scheduling.
- **Snapshot adapter contract tests:** verify bundle manifests and hashes, parse saved raw responses, compare human-reviewed normalized records, and detect parser drift without network access.
- **Graph integration tests:** fake model and fake tools verify routing, interrupts, recovery, bounded retries, degradation, and idempotency.
- **API integration tests:** real PostgreSQL verifies migrations, transactions, advisory locks, notice versioning, checkpoint persistence, and subscription cursor updates.
- **Frontend tests:** Vitest covers critical state rendering and error boundaries.
- **End-to-end tests:** Playwright covers new query, intent confirmation, report and evidence inspection, failed-run retry, subscription creation, and DOCX download.
- **Evaluation regression:** a repeatable command produces machine-readable metrics and a human-readable evaluation report.

## 15. Delivery Plan

### Week 1: Data Foundation

- Repository and development environment.
- Database schema and migrations.
- Snapshot bundle contract and two fixture-backed source adapters.
- Provenance and hash validation, normalization, versioning, deterministic quality gates, and idempotent import tests.

### Week 2: Agent and Reports

- LangGraph state, nodes, tools, interrupts, and checkpoints.
- Hybrid retrieval, deterministic and semantic deduplication.
- Evidence extraction and report validation.
- Online report model and DOCX renderer.

### Week 3: Product Workflow

- React workbench and SSE run timeline.
- Run history, evidence drawer, subscriptions, scheduler, and inbox.
- Snapshot provenance, freshness, parser warnings, and partial-result states.
- Docker deployment and hosted smoke flow.

### Week 4: Evidence of Quality

- Evaluation datasets and regression runner.
- Failure, recovery, idempotency, security, and performance tests.
- Cost and latency measurement.
- README, architecture diagram, data/source disclosure, demonstration video, and measured resume bullets.

## 16. Acceptance Criteria

P0 is complete when:

1. A fresh environment starts from documented commands and applies database migrations.
2. Both official-source snapshot bundles pass provenance and hash validation and ingest normalized, versioned notices through the shared adapter contract.
3. The representative query completes the confirmed LangGraph flow and produces a cited online report.
4. Every factual report claim resolves to an immutable evidence span and source version.
5. DOCX output matches the structured online report and can be retried without duplicating the logical report.
6. A recurring query is explicitly confirmed, scheduled, locked against duplicate execution, and reports only new or materially changed notices.
7. A missing, stale, or parser-invalid source snapshot demonstrates partial results and a visible completeness warning.
8. A transient node failure resumes from a checkpoint without repeating successful upstream model work.
9. The fixed evaluation command publishes all agreed metrics and clearly distinguishes targets from measured values.
10. The six critical Playwright flows and all backend test layers pass in the documented environment.
11. The deployed demonstration visibly labels all records as demonstration snapshots and exposes source URL, retrieval time, content hash, and freshness.
12. README documents source policy, limitations, architecture, evaluation method, test commands, deployment, and reproducible demo steps.

## 17. Portfolio Narrative

The project should support a technically defensible interview narrative:

- Why ingestion is decoupled from interactive Agent runs.
- Why one bounded graph is more appropriate than ornamental multi-Agent orchestration.
- Where deterministic code replaces LLM judgment.
- How evidence and immutable versions prevent unsupported summaries.
- How LangGraph interrupts, checkpoints, and node-level retries improve reliability.
- How retrieval, deduplication, citations, success rate, latency, and cost are evaluated separately.
- How public-source access, prompt injection, SSRF, idempotency, and partial failure are handled.

Final resume bullets must be generated only after implementation and measurement. No target metric in this document is to be presented as an achieved result.
