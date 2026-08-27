from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.context import GateContext
from harness.gates.base import Gate, create_default_engine, guarded_result, utc_now
from harness.gates.prediction_semantics import validate_live_record_semantics
from harness.probes.table_guard import DRY_RUN_GUARD_TABLES, diff_snapshots, snapshot_table_counts
from harness.result import Evidence, GateResult, GateStatus
from shared.artifact_paths import safe_path_part
from shared.calendar_service import get_calendar
from shared.input_artifacts import (
    DAILY_DATA_VERSION,
    DEFAULT_OUTPUT_ROOT,
    MONTHLY_DATA_VERSION,
    WEEKLY_DATA_VERSION,
    input_artifact_path,
)
from shared.models import DIRECTION_VALUES, PredictionRecord
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
_INPUT_CONTRACTS = {
    "daily": {
        "data_version": DAILY_DATA_VERSION,
        "source": "shared_data_service_daily",
        "coverage_column": "date",
    },
    "weekly": {
        "data_version": WEEKLY_DATA_VERSION,
        "source": "shared_data_service_weekly",
        "coverage_column": "week_id",
    },
    "monthly": {
        "data_version": MONTHLY_DATA_VERSION,
        "source": "shared_data_service_monthly",
        "coverage_column": "month_id",
    },
}


class DryRunGate(Gate):
    name = "dry-run"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        config = load_yaml_mapping(
            ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        )
        engine = ctx.engine_factory() if ctx.engine_factory is not None else create_default_engine()
        before: dict[str, int] = {}
        after: dict[str, int] = {}
        records: list[PredictionRecord] = []
        errors: list[str] = []
        input_artifacts: list[dict[str, Any]] = []
        audit_receipts: dict[str, dict[str, Any]] = {}
        try:
            before = snapshot_table_counts(engine, DRY_RUN_GUARD_TABLES)
            with tempfile.TemporaryDirectory(
                prefix="bfl-native-input-audit-"
            ) as audit_name:
                audit_root = Path(audit_name).resolve(strict=True)
                os.chmod(audit_root, 0o700)
                input_root = audit_root / "inputs"
                phase_a_cache_root = audit_root / "phase-a-cache"
                try:
                    records = run_scheme_subprocess(
                        ctx.scheme_id,
                        ctx.predict_date,
                        algo_env=ctx.algo_env,
                        timeout_sec=ctx.timeout_sec,
                        input_audit_root=audit_root,
                        input_root=input_root,
                        phase_a_cache_root=phase_a_cache_root,
                    )
                    audit_receipts = _load_input_audit_receipts(audit_root)
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
                artifact_errors, input_artifacts = _validate_input_artifacts(
                    records,
                    config,
                    scheme_id=ctx.scheme_id,
                    predict_date=ctx.predict_date,
                    audit_receipts=audit_receipts,
                    trusted_input_root=input_root,
                    ephemeral_job_input=True,
                )
                errors.extend(artifact_errors)
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
            evidence=[
                Evidence("prediction_count", len(records)),
                Evidence("table_counts_before", before),
                Evidence("table_counts_after", after),
                Evidence("table_deltas", deltas),
                Evidence("predictions_table_delta", deltas.get("t_scheme_predictions")),
                Evidence("run_log_delta", deltas.get("t_scheme_run_log")),
                Evidence("non_zero_deltas", {k: v for k, v in deltas.items() if v != 0}),
                Evidence("input_artifacts", input_artifacts),
                Evidence("sample_record", asdict(records[0]) if records else None),
            ],
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )


def run_scheme_subprocess(
    scheme_id: str,
    predict_date: str,
    algo_env: str,
    timeout_sec: int,
    input_audit_root: Path,
    input_root: Path,
    phase_a_cache_root: Path,
) -> list[PredictionRecord]:
    """懒加载 scheduler.executor.run_scheme_subprocess，保持 DryRunGate 使用既有执行路径。"""
    from scheduler.executor import run_scheme_subprocess as executor_run_scheme_subprocess
    from shared.liwei_0616_cache_contract import (
        CACHE_MUTATION_POLICY_PRIVATE_BUILD,
    )

    return executor_run_scheme_subprocess(
        scheme_id,
        predict_date,
        algo_env=algo_env,
        timeout_sec=timeout_sec,
        native_input_audit_root=input_audit_root,
        ephemeral_native_runtime_root=input_root,
        native_cache_mutation_policy=CACHE_MUTATION_POLICY_PRIVATE_BUILD,
        native_phase_a_cache_root=phase_a_cache_root,
    )


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
        if record.predicted_direction not in DIRECTION_VALUES:
            errors.append(f"{prefix}.predicted_direction must be -1/0/1, got {record.predicted_direction}")
        errors.extend(
            validate_live_record_semantics(
                record,
                expected_predict_date=predict_date,
                prefix=prefix,
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


def _validate_input_artifacts(
    records: list[PredictionRecord],
    config: dict[str, Any],
    *,
    scheme_id: str,
    predict_date: str,
    audit_receipts: dict[str, dict[str, Any]] | None = None,
    trusted_input_root: Path = DEFAULT_OUTPUT_ROOT,
    ephemeral_job_input: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    """核验 dry-run 实际生成的输入文件，不再另跑一次 InputGate。"""
    if not records:
        return [], []

    input_spec = (
        config.get("input_spec")
        if isinstance(config.get("input_spec"), dict)
        else {}
    )
    primary_frequency = str(config.get("frequency", "") or "")
    specs = [
        (
            primary_frequency,
            input_spec,
            "input_artifact_path",
            "input_artifact_source",
        )
    ]
    auxiliary_inputs = input_spec.get("auxiliary_inputs")
    if isinstance(auxiliary_inputs, list):
        for item in auxiliary_inputs:
            spec = item if isinstance(item, dict) else {}
            frequency = str(spec.get("frequency", "") or "")
            specs.append(
                (
                    frequency,
                    spec,
                    f"{frequency}_input_artifact_path",
                    f"{frequency}_input_artifact_source",
                )
            )

    errors: list[str] = []
    evidence: list[dict[str, Any]] = []
    if audit_receipts is not None:
        expected_frequencies = {item[0] for item in specs}
        unexpected_receipts = sorted(
            set(audit_receipts) - expected_frequencies
        )
        if unexpected_receipts:
            errors.append(
                f"unexpected native input audit receipts: {unexpected_receipts}"
            )
    for frequency, spec, path_key, source_key in specs:
        contract = _INPUT_CONTRACTS.get(frequency)
        if contract is None:
            errors.append(f"unsupported input artifact frequency: {frequency}")
            continue

        configured_version = str(spec.get("data_version", "") or "")
        if configured_version != contract["data_version"]:
            errors.append(
                f"{frequency} input artifact data_version mismatch: "
                f"expected {contract['data_version']}, got {configured_version}"
            )

        paths = {
            str((record.extra or {}).get(path_key) or "")
            for record in records
        }
        sources = {
            str((record.extra or {}).get(source_key) or "")
            for record in records
        }
        if len(paths) != 1 or "" in paths:
            errors.append(
                f"{frequency} input artifact path must be present and identical "
                f"across predictions: {sorted(paths)}"
            )
            continue
        if sources != {contract["source"]}:
            errors.append(
                f"{frequency} input artifact source mismatch: "
                f"expected {contract['source']}, got {sorted(sources)}"
            )

        actual_path = Path(next(iter(paths)))
        expected_path = (
            trusted_input_root
            / "views"
            / safe_path_part(scheme_id)
            / f"{frequency}_output_{predict_date}.csv"
            if ephemeral_job_input
            else input_artifact_path(
                scheme_id=scheme_id,
                frequency=frequency,
                predict_date=predict_date,
            )
        )
        actual_absolute = Path(os.path.abspath(actual_path))
        expected_absolute = Path(os.path.abspath(expected_path))
        if actual_absolute != expected_absolute:
            errors.append(
                f"{frequency} input artifact path mismatch: "
                f"expected {expected_path}, got {actual_path}"
            )
            continue

        details, path_error = _controlled_input_file_details(
            actual_absolute,
            trusted_root=trusted_input_root,
        )
        if details is None:
            errors.append(f"{frequency} input artifact invalid: {path_error}")
            continue

        receipt = (
            audit_receipts.get(frequency)
            if audit_receipts is not None
            else None
        )
        if audit_receipts is not None and receipt is None:
            errors.append(
                f"{frequency} input artifact was not generated by the shared "
                "builder in this dry-run"
            )
        receipt_errors = _validate_audit_receipt(
            receipt,
            frequency=frequency,
            scheme_id=scheme_id,
            predict_date=predict_date,
            actual_path=actual_absolute,
            contract=contract,
            details=details,
            records=records,
        )
        errors.extend(receipt_errors)

        columns, first_key, read_error = _read_artifact_header(
            actual_path,
            coverage_column=str(contract["coverage_column"]),
        )
        required_columns = [
            str(item) for item in spec.get("required_columns", [])
        ]
        missing = [item for item in required_columns if item not in columns]
        if read_error is not None:
            errors.append(f"{frequency} input artifact invalid: {read_error}")
        if receipt is not None:
            receipt_columns = receipt.get("columns")
            if receipt_columns != columns:
                errors.append(
                    f"{frequency} input audit columns mismatch: "
                    f"receipt={receipt_columns!r}, csv={columns!r}"
                )
            try:
                receipt_row_count = int(receipt.get("row_count", 0))
            except (TypeError, ValueError):
                receipt_row_count = 0
            if receipt_row_count <= 0:
                errors.append(f"{frequency} input audit row_count must be positive")
            content_hash, csv_row_count = _hash_and_count_csv_rows(
                actual_absolute
            )
            if receipt.get("content_hash") != content_hash:
                errors.append(
                    f"{frequency} input audit content_hash mismatch"
                )
            if receipt_row_count != csv_row_count:
                errors.append(
                    f"{frequency} input audit row_count mismatch: "
                    f"receipt={receipt_row_count}, csv={csv_row_count}"
                )
        if missing:
            errors.append(
                f"{frequency} input artifact missing required columns: {missing}"
            )
        evidence.append(
            {
                "frequency": frequency,
                "path": str(actual_path),
                "source": receipt.get("source") if receipt is not None else None,
                "data_version": (
                    receipt.get("data_version") if receipt is not None else None
                ),
                "columns": columns,
                "required_columns": required_columns,
                "missing_required_cols": missing,
                "first_coverage_key": first_key,
                "date_coverage": (
                    receipt.get("date_coverage") if receipt is not None else None
                ),
                "row_count": receipt.get("row_count") if receipt is not None else None,
            }
        )
    return errors, evidence


def _controlled_input_file_details(
    path: Path,
    *,
    trusted_root: Path,
) -> tuple[os.stat_result | None, str | None]:
    """从可信输入根逐级拒绝 symlink，并要求最终目标为普通文件。"""
    root = Path(os.path.abspath(trusted_root))
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None, f"path escapes trusted input root: {path}"
    try:
        root_details = root.lstat()
        if (
            stat.S_ISLNK(root_details.st_mode)
            or not stat.S_ISDIR(root_details.st_mode)
        ):
            return None, f"trusted input root is unsafe: {root}"
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            details = current.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                return None, f"input artifact ancestor is unsafe: {current}"
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            return None, "input artifact must be a regular non-symlink file"
        resolved_root = root.resolve(strict=True)
        path.resolve(strict=True).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    return details, None


def _hash_and_count_csv_rows(path: Path) -> tuple[str, int]:
    """单次流式读取绑定最终字节并统计生成器 CSV 的数据行。"""
    digest = hashlib.sha256()
    newline_count = 0
    last_byte = b""
    total_bytes = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            newline_count += chunk.count(b"\n")
            last_byte = chunk[-1:]
            total_bytes += len(chunk)
    physical_lines = newline_count + (
        1 if total_bytes and last_byte != b"\n" else 0
    )
    return digest.hexdigest(), max(physical_lines - 1, 0)


def _load_input_audit_receipts(root: Path) -> dict[str, dict[str, Any]]:
    receipts: dict[str, dict[str, Any]] = {}
    for path in sorted(root.iterdir()):
        details = path.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or path.suffix != ".json"
        ):
            raise ValueError(f"unsafe native input audit receipt: {path.name}")
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f"native input audit receipt must be an object: {path.name}")
        frequency = str(payload.get("frequency") or "")
        if frequency not in _INPUT_CONTRACTS or frequency in receipts:
            raise ValueError(f"invalid native input audit frequency: {frequency}")
        receipts[frequency] = payload
    return receipts


def _validate_audit_receipt(
    receipt: dict[str, Any] | None,
    *,
    frequency: str,
    scheme_id: str,
    predict_date: str,
    actual_path: Path,
    contract: dict[str, str],
    details: os.stat_result,
    records: list[PredictionRecord],
) -> list[str]:
    if receipt is None:
        return []
    errors: list[str] = []
    expected = {
        "scheme_id": scheme_id,
        "frequency": frequency,
        "path": str(actual_path),
        "source": contract["source"],
        "data_version": contract["data_version"],
        "file_size": details.st_size,
        "modified_ns": details.st_mtime_ns,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            errors.append(
                f"{frequency} input audit {key} mismatch: "
                f"expected {value!r}, got {receipt.get(key)!r}"
            )

    metadata = receipt.get("metadata")
    if not isinstance(metadata, dict):
        return [*errors, f"{frequency} input audit metadata is invalid"]
    if metadata.get("predict_date") != predict_date:
        errors.append(
            f"{frequency} input audit predict_date mismatch: "
            f"expected {predict_date}, got {metadata.get('predict_date')}"
        )
    feature_dates = {
        str(record.feature_date or (record.extra or {}).get("feature_date") or "")
        for record in records
    }
    if len(feature_dates) != 1 or "" in feature_dates:
        errors.append(
            f"{frequency} input audit requires one common feature_date: "
            f"{sorted(feature_dates)}"
        )
        return errors
    feature_date = next(iter(feature_dates))
    if frequency in {"daily", "monthly"}:
        if metadata.get("end_date") != feature_date:
            errors.append(
                f"{frequency} input audit end_date mismatch: "
                f"expected {feature_date}, got {metadata.get('end_date')}"
            )
    elif metadata.get("as_of_date") != feature_date:
        errors.append(
            f"weekly input audit as_of_date mismatch: "
            f"expected {feature_date}, got {metadata.get('as_of_date')}"
        )

    coverage = receipt.get("date_coverage")
    if (
        not isinstance(coverage, dict)
        or coverage.get("start") is None
        or coverage.get("end") is None
    ):
        errors.append(f"{frequency} input audit date_coverage is invalid")
        return errors
    coverage_end = str(coverage["end"])
    if frequency == "daily" and coverage_end > feature_date:
        errors.append(
            f"daily input audit cutoff exceeds feature_date: "
            f"{coverage_end} > {feature_date}"
        )
    if frequency == "monthly":
        feature_month = feature_date[:7].replace("-", "")
        if coverage_end.replace("-", "") > feature_month:
            errors.append(
                f"monthly input audit cutoff exceeds feature month: "
                f"{coverage_end} > {feature_month}"
            )
    if frequency == "weekly" and metadata.get("end_week") is not None:
        try:
            if int(coverage_end) > int(metadata["end_week"]):
                errors.append(
                    f"weekly input audit cutoff exceeds end_week: "
                    f"{coverage_end} > {metadata['end_week']}"
                )
        except (TypeError, ValueError):
            errors.append("weekly input audit cutoff is invalid")
    return errors


def _read_artifact_header(
    path: Path,
    *,
    coverage_column: str,
) -> tuple[list[str], str | None, str | None]:
    """只读 CSV 表头和首行，避免为 Gate 再完整解析一次大文件。"""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = list(reader.fieldnames or [])
            first_row = next(reader, None)
    except (OSError, csv.Error) as exc:
        return [], None, str(exc)
    if not columns:
        return [], None, "CSV header is empty"
    if coverage_column not in columns:
        return columns, None, f"coverage column is missing: {coverage_column}"
    if first_row is None:
        return columns, None, "CSV has no data rows"
    first_key = str(first_row.get(coverage_column) or "").strip()
    if not first_key:
        return columns, None, f"first {coverage_column} is empty"
    return columns, first_key, None
