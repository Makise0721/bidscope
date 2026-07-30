# ADR: Authorized CCGP Live-Ingestion Configuration Boundary

**Status:** Accepted for implementation; source authorization remains an external operator prerequisite

**Date:** 2026-07-30

## Decision

BidScope adds an explicit `ingestion` process role for a future authorized CCGP
live-data worker. Live acquisition is disabled by default. The API and
subscription scheduler remain snapshot-only and reject CCGP client/signing
credentials in their runtime configuration.

When enabled, configuration must provide:

- an HTTPS CCGP origin whose host is already present in the CCGP official-host
  allowlist;
- operator-provided client and signing material, held as `SecretStr` values;
- bounded polling, timeout, response-size, pagination, and interval limits; and
- a complete, approved schema-v2 data-contract configuration with an external
  authorization reference.

The settings layer validates the transport origin, role separation, and
configuration completeness before the worker can be started. It does not infer
an endpoint, signing algorithm, credential, rate limit, or authorization from
public documentation. Those values are supplied by the authorized operator.

## Consequences

- A production deployment can continue to run with no CCGP credentials when
  `BIDSCOPE_LIVE_INGESTION_ENABLED=false`.
- Any later live worker implementation has a narrow configuration boundary and
  cannot silently grant network access to API or scheduler processes.
- Secret rotation occurs in the protected ingestion environment and is not
  documented with literal secret values.
- Network authorization, legal approval, retention approval, and endpoint
  details remain operator-controlled and must be recorded outside Git.

## Rejected alternatives

- Public HTML scraping, browser automation, CAPTCHA bypass, WAF evasion, and
  undocumented endpoint probing.
- Reusing API or scheduler credentials for source acquisition.
- Enabling live traffic by default or falling back to public fetching when the
  authorized interface is unavailable.
