"""平台业务持久化的唯一公共入口；事务实现位于内部 persistence 包。"""

from __future__ import annotations

from scheduler.persistence.actuals import (
    ActualTailRepairPlan,
    ActualWriteStats,
    _upsert_actuals_detailed_conn,
    delete_actuals_after_source_watermark,
    plan_actuals_tail_repair,
    repair_actuals_after_source_watermark,
    repair_actuals_after_source_watermark_detailed,
    upsert_actuals,
    upsert_actuals_detailed,
    upsert_monthly_actuals,
    upsert_monthly_actuals_detailed,
    upsert_period_average_actuals,
    upsert_period_average_actuals_detailed,
    upsert_weekly_actuals,
    upsert_weekly_actuals_detailed,
)

from scheduler.persistence.connections import (
    create_engine_from_env,
)

from scheduler.persistence.lifecycle import (
    BlackboxLifecycleIdentityAbsent,
    _sync_scheme_registry_conn,
    activate_blackbox_initial,
    activate_blackbox_revision,
    active_native_identity_error,
    read_blackbox_execution_approval,
    read_blackbox_lifecycle_state,
    read_blackbox_revision_activation_preflight,
    registry_scheme_id,
    resolve_database_lifecycle,
)
from scheduler.persistence.predictions import (
    PARTIAL_PREDICTION_KEY_CONFLICT,
    PREDICTION_KEYS_ALREADY_EXIST,
    attach_run_data_snapshot,
    complete_active_native_run,
    complete_approved_blackbox_run,
    complete_gray_gap_run,
    complete_gray_gap_runs_atomic,
    create_scheme_run,
    fail_scheme_run_atomic,
    write_run_log,
)
