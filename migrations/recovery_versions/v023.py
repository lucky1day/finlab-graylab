from migrations.recovery_orchestration import RecoveryOrchestrationSpec


SPEC = RecoveryOrchestrationSpec(
    version=23,
    filename="023_auth_user_profiles.sql",
    sha256=(
        "393cb5266407f231292389c00e8ecbad"
        "ea1ad652e137405dd2eeb5cf633d55ec"
    ),
    target_count_error="recovery requires exactly one migration 023 file",
    target_identity_error="recovery target is not the reviewed migration 023",
    digest_changed_error=(
        "APPLYING 023 state digest changed; run a new read-only "
        "inspection before recovery"
    ),
    unsafe_error_prefix="APPLYING 023 recovery refused unsafe state: ",
    partial_error_prefix=(
        "migration 023 replay did not reach COMPLETE; "
        "history remains APPLYING. "
    ),
    unknown_classification_prefix=(
        "unknown APPLYING 023 inspection classification: "
    ),
)
