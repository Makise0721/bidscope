# Authorized live-ingestion staging runbook

This runbook is for an operator-approved, non-production staging acceptance of
the authorized CCGP ingestion boundary. It does not authorize scraping,
undocumented endpoints, credential sharing, or production traffic.

## Preconditions

- A human owner has recorded the CCGP authorization reference, data owner,
  approved endpoint contract, coverage, retention period, correction/withdrawal
  policy, rate limit, and incident contact outside Git.
- The operator has confirmed that the staging authorization permits the exact
  endpoint and fields configured for the run.
- `BIDSCOPE_CCGP_RUNNER_FACTORY` points to the reviewed operator-supplied
  `module:attribute` entry point that constructs the exact endpoint contract
  and signing implementation. The repository intentionally does not provide
  a guessed endpoint or signing algorithm.
- The staging database and object store are disposable or backed up, migrated
  to the live-ingestion revision, and reachable only from the isolated
  ingestion process role.
- Credentials are injected into the ingestion process environment at runtime.
  They must not appear in command lines, logs, fixtures, screenshots, reports,
  object keys, or this repository.

## Offline gate

Run the deterministic fixture and all database-free quality checks before any
external request:

```text
uv run pytest backend/tests/unit backend/tests/contract backend/tests/security -q
npm run test:web
npm --prefix web run build
```

The fixture in
`backend/tests/fixtures/authorized_ingestion.py` covers pagination, a notice
correction, a withdrawal, a bounded timeout, and recovery from the same cursor.
It must remain network-free and synthetic.

## Controlled staging sequence

1. Record the operator, authorization reference, code revision, contract
   version, planned start time, and rollback owner in the restricted staging
   change record.
2. Confirm the API process has `process_role=api` and live ingestion disabled.
   Inject CCGP credentials only into the isolated ingestion process, with
   `process_role=ingestion` and `live_ingestion_enabled=true`.
3. Start one ingestion worker and verify the process health/readiness check.
   A second worker may be started only to verify the PostgreSQL advisory-lock
   skip behavior; it must not create a second acquisition run.
4. Run one bounded acquisition. Observe only the source ID, run status,
   bounded failure code, request/record/import counts, timestamps, and SHA-256
   prefixes in the operator view. Do not copy response bodies, request URLs
   with query strings, headers, cursor values, or object keys into evidence.
5. Verify the first successful run advances the cursor only after materialize,
   inspect, import, and audit complete. Verify a duplicate run reuses the
   immutable bundle and does not create a duplicate notice version.
6. In a synthetic or operator-approved staging correction/withdrawal case,
   verify the notice history changes while the original immutable response
   bundle remains available under the retention policy.
7. Exercise one bounded failure case at a time: timeout/5xx, 429, rejected
   authorization, oversized response, and malformed payload. Confirm the
   cursor remains unchanged and the source status is respectively `failed`,
   `rate_limited`, or `quarantined` with no secret-bearing diagnostic.
8. Stop the worker, restart it, and confirm cursor recovery resumes from the
   last committed cursor. Release the worker lock during shutdown.

## Acceptance evidence

Store a signed, access-controlled acceptance record containing only:

- code revision and contract/authorization reference;
- source ID, run IDs, start/finish timestamps, status and bounded failure codes;
- request, record, bundle, and imported-notice counts;
- response and manifest SHA-256 values or approved prefixes;
- cursor advancement result (`advanced`/`unchanged`), duplicate-import result,
  and recovery result;
- operator, reviewer, acceptance decision, and any incident reference.

Never store live response bodies, credentials, signed requests, request
headers, unrestricted URLs, raw cursor values, or production data in the
acceptance record.

## Stop and rollback

Stop immediately if authorization, host policy, contract, retention, source
coverage, or incident contact is unclear; if a credential appears outside the
ingestion role; if a cursor advances after a failed import; if a response is
not quarantined; or if a duplicate creates a new notice version.

Disable `live_ingestion_enabled`, stop the ingestion process, preserve only
the bounded run metadata and hashes, and follow the staging database/object
store rollback procedure. Do not delete evidence before the incident owner
has confirmed retention and recovery requirements.

Production readiness requires a separate human approval of authorization,
retention, operational limits, incident ownership, and real-data evaluation.
