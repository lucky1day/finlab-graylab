from __future__ import annotations

from datetime import date

from sqlalchemy import text

from harness.context import GateContext
from harness.gates.live_gate import LiveGate
from harness.result import Evidence
from shared.blackbox_v2.contracts import load_metadata
from shared.calendar_service import get_calendar
from shared.data_bridge.refresh import DataBridgeRefreshConfig
from shared.input_artifacts import read_blackbox_current_state
from shared.models import PredictionRecord
from shared.prediction_context import (
    build_daily_live_context,
    build_monthly_live_context,
    build_weekly_live_context,
)


class GrayBackfillGate(LiveGate):
    """Blackbox V2 历史灰度补齐专用 Gate。"""

    name = "gray-backfill"
    authorization_action = "gray_backfill_write"
    required_prediction_phase = "gray_live"
    blackbox_snapshot_mode = "historical_as_of_replay"
    blackbox_only = True

    def _mode_preflight(self, ctx: GateContext, cfg, engine):
        return _gray_backfill_preflight(ctx, cfg, engine)

    def _execution_mode_kwargs(
        self,
        ctx: GateContext,
        cfg,
        mode_evidence: list[Evidence],
    ) -> dict:
        values = {item.key: item.value for item in mode_evidence}
        generation_id = str(values.get("current_generation_id") or "")
        refresh_date = str(values.get("current_refresh_date") or "")
        feature_date = str(values.get("candidate_feature_date") or "")
        target_date = str(values.get("candidate_target_date") or "")
        return {
            "blackbox_expected_generation_id": generation_id,
            "blackbox_expected_refresh_date": refresh_date,
            "blackbox_record_validator": lambda records: (
                _validate_gray_backfill_records(
                    records,
                    expected_generation_id=generation_id,
                    expected_refresh_date=refresh_date,
                    expected_feature_date=feature_date,
                    expected_target_date=target_date,
                    frequency=getattr(cfg, "frequency", None),
                )
            ),
        }


def _gray_backfill_preflight(
    ctx: GateContext,
    cfg,
    engine,
) -> tuple[list[Evidence], list[str]]:
    errors: list[str] = []
    data_bridge_config = DataBridgeRefreshConfig.from_env()
    state = read_blackbox_current_state(
        schema_path=data_bridge_config.schema_path,
        data_root=data_bridge_config.data_root,
        refresh_runtime_root=data_bridge_config.runtime_root,
    )
    refresh_date = _canonical_date(state.get("refresh_date"), "refresh_date")
    generation_id = str(state.get("generation_id") or "")
    predict_date = _canonical_date(ctx.predict_date, "predict_date")
    if not generation_id:
        errors.append("DataBridge current generation_id must be non-empty")
    if predict_date > refresh_date:
        errors.append(
            "gray-backfill predict_date must be on or before current DataBridge "
            f"refresh_date: predict_date={predict_date}, refresh_date={refresh_date}"
        )

    if cfg.delivery_metadata is None:
        errors.append("Blackbox delivery metadata path is required")
        return _preflight_evidence(
            generation_id=generation_id,
            refresh_date=refresh_date,
        ), errors
    metadata = load_metadata(cfg.delivery_metadata)
    calendar = get_calendar(engine)
    if metadata.frequency == "daily":
        context = build_daily_live_context(
            calendar,
            predict_date,
            horizon=metadata.horizon,
        )
    elif metadata.frequency == "weekly":
        context = build_weekly_live_context(calendar, predict_date)
    elif metadata.frequency == "monthly":
        context = build_monthly_live_context(calendar, predict_date)
    else:
        errors.append(f"unsupported Blackbox frequency: {metadata.frequency}")
        return _preflight_evidence(
            generation_id=generation_id,
            refresh_date=refresh_date,
        ), errors

    historical_target_max, existing_target_count = (
        _read_gray_backfill_database_state(
            engine,
            scheme_id=cfg.scheme_id,
            target_tenor=metadata.target_tenor,
            horizon=metadata.horizon,
            candidate_target_date=context.target_date,
        )
    )
    if historical_target_max is None:
        errors.append(
            "gray-backfill requires a latest historical backtest using "
            "blackbox_v2_current_snapshot_as_of"
        )
    elif context.target_date <= historical_target_max:
        errors.append(
            "gray-backfill target_date must be after latest historical target: "
            f"target_date={context.target_date}, historical_target_max="
            f"{historical_target_max}"
        )
    if existing_target_count != 0:
        errors.append(
            "gray-backfill target already exists in t_scheme_predictions: "
            f"target_date={context.target_date}, count={existing_target_count}"
        )

    return _preflight_evidence(
        generation_id=generation_id,
        refresh_date=refresh_date,
        feature_date=context.feature_date,
        target_date=context.target_date,
        historical_target_max=historical_target_max,
        existing_target_count=existing_target_count,
    ), errors


def _read_gray_backfill_database_state(
    engine,
    *,
    scheme_id: str,
    target_tenor: str,
    horizon: int,
    candidate_target_date: str,
) -> tuple[str | None, int]:
    latest_run_sql = text(
        """
        SELECT id
        FROM v_latest_backtest_run
        WHERE scheme_id = :scheme_id
          AND data_source = 'blackbox_v2_current_snapshot_as_of'
          AND status = 'success'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """
    )
    max_target_sql = text(
        """
        SELECT MAX(target_date)
        FROM t_backtest_predictions
        WHERE run_id = :run_id
          AND target_tenor = :target_tenor
          AND horizon = :horizon
        """
    )
    existing_sql = text(
        """
        SELECT COUNT(*)
        FROM t_scheme_predictions
        WHERE scheme_id = :scheme_id
          AND target_tenor = :target_tenor
          AND horizon = :horizon
          AND target_date = :target_date
        """
    )
    with engine.connect() as conn:
        run_id = conn.execute(
            latest_run_sql,
            {"scheme_id": scheme_id},
        ).scalar_one_or_none()
        historical_target = None
        if run_id is not None:
            historical_target = conn.execute(
                max_target_sql,
                {
                    "run_id": int(run_id),
                    "target_tenor": target_tenor,
                    "horizon": int(horizon),
                },
            ).scalar_one_or_none()
        existing_count = conn.execute(
            existing_sql,
            {
                "scheme_id": scheme_id,
                "target_tenor": target_tenor,
                "horizon": int(horizon),
                "target_date": candidate_target_date,
            },
        ).scalar_one()
    return (
        str(historical_target)[:10] if historical_target is not None else None,
        int(existing_count),
    )


def _validate_gray_backfill_records(
    records: list[PredictionRecord],
    *,
    expected_generation_id: str,
    expected_refresh_date: str,
    expected_feature_date: str | None = None,
    expected_target_date: str | None = None,
    frequency: str | None = None,
) -> None:
    """提交前核对历史灰度记录的实际快照与 cutoff provenance。"""
    if not records:
        raise ValueError("gray-backfill must return at least one prediction record")
    for record in records:
        extra = dict(record.extra or {})
        if extra.get("data_generation_id") != expected_generation_id:
            raise ValueError(
                "gray-backfill record data generation does not match preflight: "
                f"expected={expected_generation_id}, "
                f"actual={extra.get('data_generation_id')}"
            )
        if extra.get("source_refresh_date") != expected_refresh_date:
            raise ValueError(
                "gray-backfill record source refresh_date does not match preflight: "
                f"expected={expected_refresh_date}, "
                f"actual={extra.get('source_refresh_date')}"
            )
        if expected_feature_date and record.feature_date != expected_feature_date:
            raise ValueError(
                "gray-backfill record feature_date does not match preflight: "
                f"expected={expected_feature_date}, actual={record.feature_date}"
            )
        if expected_target_date and record.target_date != expected_target_date:
            raise ValueError(
                "gray-backfill record target_date does not match preflight: "
                f"expected={expected_target_date}, actual={record.target_date}"
            )
        cutoff_fields = (
            "daily_cutoff_key",
            "weekly_cutoff_key",
            "monthly_cutoff_key",
        )
        missing = [field for field in cutoff_fields if not extra.get(field)]
        if missing:
            raise ValueError(
                f"gray-backfill record cutoff provenance is incomplete: {missing}"
            )
        if frequency == "daily" and extra["daily_cutoff_key"] != record.feature_date:
            raise ValueError(
                "gray-backfill daily_cutoff_key must equal feature_date: "
                f"cutoff={extra['daily_cutoff_key']}, feature_date={record.feature_date}"
            )


def _preflight_evidence(
    *,
    generation_id: str,
    refresh_date: str,
    feature_date: str | None = None,
    target_date: str | None = None,
    historical_target_max: str | None = None,
    existing_target_count: int | None = None,
) -> list[Evidence]:
    return [
        Evidence("current_generation_id", generation_id),
        Evidence("current_refresh_date", refresh_date),
        Evidence("candidate_feature_date", feature_date),
        Evidence("candidate_target_date", target_date),
        Evidence("historical_target_max", historical_target_max),
        Evidence("existing_target_count", existing_target_count),
    ]


def _canonical_date(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a canonical YYYY-MM-DD date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical YYYY-MM-DD date")
    return value
