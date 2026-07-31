from enum import StrEnum


class SourceName(StrEnum):
    CCGP = "ccgp"
    GGZY = "ggzy"
    SYNTHETIC_DEMO = "synthetic_demo"


class CaptureKind(StrEnum):
    RAW_RESPONSE = "raw_response"
    CURATED_PUBLIC_EXCERPT = "curated_public_excerpt"
    SYNTHETIC_DEMO = "synthetic_demo"


class RunStatus(StrEnum):
    PENDING = "pending"
    PARSE_INTENT = "parse_intent"
    VALIDATE_INTENT = "validate_intent"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRM_INTENT = "confirm_intent"
    BUILD_RETRIEVAL_PLAN = "build_retrieval_plan"
    RETRIEVE_CANDIDATES = "retrieve_candidates"
    CANDIDATES_RESOLVED = "candidates_resolved"
    RESOLVE_DUPLICATES = "resolve_duplicates"
    VERIFY_EVIDENCE = "verify_evidence"
    SYNTHESIZE_REPORT = "synthesize_report"
    VALIDATE_REPORT = "validate_report"
    PERSIST_AND_DELIVER = "persist_and_deliver"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYABLE = "retryable"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"


class ImportStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    INVALID = "invalid"


class InboxEventType(StrEnum):
    NEW_NOTICE = "new_notice"
    MATERIAL_CHANGE = "material_change"
    SOURCE_COMPLETENESS_WARNING = "source_completeness_warning"
    RUN_FAILURE = "run_failure"


class ClaimSupportStatus(StrEnum):
    """Semantic Citation Contract §3: how the cited evidence relates to a claim.

    The judgment is about the *relationship* between the current citation
    evidence set and the claim — never about the claim's truth in the world.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"
