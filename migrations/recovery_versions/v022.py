from migrations.recovery_orchestration import RecoveryOrchestrationSpec


SPEC = RecoveryOrchestrationSpec(
    version=22,
    filename="022_authentication.sql",
    sha256=(
        "c3981f525d296bf2d5103d53651cc7f4"
        "f12374ed93cdcb9276f726bc38b03292"
    ),
    target_count_error="recovery requires exactly one migration 022 file",
    target_identity_error="recovery target is not the reviewed migration 022",
    digest_changed_error=(
        "APPLYING 022 state digest changed; run a new read-only "
        "inspection before recovery"
    ),
    unsafe_error_prefix="APPLYING 022 recovery refused unsafe state: ",
    partial_error_prefix=(
        "migration 022 replay did not reach COMPLETE; "
        "history remains APPLYING. "
    ),
    unknown_classification_prefix=(
        "unknown APPLYING 022 inspection classification: "
    ),
)
