from migrations.recovery_orchestration import RecoveryOrchestrationSpec


SPEC = RecoveryOrchestrationSpec(
    version=21,
    filename="021_registry_owner.sql",
    sha256=(
        "d9c3332d0e923a0220f68d5ca7c70567"
        "6b3e9a5e0fd4f4579c0d08e1b2f6667f"
    ),
    target_count_error="recovery requires exactly one migration 021 file",
    target_identity_error="recovery target is not the reviewed migration 021",
    digest_changed_error=(
        "APPLYING 021 state digest changed; run a new read-only "
        "inspection before recovery"
    ),
    unsafe_error_prefix="APPLYING 021 recovery refused unsafe state: ",
    partial_error_prefix=(
        "migration 021 replay did not reach COMPLETE; "
        "history remains APPLYING. "
    ),
    unknown_classification_prefix=(
        "unknown APPLYING 021 inspection classification: "
    ),
)
