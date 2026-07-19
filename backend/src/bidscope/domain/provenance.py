"""Shared provenance validation for notices and snapshot manifests.

The integrity of the audit trail depends on a notice's declared source,
capture kind, source-URL host and external identifier agreeing with one
another. Both :class:`~bidscope.domain.snapshots.SnapshotManifest` and
:class:`~bidscope.domain.notices.NormalizedNotice` route through
:func:`validate_provenance` so the rule lives in exactly one place.

Official sources map to a precise set of hosts: an official notice may only
be attributed to the host(s) operated by its declared publisher, preventing
one official source from impersonating another.
"""

from __future__ import annotations

from dataclasses import dataclass

from bidscope.domain.enums import CaptureKind, SourceName

#: Per-source host allowlist. An official notice's host must belong to its
#: declared publisher, which stops CCGP data being attributed to ggzy.gov.cn
#: and vice versa.
OFFICIAL_HOSTS_BY_SOURCE: dict[SourceName, frozenset[str]] = {
    SourceName.CCGP: frozenset({"www.ccgp.gov.cn", "search.ccgp.gov.cn"}),
    SourceName.GGZY: frozenset({"www.ggzy.gov.cn"}),
}
OFFICIAL_HOSTS: frozenset[str] = frozenset().union(*OFFICIAL_HOSTS_BY_SOURCE.values())
SYNTHETIC_HOST = "example.invalid"
_SYNTHETIC_ID_PREFIX = "demo-"


@dataclass(frozen=True)
class ProvenanceValidation:
    valid: bool
    errors: tuple[str, ...] = ()

    def raise_invalid(self) -> None:
        if not self.valid:
            raise ValueError("; ".join(self.errors))


def validate_provenance(
    *,
    source: SourceName,
    capture_kind: CaptureKind,
    host: str | None,
    external_id: str,
) -> ProvenanceValidation:
    """Return whether source / capture_kind / host / external_id agree.

    * ``synthetic_demo`` bundles must declare ``source=synthetic_demo``,
      resolve to ``example.invalid`` and carry an ``external_id`` prefixed
      with ``demo-``.
    * Official capture kinds (``raw_response``, ``curated_public_excerpt``)
      must declare an official source, resolve to a host in that source's
      :data:`OFFICIAL_HOSTS_BY_SOURCE` entry and must not impersonate a
      synthetic id.
    """
    errors: list[str] = []

    if host is None:
        errors.append("source URL has no host")
        return ProvenanceValidation(valid=False, errors=tuple(errors))

    if capture_kind == CaptureKind.SYNTHETIC_DEMO:
        if source != SourceName.SYNTHETIC_DEMO:
            errors.append("synthetic_demo bundles must declare source=synthetic_demo")
        if host != SYNTHETIC_HOST:
            errors.append(f"synthetic_demo URLs must use {SYNTHETIC_HOST}; got {host}")
        if not external_id.startswith(_SYNTHETIC_ID_PREFIX):
            errors.append(
                f"synthetic_demo external_id must start with '{_SYNTHETIC_ID_PREFIX}'; "
                f"got {external_id!r}"
            )
    else:
        if source == SourceName.SYNTHETIC_DEMO:
            errors.append(
                "official capture kinds must not use source=synthetic_demo"
            )
        if host == SYNTHETIC_HOST:
            errors.append(
                f"official capture kinds must not use {SYNTHETIC_HOST}; got {host}"
            )
        allowed = OFFICIAL_HOSTS_BY_SOURCE.get(source)
        if allowed is None:
            errors.append(
                f"official capture kinds require an official source; got {source.value}"
            )
        elif host not in allowed:
            errors.append(
                f"source {source.value} may only reference {sorted(allowed)}; got {host}"
            )
        if external_id.startswith(_SYNTHETIC_ID_PREFIX):
            errors.append(
                f"official capture kinds must not use synthetic prefix "
                f"'{_SYNTHETIC_ID_PREFIX}'; got {external_id!r}"
            )

    return ProvenanceValidation(valid=not errors, errors=tuple(errors))


def allowed_hosts_for(source: SourceName, capture_kind: CaptureKind) -> frozenset[str]:
    if capture_kind == CaptureKind.SYNTHETIC_DEMO:
        return frozenset({SYNTHETIC_HOST})
    return OFFICIAL_HOSTS_BY_SOURCE.get(source, frozenset())


def expected_id_prefixes(capture_kind: CaptureKind) -> tuple[str, ...]:
    if capture_kind == CaptureKind.SYNTHETIC_DEMO:
        return (_SYNTHETIC_ID_PREFIX,)
    return ()
