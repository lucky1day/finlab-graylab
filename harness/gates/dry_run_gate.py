from __future__ import annotations

from dataclasses import asdict
from typing import Any

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.gates.prediction_semantics import validate_live_record_semantics
from harness.probes.table_guard import DRY_RUN_GUARD_TABLES, diff_snapshots, snapshot_table_counts
from harness.result import Evidence, GateResult, GateStatus
from shared.calendar_service import get_calendar
from shared.models import PredictionRecord
from shared.scheme_config_loader import load_yaml_mapping


COMMON_EXTRA_KEYS = ("input_artifact_path", "input_artifact_source")
DAILY_EXTRA_KEYS = ("feature_date",)
WEEKLY_EXTRA_KEYS = ("feature_week_id", "target_week_id", "feature_date", "target_date", "target_rule")
MONTHLY_EXTRA_KEYS = (
    "db_rdate",
    "trigger_date",
    "scheduled_trigger_date",
    "input_cutoff_date",
    "feature_month_id",
    "target_month_id",
    "feature_date",
    "target_date",
    "target_rule",
)


class DryRunGate(Gate):
    name = "dry-run"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_yaml_mapping(
            ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        )
        engine = ctx.engine_factory() if ctx.engine_factory is not None else _create_engine()
        before: dict[str, int] = {}
        after: dict[str, int] = {}
        records: list[PredictionRecord] = []
        errors: list[str] = []
        try:
            before = snapshot_table_counts(engine, DRY_RUN_GUARD_TABLES)
            try:
                records = run_scheme_subprocess(
                    ctx.scheme_id,
                    ctx.predict_date,
                    algo_env=ctx.algo_env,
                    timeout_sec=ctx.timeout_sec,
                )
            except Exception as exc:
                errors.append(str(exc))
            finally:
                after = snapshot_table_counts(engine, DRY_RUN_GUARD_TABLES)
            errors.extend(
                _validate_records(
                    records,
                    config,
                    ctx.scheme_id,
                    predict_date=ctx.predict_date,
                    calendar=get_calendar(engine),
                )
            )
        finally:
            if engine is not None and hasattr(engine, "dispose"):
                engine.dispose()

        deltas = diff_snapshots(before, after)
        for table, delta in deltas.items():
            if delta != 0:
                errors.append(f"{table} delta must be 0 for dry-run, got {delta}")

        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=[
                Evidence("prediction_count", len(records)),
                Evidence("table_counts_before", before),
                Evidence("table_counts_after", after),
                Evidence("table_deltas", deltas),
                Evidence("predictions_table_delta", deltas.get("t_scheme_predictions")),
                Evidence("run_log_delta", deltas.get("t_scheme_run_log")),
                Evidence("non_zero_deltas", {k: v for k, v in deltas.items() if v != 0}),
                Evidence("sample_record", asdict(records[0]) if records else None),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )


def run_scheme_subprocess(scheme_id: str, predict_date: str, algo_env: str, timeout_sec: int) -> list[PredictionRecord]:
    """懒加载 scheduler.executor.run_scheme_subprocess，保持 DryRunGate 使用既有执行路径。"""
    from scheduler.executor import run_scheme_subprocess as executor_run_scheme_subprocess

    return executor_run_scheme_subprocess(
        scheme_id,
        predict_date,
        algo_env=algo_env,
        timeout_sec=timeout_sec,
    )


def _create_engine():
    from scheduler.repository import create_engine_from_env

    return create_engine_from_env()


def _validate_records(
    records: list[PredictionRecord],
    config: dict[str, Any],
    scheme_id: str,
    *,
    predict_date: str,
    calendar: Any | None = None,
) -> list[str]:
    errors: list[str] = []
    frequency = str(config.get("frequency", "") or "")
    horizon = int(config.get("horizon", 0) or 0)
    tenors = [str(item) for item in config.get("tenors", [])]
    if len(records) != len(tenors):
        errors.append(f"prediction count must equal configured tenors: expected {len(tenors)}, got {len(records)}")
    for index, record in enumerate(records):
        prefix = f"record[{index}]"
        if record.scheme_id != scheme_id:
            errors.append(f"{prefix}.scheme_id expected {scheme_id}, got {record.scheme_id}")
        if record.horizon != horizon:
            errors.append(f"{prefix}.horizon expected {horizon}, got {record.horizon}")
        if record.target_tenor not in tenors:
            errors.append(f"{prefix}.target_tenor {record.target_tenor} not in {tenors}")
        if record.predicted_direction not in {-1, 0, 1}:
            errors.append(f"{prefix}.predicted_direction must be -1/0/1, got {record.predicted_direction}")
        errors.extend(
            validate_live_record_semantics(
                record,
                expected_predict_date=predict_date,
                prefix=prefix,
                require_phase=False,
                frequency=frequency,
                horizon=horizon,
                calendar=calendar,
                expected_weekly_target_rule=config.get("target_rule") if frequency == "weekly" else None,
                task_type=str(config.get("task_type") or ""),
            )
        )
        extra = record.extra or {}
        for key in COMMON_EXTRA_KEYS:
            if key not in extra:
                errors.append(f"{prefix}.extra missing {key}")
        if frequency == "daily":
            frequency_keys = DAILY_EXTRA_KEYS
        elif frequency == "weekly":
            frequency_keys = WEEKLY_EXTRA_KEYS
        elif frequency == "monthly":
            frequency_keys = MONTHLY_EXTRA_KEYS
        else:
            frequency_keys = ()
        for key in frequency_keys:
            if key not in extra:
                errors.append(f"{prefix}.extra missing {key}")
        if frequency == "weekly":
            expected_rule = config.get("target_rule")
            if expected_rule and extra.get("target_rule") != expected_rule:
                errors.append(f"{prefix}.extra.target_rule expected {expected_rule}, got {extra.get('target_rule')}")
        if frequency == "monthly":
            expected_rule = config.get("target_rule")
            if expected_rule and extra.get("target_rule") != expected_rule:
                errors.append(f"{prefix}.extra.target_rule expected {expected_rule}, got {extra.get('target_rule')}")
    return errors
