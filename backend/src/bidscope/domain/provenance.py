"""Shared provenance validation for notices and snapshot manifests.

The integrity of the audit trail depends on a notice's declared source,
capture kind, source-URL host and external identifier agreeing with one
another. Both :class:`~bidscope.domain.snapshots.SnapshotManifest` and
:class:`~bidscope.domain.notices.NormalizedNotice` route through
:func:`validate_provenance` so the rule lives in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from bidscope.domain.enums import CaptureKind, SourceName

OFFICIAL_HOSTS = {"www.ccgp.gov.cn", "search.ccgp.gov.cn", "www.ggzy.gov.cn"}
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
      must declare an official source, resolve to a host in
      :data:`OFFICIAL_HOSTS` and must not impersonate a synthetic id.
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
        if host not in OFFICIAL_HOSTS:
            errors.append(
                f"official bundles may only reference {sorted(OFFICIAL_HOSTS)}; got {host}"
            )
        if external_id.startswith(_SYNTHETIC_ID_PREFIX):
            errors.append(
                f"official capture kinds must not use synthetic prefix "
                f"'{_SYNTHETIC_ID_PREFIX}'; got {external_id!r}"
            )

    return ProvenanceValidation(valid=not errors, errors=tuple(errors))


def allowed_hosts_for(capture_kind: CaptureKind) -> frozenset[str]:
    if capture_kind == CaptureKind.SYNTHETIC_DEMO:
        return frozenset({SYNTHETIC_HOST})
    return frozenset(OFFICIAL_HOSTS)


def expected_id_prefixes(capture_kind: CaptureKind) -> tuple[str, ...]:
    if capture_kind == CaptureKind.SYNTHETIC_DEMO:
        return (_SYNTHETIC_ID_PREFIX,)
    return ()
