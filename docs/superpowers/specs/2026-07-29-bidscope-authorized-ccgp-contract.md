# Authorized CCGP Snapshot Contract

**Status:** Proposed — pending source authorization and pilot-owner approval

**Date:** 2026-07-29

## Contract boundary

The contract extends the existing `SnapshotManifest` without changing the
meaning of schema version 1. Existing demo and historical official fixtures
remain readable. A schema version 2 manifest is required for a new authorized
pilot batch and must include a bounded `data_contract` object plus an explicit
`batch_id`.

The contract is validated before adapter parsing, database writes, object-store
writes or search indexing.

## Schema version 2 requirements

`data_contract` contains:

- `contract_version`, identifying the source contract revision;
- `authorization_ref`, an opaque external governance reference;
- `data_owner`, a bounded owner label;
- `regions` and `categories`, both non-empty bounded lists;
- `review_status`, which must be `approved` for admission;
- `reviewed_at`, a timezone-aware review timestamp;
- `update_sla`, fixed to `weekly` for this pilot;
- `retention_days`, a positive bounded retention value.

`batch_id` is a bounded identifier and is not interpreted as a filesystem path.
The contract rejects empty values, control characters, overlong identifiers,
non-approved review status, naive timestamps and invalid retention values.
`review_status=approved` is an attestation from the separately controlled
governance process; the local manifest validator cannot prove a legal grant or
replace the external authorization record. Operators must retain that record
and its review trail outside the repository before staging admission.

## Quarantine semantics

Inspection returns a structured `quarantined` disposition for any manifest or
payload that fails the contract, provenance, path, file-type or hash checks.
Quarantine is a pre-admission decision: no `SnapshotBundle`, `SnapshotImport`,
notice version or object-store payload is created. The CLI keeps the existing
`valid`/`invalid` status for compatibility and exposes the disposition as an
additional machine-readable field.

## Immutability and reprocessing

- A repeated import of the same bundle and notice content remains idempotent.
- A new batch or changed notice content creates a new immutable version.
- The previous evidence span and source version remain addressable.
- A failed transaction leaves no partially searchable records.

## Non-goals

This contract does not fetch URLs, enforce a legal agreement, store the legal
agreement itself, add accounts/RBAC, or permit synthetic records to use an
official source identity.
