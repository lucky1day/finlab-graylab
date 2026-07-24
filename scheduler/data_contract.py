"""Scheduler-facing compatibility imports for the shared data contract."""

from shared.data_contract import (
    CALENDAR_SOURCE_TABLES,
    FACTOR_SOURCE_TABLES,
    METADATA_SOURCE_TABLE,
    SHANGHAI,
    LateSourceWrite,
    SourceCommitEvidence,
    SourceTableEvidence,
    assert_source_commit_evidence_at_cutoff,
    capture_source_commit_evidence,
    capture_source_commit_evidence_from_connection,
    detect_late_source_writes,
)

__all__ = [
    "CALENDAR_SOURCE_TABLES",
    "FACTOR_SOURCE_TABLES",
    "METADATA_SOURCE_TABLE",
    "SHANGHAI",
    "LateSourceWrite",
    "SourceCommitEvidence",
    "SourceTableEvidence",
    "assert_source_commit_evidence_at_cutoff",
    "capture_source_commit_evidence",
    "capture_source_commit_evidence_from_connection",
    "detect_late_source_writes",
]
