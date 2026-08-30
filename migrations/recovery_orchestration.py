from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecoveryOrchestrationSpec:
    """单个受支持 APPLYING migration 的不可变恢复合同。"""

    version: int
    filename: str
    sha256: str
    target_count_error: str
    target_identity_error: str
    digest_changed_error: str
    unsafe_error_prefix: str
    partial_error_prefix: str
    unknown_classification_prefix: str
