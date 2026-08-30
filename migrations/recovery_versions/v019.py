from migrations.recovery_orchestration import RecoveryOrchestrationSpec


SPEC = RecoveryOrchestrationSpec(
    version=19,
    filename="019_retire_scheme_serving_pointer.sql",
    sha256=(
        "c5713935b4c33c492cac54f8cf85725b"
        "2353fc33079129b762a7ce868785b002"
    ),
    target_count_error="recovery requires exactly one migration 019 file",
    target_identity_error="recovery target is not the reviewed migration 019",
    digest_changed_error=(
        "APPLYING 019 state digest changed; run a new read-only "
        "inspection before recovery"
    ),
    unsafe_error_prefix="APPLYING 019 recovery refused unsafe state: ",
    partial_error_prefix=(
        "migration 019 replay did not reach COMPLETE; "
        "history remains APPLYING. "
    ),
    unknown_classification_prefix=(
        "unknown APPLYING 019 inspection classification: "
    ),
)
