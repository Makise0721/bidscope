# BidScope Single-Tenant Access ADR

**Status:** Accepted for the first productization pilot

**Date:** 2026-07-29

## Context

The P1 deployment is a single-tenant administrator console protected by one
`X-Admin-Token`. Productization needs an explicit access decision before any
schema or authentication work because an operator credential is not a customer
identity system.

## Decision

Remain single-tenant for the first internal-team pilot.

- Keep the existing Admin Token boundary and protected routes.
- Do not add users, organizations, sessions, cookies, OAuth, RBAC, quotas or
  tenant foreign keys as part of data productization.
- Treat audit attribution as an operator/deployment context, not as a user
  identity claim.
- Re-open the decision only when an external customer, multiple organization,
  per-user attribution or independent data isolation is a confirmed requirement.

## Alternatives considered

### Introduce multi-tenancy first

Rejected for the pilot. It would add migrations, authorization policy, session
security and isolation testing before real-data usefulness is established.

### Add an account system but keep one tenant

Deferred. It creates identity and session obligations without changing the
pilot's operational need.

## Consequences

- No public API or database migration is needed for access control in this
  slice.
- The current Admin Token remains an operational secret and is never treated as
  a person identifier.
- A future multi-tenant implementation must be a separate ADR and migration
plan with explicit isolation and export permissions.
