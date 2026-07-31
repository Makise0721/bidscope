"""Evidence extraction and verification for the BidScope query workflow.

Three pure layers bound to the frozen :mod:`bidscope.domain` contracts:

* :mod:`bidscope.evidence.extractor` — turns a verified opportunity's source
  text and the claims that cite it into immutable
  :class:`~bidscope.domain.notices.NoticeEvidence` spans with character offsets
  and a span hash. It never consults the network or the current time.
* :mod:`bidscope.evidence.validator` — checks that every reported claim cites
  evidence that exists, resolves to the same notice version, sits at valid
  offsets, and matches its stored span hash. A report that fails this gate is
  never delivered.
* :mod:`bidscope.evidence.semantic_verifier` — the Semantic Citation Contract
  boundary: deterministic ``validate_claim`` first, then an optional LLM
  verifier (:mod:`bidscope.evidence.fake_verifier` for offline runs,
  :mod:`bidscope.evidence.deepseek_verifier` for configured deployments)
  judges whether the cited evidence set supports each claim's wording.
  UNSUPPORTED claims are filtered from the main intelligence list; the full
  judgment record is persisted for audit and review.

The extractor and validator are written as pure functions so they can be
unit-tested without the graph and reused by the ``verify_evidence`` /
``validate_report`` / ``verify_semantics`` nodes.
"""
