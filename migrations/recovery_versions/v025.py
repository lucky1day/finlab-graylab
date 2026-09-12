from migrations.recovery_orchestration import RecoveryOrchestrationSpec


SPEC = RecoveryOrchestrationSpec(
    version=25,
    filename="025_drop_platform_confidence.sql",
    sha256="1febb0851702b411a6b5a244c8621ba3e5f4c4a2d2682662abe65b91d7eb5a85",
    target_count_error="recovery requires exactly one migration 025 file",
    target_identity_error="recovery target is not the reviewed migration 025",
    digest_changed_error=(
        "APPLYING 025 state digest changed; run a new read-only inspection before recovery"
    ),
    unsafe_error_prefix="APPLYING 025 recovery refused unsafe state: ",
    partial_error_prefix="migration 025 replay did not reach COMPLETE; history remains APPLYING. ",
    unknown_classification_prefix="unknown APPLYING 025 inspection classification: ",
)
