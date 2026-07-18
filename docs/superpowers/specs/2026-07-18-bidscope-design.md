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
3. Query two public-source connectors over previously ingested and versioned notices.
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
- **Data sources:** inspect connector health, last successful crawl, cursor, freshness, and circuit-breaker state.

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
9. A prepared second run demonstrates new and materially changed notices.
10. A prepared connector-failure scenario demonstrates partial results, a completeness warning, checkpoint recovery, and an idempotent retry.

## 4. System Architecture

BidScope separates ingestion from user-facing query execution.

### 4.1 Incremental Ingestion Plane

Two connectors fetch public index and detail pages. Each connector owns its own cursor, rate limit, timeout, retry policy, health status, and circuit breaker. A failed connector does not block other connectors.

The ingestion pipeline is:

```text
connector fetch
  -> raw response snapshot
  -> field normalization
  -> deterministic validation
  -> immutable notice version
  -> exact duplicate candidates
  -> searchable canonical notice
  -> embedding/index update
```

A background crawl refreshes cached data. A user query normally reads the controlled data store instead of scraping entire websites in the request path. A bounded refresh tool may update a known source and time range when freshness is insufficient.

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

## 6. Connectors and Source Policy

P0 targets:

- 中国政府采购网.
- 全国公共资源交易平台.

A connector conforms to:

```python
class NoticeConnector(Protocol):
    async def fetch_index(self, cursor: CrawlCursor) -> FetchBatch: ...
    async def fetch_detail(self, item: SourceItem) -> RawNotice: ...
    async def healthcheck(self) -> ConnectorHealth: ...
```

Connectors access only configured HTTPS hosts. They use respectful rate limits, identify the application where appropriate, and stop when authentication, CAPTCHA, access controls, or explicit blocking is encountered. JavaScript execution is disabled by default; Playwright is allowed only for a source whose public content cannot be obtained through a normal HTTP client and whose access policy permits it.

The online demonstration includes public-page fixtures derived from captured notices. The UI labels records as either live-source data or demonstration snapshots. Snapshot mode is an explicit fallback, not a hidden substitution. Connector contract tests parse fixtures so changes in page structure are detectable without repeatedly hitting public services.

## 7. Data Model

Core records are:

- **`source_notice`:** source, external ID, canonicalized source URL, first and latest fetch timestamps, and current content hash.
- **`notice_version`:** immutable raw snapshot reference, normalized fields, parser version, and content hash.
- **`canonical_notice`:** the logical notice shared by one or more source records.
- **`notice_evidence`:** notice-version ID, text span, character offsets, and span hash.
- **`crawl_run` / `crawl_cursor`:** ingestion batch state, source cursor, failures, retry state, and metrics.
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

- `ConnectorUnavailable`
- `ParseDrift`
- `IntentInvalid`
- `RetrievalEmpty`
- `EvidenceInsufficient`
- `ModelTransientError`
- `DeliveryError`

Network and transient model failures use exponential backoff with jitter and at most two retries. Parse drift opens the connector circuit and stores a diagnostic sample rather than repeatedly requesting a changed page. Empty retrieval is a valid result, not a system failure.

Every crawl batch, query run, subscription trigger, and report export has an idempotency key. A retry cannot create duplicate logical notices, inbox events, or report records. A connector outage produces partial results with a visible completeness warning when at least one source remains usable. A DOCX failure does not roll back the online report and can be retried independently.

Graph checkpoints are written at node boundaries. Recovery resumes at the failed node and reuses persisted upstream outputs. Subscription execution uses PostgreSQL advisory locks, and repeated failures pause the subscription and create an inbox event.

## 10. Security Boundaries

- Network tools use an explicit HTTPS host allowlist and block arbitrary URLs and redirect escapes.
- The system does not bypass authentication, CAPTCHA, paywalls, access controls, or anti-bot protections.
- Crawled content is untrusted data. It cannot override system instructions or request tool execution.
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
- Connector freshness and source completeness.

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
- **Connector contract tests:** parse saved public-page fixtures and verify normalized records and drift detection.
- **Graph integration tests:** fake model and fake tools verify routing, interrupts, recovery, bounded retries, degradation, and idempotency.
- **API integration tests:** real PostgreSQL verifies migrations, transactions, advisory locks, notice versioning, checkpoint persistence, and subscription cursor updates.
- **Frontend tests:** Vitest covers critical state rendering and error boundaries.
- **End-to-end tests:** Playwright covers new query, intent confirmation, report and evidence inspection, failed-run retry, subscription creation, and DOCX download.
- **Evaluation regression:** a repeatable command produces machine-readable metrics and a human-readable evaluation report.

## 15. Delivery Plan

### Week 1: Data Foundation

- Repository and development environment.
- Database schema and migrations.
- Connector protocol and two fixture-backed connector implementations.
- Raw snapshots, normalization, versioning, deterministic quality gates, and ingestion tests.

### Week 2: Agent and Reports

- LangGraph state, nodes, tools, interrupts, and checkpoints.
- Hybrid retrieval, deterministic and semantic deduplication.
- Evidence extraction and report validation.
- Online report model and DOCX renderer.

### Week 3: Product Workflow

- React workbench and SSE run timeline.
- Run history, evidence drawer, subscriptions, scheduler, and inbox.
- Connector health and partial-result states.
- Docker deployment and hosted smoke flow.

### Week 4: Evidence of Quality

- Evaluation datasets and regression runner.
- Failure, recovery, idempotency, security, and performance tests.
- Cost and latency measurement.
- README, architecture diagram, data/source disclosure, demonstration video, and measured resume bullets.

## 16. Acceptance Criteria

P0 is complete when:

1. A fresh environment starts from documented commands and applies database migrations.
2. Both connector fixtures ingest normalized, versioned notices through the same protocol used by live connectors.
3. The representative query completes the confirmed LangGraph flow and produces a cited online report.
4. Every factual report claim resolves to an immutable evidence span and source version.
5. DOCX output matches the structured online report and can be retried without duplicating the logical report.
6. A recurring query is explicitly confirmed, scheduled, locked against duplicate execution, and reports only new or materially changed notices.
7. A connector outage demonstrates partial results and a visible completeness warning.
8. A transient node failure resumes from a checkpoint without repeating successful upstream model work.
9. The fixed evaluation command publishes all agreed metrics and clearly distinguishes targets from measured values.
10. The six critical Playwright flows and all backend test layers pass in the documented environment.
11. The deployed demonstration visibly labels live-source records and demonstration snapshots.
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
