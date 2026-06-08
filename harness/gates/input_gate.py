from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from harness.config_loader import load_config_raw
from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus


class InputGate(Gate):
    name = "input"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_config_raw(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
        input_spec = config.get("input_spec") if isinstance(config.get("input_spec"), dict) else {}
        frequency = str(config.get("frequency", "")).strip()
        required_columns = [str(item) for item in input_spec.get("required_columns", [])]
        expected_data_version = str(input_spec.get("data_version", "") or "")

        engine = ctx.engine_factory() if ctx.engine_factory is not None else None
        try:
            artifact = self._build_artifact(ctx, frequency, engine)
        finally:
            if engine is not None and hasattr(engine, "dispose"):
                engine.dispose()

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
            errors.append(f"missing required input columns: {missing_required_cols}")
        if not date_coverage or date_coverage.get("start") is None or date_coverage.get("end") is None:
            errors.append("input artifact date_coverage must include start and end")
        if not source.startswith("shared_data_service_"):
            errors.append(f"input artifact source must come from shared data service: {source}")
        if expected_data_version and data_version != expected_data_version:
            errors.append(f"input artifact data_version mismatch: expected {expected_data_version}, got {data_version}")

        evidence = [
            Evidence("frequency", frequency),
            Evidence("input_artifact_path", str(getattr(artifact, "path", ""))),
            Evidence("source", source),
            Evidence("data_version", data_version),
            Evidence("row_count", int(getattr(artifact, "row_count", 0))),
            Evidence("column_count", int(getattr(artifact, "column_count", 0))),
            Evidence("columns", artifact_columns),
            Evidence("required_columns", required_columns),
            Evidence("date_coverage", date_coverage),
            Evidence("quality_flags", quality_flags),
            Evidence("missing_required_cols", missing_required_cols),
        ]
        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=evidence,
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _build_artifact(self, ctx: GateContext, frequency: str, engine: Any):
        if frequency == "weekly":
            return build_weekly_input_artifact(
                scheme_id=ctx.scheme_id,
                predict_date=ctx.predict_date,
                engine=engine,
            )
        if frequency == "daily":
            end_date = ctx.predict_date
            start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=8 * 365)).strftime("%Y-%m-%d")
            return build_daily_input_artifact(
                scheme_id=ctx.scheme_id,
                predict_date=ctx.predict_date,
                start_date=start_date,
                end_date=end_date,
                engine=engine,
            )
        raise ValueError(f"unsupported frequency for InputGate: {frequency}")


def build_daily_input_artifact(**kwargs):
    from shared.input_artifacts import build_daily_input_artifact as builder

    return builder(**kwargs)


def build_weekly_input_artifact(**kwargs):
    from shared.input_artifacts import build_weekly_input_artifact as builder

    return builder(**kwargs)
