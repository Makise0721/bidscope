# BidScope Real-Data Evaluation Specification

**Status:** Proposed — pending pilot-owner approval

**Date:** 2026-07-29

## Purpose

Measure product usefulness on an access-controlled, versioned CCGP evaluation
set without confusing real-data evidence with the deterministic synthetic CI
gate.

## Dataset contract

Each restricted evaluation manifest records a dataset ID/version, the imported
snapshot bundle IDs and hashes, annotation-guide version, annotation set
version, record count, access class and creation timestamp. The dataset must
be derived from approved snapshot versions and must not contain synthetic
records disguised as official notices. A staging validator also requires a
read-only `snapshot-admission-catalog-v1` exported from the controlled import
record; its CCGP/schema/review fields and hashes must match the dataset
manifest.

## Result contract

Each real evaluation result records:

- result schema version and run ID;
- dataset ID/version and dataset-manifest hash;
- snapshot IDs/hashes and sample count;
- mode (`offline_baseline` or `staging_live_model`);
- provider, model, model version and prompt version;
- pricing snapshot date and measured cost;
- staging environment and failure policy;
- retrieval, deduplication, citation, latency, cost and human-usefulness
  metrics;
- explicit citation/provenance hard-gate outcomes.

The result uses no `target_pass` field. Thresholds for real data are selected
after the first baseline and reviewed by the pilot owner. Deterministic
`evaluation-result-v1` remains unchanged and continues to be the CI gate.

## Acceptance flow

1. Validate the dataset manifest and its hashes.
2. Run the offline/fake-model baseline.
3. Review retrieval, deduplication and citation evidence.
4. Run the optional live-model sample only in staging with bounded cost and
   recorded provider metadata.
5. Block release on any provenance or citation-support failure; review other
   thresholds against the measured baseline.

The repository command `eval validate-real` is the metadata and admission
gate only. It deliberately does not execute a provider, read prompts, or
access restricted records. The offline baseline and optional staging
live-model runner remain controlled staging operations and must produce the
result artifact before this gate can support a release decision.

## Failure and privacy rules

Provider errors, timeouts and sample failures are recorded with bounded error
codes and do not expose prompts, credentials, raw request headers or report
bodies. Restricted data and evaluation outputs remain outside deterministic CI
fixtures unless explicitly approved for publication.
