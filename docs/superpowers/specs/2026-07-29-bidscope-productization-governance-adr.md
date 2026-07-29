# BidScope Productization Governance ADR

**Status:** Proposed — pending pilot-owner approval

**Date:** 2026-07-29

## Context

P1 hardened BidScope's snapshot-only ingestion and single-operator deployment,
but it has not yet been evaluated against authorized real tender data. The
first productization target is an internal team that needs weekly intelligence
over CCGP central public tenders. The repository must not turn that goal into a
live crawler or a customer-account system by accident.

## Decision

The first pilot will use one authorized, manually reviewed CCGP data path:

- capture kind is `curated_public_excerpt`;
- source coverage is limited to CCGP central public tenders selected by the
  pilot owner;
- imports are weekly batches through the existing local snapshot CLI;
- the data owner, authorization reference, review status, coverage, retention
  and correction process are recorded in a versioned data-contract manifest;
- only an approved contract can pass the searchable-data admission gate;
- real pilot data stays in access-controlled staging by default and is not
  committed to Git or copied into deterministic CI fixtures.

The source contract is a governance boundary, not permission to access a live
website. Any future provider feed or online source requires a separate written
authorization and design.

## Required governance record

Before a real batch is imported, the operator must have an external record for:

1. data owner and authorization basis;
2. covered regions/categories and source URLs;
3. weekly update SLA and cost/rate limits for the authorized acquisition path;
4. retention period and storage classification;
5. correction, takedown and reprocessing contacts;
6. the opaque authorization reference placed in the manifest.

The manifest stores bounded references and review metadata, never credentials,
cookies, captcha material, or the full legal agreement.

## Alternatives considered

### Live CCGP crawler

Rejected. It would violate the existing source policy and introduce WAF,
captcha, authorization and SSRF risks before the product value is known.

### Provider feed as the first source

Deferred. It may be the right scale path, but it needs a provider contract,
field SLA and cost model that are not available in the current baseline.

### Multiple sources in the first pilot

Rejected. It would mix source-specific quality and governance failures, making
the CCGP pilot impossible to diagnose.

## Consequences

- Existing `inspect` and `import` commands remain the only ingestion path.
- An invalid or unapproved bundle is quarantined before database/object writes
  and cannot become searchable.
- Weekly operations are explicit and auditable rather than hidden in an
  interactive query.
- The pilot cannot claim market performance until the restricted evaluation and
  staging acceptance gates pass.
- A later SaaS/multi-tenant decision must be recorded separately.
