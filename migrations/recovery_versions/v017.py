from migrations.recovery_orchestration import RecoveryOrchestrationSpec


SPEC = RecoveryOrchestrationSpec(
    version=17,
    filename="017_daily_schedule_ledger.sql",
    sha256=(
        "a405ba82a857fc36252256c91fa9719e5"
        "7b63007830bd9acf734f5f8e0c743fb"
    ),
    target_count_error="recovery requires exactly one migration 017 file",
    target_identity_error="recovery target is not the reviewed migration 017",
    digest_changed_error=(
        "APPLYING 017 state digest changed; run a new read-only "
        "inspection before recovery"
    ),
    unsafe_error_prefix="APPLYING 017 recovery refused unsafe state: ",
    partial_error_prefix=(
        "migration 017 replay did not reach COMPLETE; "
        "history remains APPLYING. "
    ),
    unknown_classification_prefix=(
        "unknown APPLYING 017 inspection classification: "
    ),
)
