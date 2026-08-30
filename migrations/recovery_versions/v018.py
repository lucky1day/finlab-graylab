from migrations.recovery_orchestration import RecoveryOrchestrationSpec


SPEC = RecoveryOrchestrationSpec(
    version=18,
    filename="018_schedule_run_started_at_nullable.sql",
    sha256=(
        "320cdf0877618330b8dbd52bb091e956"
        "987e916447cb41fc4f8d7a567b5b3ba1"
    ),
    target_count_error="recovery requires exactly one migration 018 file",
    target_identity_error="recovery target is not the reviewed migration 018",
    digest_changed_error=(
        "APPLYING 018 state digest changed; run a new read-only "
        "inspection before recovery"
    ),
    unsafe_error_prefix="APPLYING 018 recovery refused unsafe state: ",
    partial_error_prefix=(
        "migration 018 replay did not reach COMPLETE; "
        "history remains APPLYING. "
    ),
    unknown_classification_prefix=(
        "unknown APPLYING 018 inspection classification: "
    ),
)
