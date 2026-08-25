from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from harness.context import GateContext
from harness.gates.base import Gate, create_default_engine, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from shared.prediction_context import build_monthly_live_context
from shared.scheme_config_loader import load_yaml_mapping


MONTHLY_SOURCE_START_DATE = "2010-01-01"


class InputGate(Gate):
    name = "input"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_yaml_mapping(
            ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        )
        input_spec = config.get("input_spec") if isinstance(config.get("input_spec"), dict) else {}
        frequency = str(config.get("frequency", "")).strip()
        required_columns = [str(item) for item in input_spec.get("required_columns", [])]
        expected_data_version = str(input_spec.get("data_version", "") or "")
        aux_specs = input_spec.get("auxiliary_inputs")
        auxiliary_inputs = aux_specs if isinstance(aux_specs, list) else []

        engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
        try:
            calendar = get_calendar(engine)
            monthly_context = build_monthly_live_context(calendar, ctx.predict_date) if frequency == "monthly" else None
            feature_date = (
                monthly_context.feature_date
                if monthly_context is not None
                else calendar.previous_trading_day(ctx.predict_date)
            )
            feature_week_id = (
                None
                if monthly_context is not None
                else calendar.week_id_for_date(feature_date)
            )
            artifact = self._build_artifact(
                ctx,
                frequency,
                engine,
                feature_date=feature_date,
                feature_week_id=feature_week_id,
            )
            auxiliary_results = self._build_auxiliary_artifacts(
                ctx,
                auxiliary_inputs,
                engine,
                feature_date=feature_date,
                feature_week_id=feature_week_id,
            )
        finally:
            if engine is not None and hasattr(engine, "dispose"):
                engine.dispose()

        errors: list[str] = []
        primary = self._artifact_validation(
            artifact,
            required_columns=required_columns,
            expected_data_version=expected_data_version,
        )
        errors.extend(primary["errors"])

        auxiliary_evidence: list[dict[str, Any]] = []
        for spec, aux_artifact, build_error in auxiliary_results:
            aux_frequency = str(spec.get("frequency", "") or "")
            if build_error is not None:
                errors.append(build_error)
                auxiliary_evidence.append(
                    {
                        "frequency": aux_frequency,
                        "error": build_error,
                    }
                )
                continue
            aux_required = [str(item) for item in spec.get("required_columns", [])]
            aux_expected_data_version = str(spec.get("data_version", "") or "")
            aux_validation = self._artifact_validation(
                aux_artifact,
                required_columns=aux_required,
                expected_data_version=aux_expected_data_version,
                error_prefix=f"auxiliary input ({aux_frequency}) ",
            )
            errors.extend(aux_validation["errors"])
            auxiliary_evidence.append(self._artifact_evidence(aux_frequency, aux_artifact, aux_validation))

        evidence = [
            Evidence("frequency", frequency),
            Evidence("input_artifact_path", str(getattr(artifact, "path", ""))),
            Evidence("source", primary["source"]),
            Evidence("data_version", primary["data_version"]),
            Evidence("row_count", int(getattr(artifact, "row_count", 0))),
            Evidence("column_count", int(getattr(artifact, "column_count", 0))),
            Evidence("columns", primary["columns"]),
            Evidence("required_columns", required_columns),
            Evidence("date_coverage", primary["date_coverage"]),
            Evidence("quality_flags", primary["quality_flags"]),
            Evidence("missing_required_cols", primary["missing_required_cols"]),
            Evidence("feature_date", feature_date),
            Evidence("feature_week_id", feature_week_id),
            Evidence("feature_month_id", monthly_context.feature_month_id if monthly_context is not None else None),
            Evidence("target_month_id", monthly_context.target_month_id if monthly_context is not None else None),
            Evidence("auxiliary_input_artifacts", auxiliary_evidence),
        ]
        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            evidence=evidence,
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _build_artifact(self, ctx: GateContext, frequency: str, engine: Any, *, feature_date: str, feature_week_id: int | None):
        if frequency == "weekly":
            return build_weekly_input_artifact(
                scheme_id=ctx.scheme_id,
                predict_date=ctx.predict_date,
                end_week=feature_week_id,
                as_of_date=feature_date,
                engine=engine,
            )
        if frequency == "daily":
            start_date, end_date = _daily_window(feature_date)
            return build_daily_input_artifact(
                scheme_id=ctx.scheme_id,
                predict_date=ctx.predict_date,
                start_date=start_date,
                end_date=end_date,
                engine=engine,
            )
        if frequency == "monthly":
            return build_monthly_input_artifact(
                scheme_id=ctx.scheme_id,
                predict_date=ctx.predict_date,
                start_date=MONTHLY_SOURCE_START_DATE,
                end_date=feature_date,
                engine=engine,
            )
        raise ValueError(f"unsupported frequency for InputGate: {frequency}")

    def _build_auxiliary_artifacts(
        self,
        ctx: GateContext,
        aux_specs: list,
        engine: Any,
        *,
        feature_date: str,
        feature_week_id: int | None,
    ) -> list[tuple[dict, Any, str | None]]:
        results: list[tuple[dict, Any, str | None]] = []
        for item in aux_specs:
            spec = item if isinstance(item, dict) else {}
            frequency = str(spec.get("frequency", "") or "")
            try:
                if frequency == "daily":
                    start_date, end_date = _daily_window(feature_date)
                    artifact = build_daily_input_artifact(
                        scheme_id=ctx.scheme_id,
                        predict_date=ctx.predict_date,
                        start_date=start_date,
                        end_date=end_date,
                        engine=engine,
                    )
                elif frequency == "weekly":
                    artifact = build_weekly_input_artifact(
                        scheme_id=ctx.scheme_id,
                        predict_date=ctx.predict_date,
                        end_week=feature_week_id,
                        as_of_date=feature_date,
                        engine=engine,
                    )
                elif frequency == "monthly":
                    start_date, end_date = _daily_window(feature_date)
                    artifact = build_monthly_input_artifact(
                        scheme_id=ctx.scheme_id,
                        predict_date=ctx.predict_date,
                        start_date=start_date,
                        end_date=end_date,
                        engine=engine,
                    )
                else:
                    raise ValueError(f"unsupported auxiliary input frequency: {frequency}")
            except Exception as exc:  # noqa: BLE001 - gate evidence should capture build failures.
                results.append((spec, None, f"auxiliary input ({frequency}) build failed: {exc}"))
            else:
                results.append((spec, artifact, None))
        return results

    def _artifact_validation(
        self,
        artifact: Any,
        *,
        required_columns: list[str],
        expected_data_version: str,
        error_prefix: str = "",
    ) -> dict[str, Any]:
        artifact_columns = list(getattr(artifact, "columns", []))
        missing_required = [col for col in required_columns if col not in artifact_columns]
        quality_flags = dict(getattr(artifact, "quality_flags", {}) or {})
        quality_missing = quality_flags.get("missing_required_cols", quality_flags.get("missing_required_columns", []))
        missing_required_cols = sorted(set(missing_required) | {str(col) for col in (quality_missing or [])})
        date_coverage = getattr(artifact, "date_coverage", {}) or {}
        source = str(getattr(artifact, "source", "") or "")
        data_version = str(getattr(artifact, "data_version", "") or "")

        errors: list[str] = []
        if missing_required_cols:
            errors.append(f"{error_prefix}missing required input columns: {missing_required_cols}")
        if not date_coverage or date_coverage.get("start") is None or date_coverage.get("end") is None:
            errors.append(f"{error_prefix}input artifact date_coverage must include start and end")
        if not source.startswith("shared_data_service_"):
            errors.append(f"{error_prefix}input artifact source must come from shared data service: {source}")
        if expected_data_version and data_version != expected_data_version:
            errors.append(
                f"{error_prefix}input artifact data_version mismatch: "
                f"expected {expected_data_version}, got {data_version}"
            )

        return {
            "errors": errors,
            "columns": artifact_columns,
            "quality_flags": quality_flags,
            "missing_required_cols": missing_required_cols,
            "date_coverage": date_coverage,
            "source": source,
            "data_version": data_version,
        }

    def _artifact_evidence(self, frequency: str, artifact: Any, validation: dict[str, Any]) -> dict[str, Any]:
        return {
            "frequency": frequency,
            "path": str(getattr(artifact, "path", "")),
            "source": validation["source"],
            "data_version": validation["data_version"],
            "row_count": int(getattr(artifact, "row_count", 0)),
            "column_count": int(getattr(artifact, "column_count", 0)),
            "date_coverage": validation["date_coverage"],
            "missing_required_cols": validation["missing_required_cols"],
        }


def _daily_window(predict_date: str) -> tuple[str, str]:
    end_date = predict_date
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=8 * 365)).strftime("%Y-%m-%d")
    return start_date, end_date


def get_calendar(engine: Any):
    from shared.calendar_service import get_calendar as calendar_factory

    return calendar_factory(engine)


def build_daily_input_artifact(**kwargs):
    from shared.input_artifacts import build_daily_input_artifact as builder

    return builder(**kwargs)


def build_weekly_input_artifact(**kwargs):
    from shared.input_artifacts import build_weekly_input_artifact as builder

    return builder(**kwargs)


def build_monthly_input_artifact(**kwargs):
    from shared.input_artifacts import build_monthly_input_artifact as builder

    return builder(**kwargs)
