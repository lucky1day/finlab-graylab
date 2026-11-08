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


def _recovery_spec(version: int, filename: str, sha256: str) -> RecoveryOrchestrationSpec:
    """将受审身份与各版本共用的错误合同组合，保留逐字错误文本。"""
    label = f"{version:03d}"
    return RecoveryOrchestrationSpec(
        version=version, filename=filename, sha256=sha256,
        target_count_error=f"recovery requires exactly one migration {label} file",
        target_identity_error=f"recovery target is not the reviewed migration {label}",
        digest_changed_error=(
            f"APPLYING {label} state digest changed; run a new read-only "
            "inspection before recovery"
        ),
        unsafe_error_prefix=f"APPLYING {label} recovery refused unsafe state: ",
        partial_error_prefix=(
            f"migration {label} replay did not reach COMPLETE; "
            "history remains APPLYING. "
        ),
        unknown_classification_prefix=f"unknown APPLYING {label} inspection classification: ",
    )


# 身份取自逐版受审记录，不从现有 SQL 字节或目录内容推断。
RECOVERY_SPECS = (
    _recovery_spec(
        17, "017_daily_schedule_ledger.sql",
        "a405ba82a857fc36252256c91fa9719e57b63007830bd9acf734f5f8e0c743fb",
    ),
    _recovery_spec(
        18, "018_schedule_run_started_at_nullable.sql",
        "320cdf0877618330b8dbd52bb091e956987e916447cb41fc4f8d7a567b5b3ba1",
    ),
    _recovery_spec(
        19, "019_retire_scheme_serving_pointer.sql",
        "c5713935b4c33c492cac54f8cf85725b2353fc33079129b762a7ce868785b002",
    ),
    _recovery_spec(
        21, "021_registry_owner.sql",
        "d9c3332d0e923a0220f68d5ca7c705676b3e9a5e0fd4f4579c0d08e1b2f6667f",
    ),
    _recovery_spec(
        22, "022_authentication.sql",
        "c3981f525d296bf2d5103d53651cc7f4f12374ed93cdcb9276f726bc38b03292",
    ),
    _recovery_spec(
        23, "023_auth_user_profiles.sql",
        "393cb5266407f231292389c00e8ecbadea1ad652e137405dd2eeb5cf633d55ec",
    ),
    _recovery_spec(
        24, "024_scheme_prediction_fact_source.sql",
        "a0888734c06859e8c9354b1f55dea991382c71b0247327de80372a72912098db",
    ),
    _recovery_spec(
        25, "025_drop_platform_confidence.sql",
        "1febb0851702b411a6b5a244c8621ba3e5f4c4a2d2682662abe65b91d7eb5a85",
    ),
)

(
    SPEC_017, SPEC_018, SPEC_019, SPEC_021,
    SPEC_022, SPEC_023, SPEC_024, SPEC_025,
) = RECOVERY_SPECS
