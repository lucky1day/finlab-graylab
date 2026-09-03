from migrations.recovery_orchestration import RecoveryOrchestrationSpec


SPEC = RecoveryOrchestrationSpec(
    version=24,
    filename="024_scheme_prediction_fact_source.sql",
    sha256="a0888734c06859e8c9354b1f55dea991382c71b0247327de80372a72912098db",
    target_count_error="recovery requires exactly one migration 024 file",
    target_identity_error="recovery target is not the reviewed migration 024",
    digest_changed_error=(
        "APPLYING 024 state digest changed; run a new read-only inspection before recovery"
    ),
    unsafe_error_prefix="APPLYING 024 recovery refused unsafe state: ",
    partial_error_prefix=(
        "migration 024 replay did not reach COMPLETE; history remains APPLYING. "
    ),
    unknown_classification_prefix=(
        "unknown APPLYING 024 inspection classification: "
    ),
)
