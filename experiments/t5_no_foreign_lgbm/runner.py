from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import pickle
import platform
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .metrics import MetricCounts, monthly_comparison
from .phase_cache import splice_phase_cache, target_map, write_json_checkpoint
from .policy import FeatureAudit, FOREIGN_DAILY_CODES, feature_source, patch_core_feature_builders
from .report import render_report


TARGET_START = "2026-04-01"
TARGET_END = "2026-06-30"
EXPERIMENT_RELATIVE_ROOT = Path("backtest_artifacts/experiments/t5_no_foreign_lgbm")


@dataclass(frozen=True)
class SchemeSpec:
    composite_id: str
    scheme_id: str
    tenor: str
    family: str
    module_path: str
    run_name: str = ""
    cache_group: str = ""
    cache_relative: str = ""
    baselines: tuple[str, ...] = ()
    model_kind: str = "lgbm"


SCHEME_SPECS = (
    SchemeSpec("daily_5y_2_v28__h5__5Y", "daily_5y_2_v28", "5Y", "v28", "schemes.daily_5y_2_v28.core.v28_common"),
    SchemeSpec("daily_7y_1_v28__h5__7Y", "daily_7y_1_v28", "7Y", "v28", "schemes.daily_7y_1_v28.core.v28_common"),
    SchemeSpec("liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y", "liwei_0616_10y01_cons_say_k3_div_k10", "10Y", "liwei", "schemes.liwei_0616_10y01_cons_say_k3_div_k10.core.v31_common", "run_10y01_for_feature_window", "10y_v61", "10y", ("STD", "ACCWT", "V55_7Y", "DIV")),
    SchemeSpec("liwei_0616_10y01_full_oos_k3_div_k10__h5__10Y", "liwei_0616_10y01_full_oos_k3_div_k10", "10Y", "liwei", "schemes.liwei_0616_10y01_full_oos_k3_div_k10.core.v31_common", "run_10y01_for_feature_window", "10y_v61", "10y", ("STD", "ACCWT", "V55_7Y", "DIV")),
    SchemeSpec("liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y", "liwei_0616_10y02_cons_say_k3_div_k5", "10Y", "liwei", "schemes.liwei_0616_10y02_cons_say_k3_div_k5.core.v31_common", "run_10y02_for_feature_window", "10y_v61", "10y", ("STD", "ACCWT", "V55_7Y", "DIV")),
    SchemeSpec("liwei_0616_5y_auc_static_all_k3_div_k10__h5__5Y", "liwei_0616_5y_auc_static_all_k3_div_k10", "5Y", "liwei", "schemes.liwei_0616_5y_auc_static_all_k3_div_k10.core.v31_common", "run_5y_all_for_feature_window", "5y_allk10_auc_static", "liwei_0616_5y_allk10_auc_static_v1/5y", ("STD", "DIV", "ACCWT", "CROSS_7Y")),
    SchemeSpec("liwei_0616_5y_auc_yearly_all_k3_div_k10__h5__5Y", "liwei_0616_5y_auc_yearly_all_k3_div_k10", "5Y", "liwei", "schemes.liwei_0616_5y_auc_yearly_all_k3_div_k10.core.v31_common", "run_5y_all_for_feature_window", "5y_allk10_auc_yearly", "liwei_0616_5y_allk10_auc_yearly_v1/5y", ("STD", "DIV", "ACCWT", "CROSS_7Y")),
    SchemeSpec("liwei_0616_5y_ic_yearly_all_k3_div_k10__h5__5Y", "liwei_0616_5y_ic_yearly_all_k3_div_k10", "5Y", "liwei", "schemes.liwei_0616_5y_ic_yearly_all_k3_div_k10.core.v31_common", "run_5y_all_for_feature_window", "5y_allk10_ic_yearly", "liwei_0616_5y_allk10_ic_yearly_v1/5y", ("STD", "DIV", "ACCWT", "CROSS_7Y")),
    SchemeSpec("liwei_0616_5y01_full_oos_k3_div_k10__h5__5Y", "liwei_0616_5y01_full_oos_k3_div_k10", "5Y", "liwei", "schemes.liwei_0616_5y01_full_oos_k3_div_k10.core.v31_common", "run_5y01_for_feature_window", "5y_v31", "5y", ("STD", "DIV", "ACCWT")),
    SchemeSpec("liwei_0616_7y01_cons_say_k3_div_k10__h5__7Y", "liwei_0616_7y01_cons_say_k3_div_k10", "7Y", "liwei", "schemes.liwei_0616_7y01_cons_say_k3_div_k10.core.v31_common", "run_7y01_for_feature_window", "7y_v31", "7y", ("STD", "ACCWT", "CROSS_5Y", "DIV")),
    SchemeSpec("liwei_0616_7y03_cons_all_k3_div_k8__h5__7Y", "liwei_0616_7y03_cons_all_k3_div_k8", "7Y", "liwei", "schemes.liwei_0616_7y03_cons_all_k3_div_k8.core.v31_common", "run_7y03_for_feature_window", "7y_v31", "7y", ("STD", "ACCWT", "CROSS_5Y", "DIV")),
    SchemeSpec("liwei_0616_cons_sda_k3_div_k10__h5__5Y", "liwei_0616_cons_sda_k3_div_k10", "5Y", "liwei", "schemes.liwei_0616_cons_sda_k3_div_k10.core.v31_common", "run_5y01_for_feature_window", "5y_v31", "5y", ("STD", "DIV", "ACCWT")),
    SchemeSpec("one_y_t5_liq_excess_a_v1__h5__1Y", "one_y_t5_liq_excess_a_v1", "1Y", "one_year", "schemes.one_y_t5_liq_excess_a_v1.delivery.one_y_t5_liq_excess_a_v1", model_kind="rule_only"),
    SchemeSpec("one_y_t5_liq_excess_a_w252_l7_v1__h5__1Y", "one_y_t5_liq_excess_a_w252_l7_v1", "1Y", "one_year", "schemes.one_y_t5_liq_excess_a_w252_l7_v1.delivery.one_y_t5_liq_excess_a_w252_l7_v1"),
    SchemeSpec("one_y_t5_liq_excess_a_w350_l7_v1__h5__1Y", "one_y_t5_liq_excess_a_w350_l7_v1", "1Y", "one_year", "schemes.one_y_t5_liq_excess_a_w350_l7_v1.delivery.one_y_t5_liq_excess_a_w350_l7_v1"),
    SchemeSpec("one_y_t5_liq_excess_b_w252_l7_v1__h5__1Y", "one_y_t5_liq_excess_b_w252_l7_v1", "1Y", "one_year", "schemes.one_y_t5_liq_excess_b_w252_l7_v1.delivery.one_y_t5_liq_excess_b_w252_l7_v1"),
    SchemeSpec("t5_daily__h5__10Y", "t5_daily", "10Y", "t5_daily", "schemes.t5_daily.core.predict_10y", model_kind="zero_foreign"),
    SchemeSpec("t5_daily__h5__3Y", "t5_daily", "3Y", "t5_daily", "schemes.t5_daily.core.predict_3y", model_kind="zero_foreign"),
    SchemeSpec("t5_daily__h5__5Y", "t5_daily", "5Y", "t5_daily", "schemes.t5_daily.core.predict_5y", model_kind="zero_foreign"),
    SchemeSpec("t5_daily__h5__7Y", "t5_daily", "7Y", "t5_daily", "schemes.t5_daily.core.predict_7y", model_kind="zero_foreign"),
)
ACTIVE_SCHEME_IDS = tuple(spec.composite_id for spec in SCHEME_SPECS)


def ensure_output_root(project_root: str | Path, output_root: str | Path) -> Path:
    project = Path(project_root).resolve()
    allowed = (project / EXPERIMENT_RELATIVE_ROOT).resolve()
    output = Path(output_root).resolve()
    try:
        output.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(f"experiment output root must be below {allowed}: {output}") from exc
    output.mkdir(parents=True, exist_ok=True)
    return output


def hash_files(paths: Iterable[str | Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_path in paths:
        path = Path(raw_path).resolve()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result[str(path)] = digest.hexdigest()
    return result


def verify_hashes(expected: Mapping[str, str]) -> bool:
    try:
        return hash_files(expected) == dict(expected)
    except OSError:
        return False


def checkpoint_key(branch: str, name: str) -> str:
    if branch not in {"baseline", "ablation", "combined"}:
        raise ValueError(f"invalid checkpoint branch: {branch}")
    safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return f"{branch}__{safe_name}"


def load_or_run_checkpoint(
    path: str | Path,
    builder: Callable[[], Any],
    *,
    resume: bool,
) -> Any:
    target = Path(path)
    if resume and target.is_file():
        try:
            with target.open("rb") as handle:
                value = pickle.load(handle)
            if value is not None:
                return value
        except (OSError, pickle.PickleError, EOFError, AttributeError, ValueError):
            pass
    value = builder()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return value


def select_recompute_feature_dates(
    feature_to_target: Mapping[str, str],
    target_start: str = TARGET_START,
    target_end: str = TARGET_END,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            feature_date
            for feature_date, target_date in feature_to_target.items()
            if target_start <= str(target_date) <= target_end
        )
    )


def v28_month_windows(feature_dates: Sequence[str]) -> tuple[tuple[str, str], ...]:
    grouped: dict[str, list[str]] = {}
    for feature_date in sorted(set(map(str, feature_dates))):
        period = pd.Timestamp(feature_date).strftime("%Y-%m")
        grouped.setdefault(period, []).append(feature_date)
    return tuple((f"{period}-01", max(values)) for period, values in sorted(grouped.items()))


def domestic_one_year_factors(factors: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(value) for value in factors if str(value) not in FOREIGN_DAILY_CODES)


def merge_phase_caches(
    baseline: Mapping[str, Any],
    extension: Mapping[str, Any],
) -> dict[str, Any]:
    left_dates, left_results = _cache_parts(baseline, "baseline")
    right_dates, right_results = _cache_parts(extension, "extension")
    left_by_config = {_config_key(result["config"]): result for result in left_results}
    right_by_config = {_config_key(result["config"]): result for result in right_results}
    if set(left_by_config) != set(right_by_config):
        raise RuntimeError("baseline and extension cache config sets differ")
    all_dates = sorted(set(left_dates) | set(right_dates))
    left_pos = {value: index for index, value in enumerate(left_dates)}
    right_pos = {value: index for index, value in enumerate(right_dates)}
    merged_results: list[dict[str, Any]] = []
    for key, left in left_by_config.items():
        right = right_by_config[key]
        predictions: list[int] = []
        probabilities: list[float] = []
        for date in all_dates:
            if date in left_pos:
                li = left_pos[date]
                predictions.append(int(np.asarray(left["preds"])[li]))
                probabilities.append(float(np.asarray(left["probs"])[li]))
            else:
                ri = right_pos[date]
                predictions.append(int(np.asarray(right["preds"])[ri]))
                probabilities.append(float(np.asarray(right["probs"])[ri]))
        merged_results.append(
            {
                "config": copy.deepcopy(left["config"]),
                "preds": np.asarray(predictions, dtype=np.int32),
                "probs": np.asarray(probabilities, dtype=np.float64),
            }
        )
    return {"test_dates": all_dates, "results": merged_results}


def _cache_parts(cache: Mapping[str, Any], name: str) -> tuple[list[str], list[Mapping[str, Any]]]:
    dates = [pd.Timestamp(value).strftime("%Y-%m-%d") for value in cache.get("test_dates", [])]
    results = list(cache.get("results", []))
    if not dates or not results or len(dates) != len(set(dates)):
        raise RuntimeError(f"{name} cache is empty or has duplicate dates")
    for result in results:
        if len(np.asarray(result.get("preds"))) != len(dates) or len(np.asarray(result.get("probs"))) != len(dates):
            raise RuntimeError(f"{name} cache result length mismatch")
    return dates, results


def _config_key(config: Mapping[str, Any]) -> str:
    return json.dumps(dict(config), sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class ExperimentInputs:
    daily: pd.DataFrame
    weekly: pd.DataFrame
    monthly: pd.DataFrame
    calendar: pd.DataFrame
    date_to_week: dict[str, int | str]
    feature_to_target: dict[str, str]


class ExperimentRunner:
    def __init__(
        self,
        *,
        project_root: str | Path,
        source_data_root: str | Path,
        source_cache_root: str | Path,
        calendar_path: str | Path,
        output_root: str | Path,
        report_path: str | Path,
        workers: int = 10,
        resume: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.source_data_root = Path(source_data_root).resolve()
        self.source_cache_root = Path(source_cache_root).resolve()
        self.calendar_path = Path(calendar_path).resolve()
        self.output_root = ensure_output_root(self.project_root, output_root)
        self.report_path = Path(report_path).resolve()
        allowed_reports = (self.project_root / "reports/experiments").resolve()
        try:
            self.report_path.relative_to(allowed_reports)
        except ValueError as exc:
            raise ValueError(f"report path must be below {allowed_reports}") from exc
        self.workers = max(1, int(workers))
        self.resume = bool(resume)
        self.inputs: ExperimentInputs | None = None
        self._source_files = self._resolve_source_files()
        self._source_hashes: dict[str, str] = {}

    def _resolve_source_files(self) -> tuple[Path, ...]:
        paths = [
            self.source_data_root / "daily_output.csv",
            self.source_data_root / "weekly_output.csv",
            self.source_data_root / "monthly_output.csv",
            self.calendar_path,
        ]
        seen_cache_files: set[Path] = set()
        for spec in SCHEME_SPECS:
            if spec.family != "liwei":
                continue
            for baseline in spec.baselines:
                seen_cache_files.add(self.source_cache_root / spec.cache_relative / f"{baseline}.pkl")
        paths.extend(sorted(seen_cache_files))
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"missing read-only experiment inputs: {missing}")
        return tuple(paths)

    def load_inputs(self) -> ExperimentInputs:
        daily = pd.read_csv(self.source_data_root / "daily_output.csv", encoding="utf-8-sig")
        weekly = pd.read_csv(self.source_data_root / "weekly_output.csv", encoding="utf-8-sig")
        monthly = pd.read_csv(self.source_data_root / "monthly_output.csv", encoding="utf-8-sig")
        calendar = pd.read_csv(self.calendar_path, encoding="utf-8-sig")
        mapping = target_map(calendar, horizon=5)
        date_to_week = {
            pd.Timestamp(date).strftime("%Y-%m-%d"): week
            for date, week in zip(calendar["rdate"], calendar["week_id"], strict=True)
        }
        self.inputs = ExperimentInputs(daily, weekly, monthly, calendar, date_to_week, mapping)
        return self.inputs

    def run(self) -> dict[str, Any]:
        self._source_hashes = hash_files(self._source_files)
        inputs = self.load_inputs()
        available_dates = set(pd.to_datetime(inputs.daily["date"], errors="raise").dt.strftime("%Y-%m-%d"))
        feature_dates = tuple(date for date in select_recompute_feature_dates(inputs.feature_to_target) if date in available_dates)
        if not feature_dates:
            raise RuntimeError("no April-June T+5 feature dates in daily snapshot")
        results: dict[str, dict[str, Any]] = {}

        for spec in (value for value in SCHEME_SPECS if value.family == "v28"):
            results[spec.composite_id] = self._run_scheme_checkpoint(spec, lambda spec=spec: self._run_v28(spec, feature_dates))

        for group in _ordered_liwei_groups():
            group_specs = [spec for spec in SCHEME_SPECS if spec.cache_group == group]
            try:
                group_results = self._run_liwei_group(group_specs, feature_dates)
            except Exception as exc:  # preserve other independent family results
                reason = f"{type(exc).__name__}: {exc}"
                group_results = {spec.composite_id: _failed_scheme(spec, reason) for spec in group_specs}
            results.update(group_results)
            self._write_partial_state(results)

        for family in ("one_year", "t5_daily"):
            for spec in (value for value in SCHEME_SPECS if value.family == family):
                adapter = self._run_one_year if family == "one_year" else self._run_t5_daily
                results[spec.composite_id] = self._run_scheme_checkpoint(spec, lambda spec=spec, adapter=adapter: adapter(spec, feature_dates))
                self._write_partial_state(results)

        if not verify_hashes(self._source_hashes):
            raise RuntimeError("one or more read-only gray-lab source files changed during the experiment")
        bundle = self._bundle(results, source_unchanged=True)
        validate_result_bundle(bundle)
        write_json_checkpoint(self.output_root, "run_state.json", bundle)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_text(self.report_path, render_report(bundle))
        return bundle

    def validate_only(self) -> dict[str, Any]:
        state_path = self.output_root / "run_state.json"
        bundle = json.loads(state_path.read_text(encoding="utf-8"))
        validate_result_bundle(bundle)
        if not bool(bundle.get("metadata", {}).get("source_unchanged")):
            raise RuntimeError("run_state does not prove unchanged source inputs")
        return bundle

    def _run_scheme_checkpoint(self, spec: SchemeSpec, builder: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        checkpoint = self.output_root / "schemes" / f"{spec.composite_id}.pkl"
        try:
            return load_or_run_checkpoint(checkpoint, builder, resume=self.resume)
        except Exception as exc:
            return _failed_scheme(spec, f"{type(exc).__name__}: {exc}")

    def _run_v28(self, spec: SchemeSpec, feature_dates: Sequence[str]) -> dict[str, Any]:
        assert self.inputs is not None
        module = importlib.import_module(spec.module_path)
        audit = FeatureAudit()

        def branch(filtered: bool) -> pd.DataFrame:
            frames: list[pd.DataFrame] = []
            context = patch_core_feature_builders(module, audit) if filtered else _null_context()
            with context:
                for window_start, window_end in v28_month_windows(feature_dates):
                    detail = module.run_prediction(
                        module.model_config(
                            daily_df=self.inputs.daily,
                            weekly_df=self.inputs.weekly,
                            monthly_df=self.inputs.monthly,
                            date_to_week=self.inputs.date_to_week,
                            test_start=window_start,
                            test_end=window_end,
                            require_labels=True,
                            emit_report=False,
                            return_details=True,
                            n_workers=self.workers,
                        )
                    )
                    frames.append(detail)
            return self._detail_rows(spec, pd.concat(frames, ignore_index=True), feature_dates)

        baseline = branch(False)
        ablation = branch(True)
        return _successful_scheme(spec, baseline, ablation, _audit_payload(audit))

    def _run_liwei_group(self, specs: Sequence[SchemeSpec], feature_dates: Sequence[str]) -> dict[str, dict[str, Any]]:
        assert self.inputs is not None
        representative = specs[0]
        phase_module = importlib.import_module(representative.module_path)
        requested_dates = self._liwei_requested_dates(representative.tenor, feature_dates)
        test_ranges = _liwei_test_ranges(feature_dates)
        audit = FeatureAudit()
        baseline_caches: dict[str, Any] = {}
        ablation_caches: dict[str, Any] = {}

        for baseline_name in representative.baselines:
            source_path = self.source_cache_root / representative.cache_relative / f"{baseline_name}.pkl"
            with source_path.open("rb") as handle:
                envelope = pickle.load(handle)
            source_cache = envelope.get("phase_a_cache", envelope)
            cached_dates = set(map(str, source_cache["test_dates"]))
            missing_dates = tuple(date for date in requested_dates if date not in cached_dates)
            complete_path = self.output_root / "phase" / representative.cache_group / "baseline" / f"{baseline_name}.pkl"

            def build_complete() -> dict[str, Any]:
                if not missing_dates:
                    return copy.deepcopy(source_cache)
                extension = self._train_phase(phase_module, baseline_name, missing_dates, filtered=False, audit=audit)
                return merge_phase_caches(source_cache, extension)

            complete = load_or_run_checkpoint(complete_path, build_complete, resume=self.resume)
            filtered_path = self.output_root / "phase" / representative.cache_group / "ablation" / f"{baseline_name}.pkl"
            filtered_payload = load_or_run_checkpoint(
                filtered_path,
                lambda baseline_name=baseline_name: self._train_filtered_phase_payload(
                    phase_module,
                    baseline_name,
                    tuple(feature_dates),
                    audit,
                ),
                resume=self.resume,
            )
            if not isinstance(filtered_payload, Mapping) or "cache" not in filtered_payload:
                raise RuntimeError(f"invalid filtered Phase A checkpoint for {representative.cache_group}/{baseline_name}")
            _restore_audit(audit, filtered_payload.get("audit", {}))
            filtered_cache = filtered_payload["cache"]
            baseline_caches[baseline_name] = complete
            ablation_caches[baseline_name] = splice_phase_cache(
                complete,
                filtered_cache,
                feature_to_target=self.inputs.feature_to_target,
                target_start=TARGET_START,
                target_end=TARGET_END,
            )

        output: dict[str, dict[str, Any]] = {}
        for spec in specs:
            def build_scheme(spec: SchemeSpec = spec) -> dict[str, Any]:
                module = importlib.import_module(spec.module_path)
                runner = getattr(module, spec.run_name)
                kwargs = dict(
                    daily_df=self.inputs.daily,
                    weekly_df=self.inputs.weekly,
                    monthly_df=self.inputs.monthly,
                    date_to_week=self.inputs.date_to_week,
                    test_ranges=test_ranges,
                    current_start=min(feature_dates),
                    current_end=max(feature_dates),
                    require_labels=True,
                    n_workers=self.workers,
                )
                baseline_detail = runner(**kwargs, phase_a_caches=baseline_caches)
                ablation_detail = runner(**kwargs, phase_a_caches=ablation_caches)
                baseline_rows = self._detail_rows(spec, baseline_detail, feature_dates)
                ablation_rows = self._detail_rows(spec, ablation_detail, feature_dates)
                return _successful_scheme(spec, baseline_rows, ablation_rows, _audit_payload(audit))

            output[spec.composite_id] = self._run_scheme_checkpoint(spec, build_scheme)
        return output

    def _train_filtered_phase_payload(
        self,
        module: Any,
        baseline: str,
        dates: Sequence[str],
        audit: FeatureAudit,
    ) -> dict[str, Any]:
        cache = self._train_phase(module, baseline, dates, filtered=True, audit=audit)
        return {"cache": cache, "audit": _audit_payload(audit)}

    def _train_phase(
        self,
        module: Any,
        baseline: str,
        dates: Sequence[str],
        *,
        filtered: bool,
        audit: FeatureAudit,
    ) -> dict[str, Any]:
        assert self.inputs is not None
        if not dates:
            raise RuntimeError("cannot train an empty Phase A date set")
        ranges = tuple((date, date) for date in dates)
        context = patch_core_feature_builders(module, audit) if filtered else _null_context()
        with context:
            output = module.run_prediction(
                module.model_config(
                    baseline,
                    daily_df=self.inputs.daily,
                    weekly_df=self.inputs.weekly,
                    monthly_df=self.inputs.monthly,
                    date_to_week=self.inputs.date_to_week,
                    test_start=min(dates),
                    test_end=max(dates),
                    test_ranges=ranges,
                    require_labels=True,
                    emit_report=False,
                    phase_a_only=True,
                    return_ctx=True,
                    n_workers=self.workers,
                )
            )
        phase_context = output[1] if isinstance(output, tuple) else output
        if not isinstance(phase_context, Mapping) or "phase_a_cache" not in phase_context:
            raise RuntimeError(f"{baseline} did not return Phase A cache")
        return dict(phase_context["phase_a_cache"])

    def _liwei_requested_dates(self, tenor: str, feature_dates: Sequence[str]) -> tuple[str, ...]:
        assert self.inputs is not None
        close_column = {"5Y": "TB5YWI0C", "7Y": "TB7YWI0C", "10Y": "TB0YWI0C"}[tenor]
        frame = self.inputs.daily.copy()
        dates = pd.to_datetime(frame["date"], errors="raise").dt.strftime("%Y-%m-%d")
        close = pd.to_numeric(frame[close_column], errors="coerce").to_numpy(dtype=float)
        valid = np.isfinite(close) & np.r_[np.isfinite(close[5:]), np.zeros(5, dtype=bool)]
        ranges = _liwei_test_ranges(feature_dates)
        in_range = np.zeros(len(frame), dtype=bool)
        for start, end in ranges:
            in_range |= dates.between(start, end).to_numpy()
        return tuple(dates[valid & in_range].tolist())

    def _run_one_year(self, spec: SchemeSpec, feature_dates: Sequence[str]) -> dict[str, Any]:
        assert self.inputs is not None
        module = importlib.import_module(spec.module_path)
        daily = self.inputs.daily.copy()
        daily["_date_key"] = pd.to_datetime(daily["date"], errors="raise").dt.strftime("%Y-%m-%d")
        positions = {date: index for index, date in enumerate(daily["_date_key"])}
        original_factors = tuple(module.MARKET_FACTORS)
        filtered_factors = domestic_one_year_factors(original_factors)

        def branch(filtered: bool) -> pd.DataFrame:
            rows: list[dict[str, Any]] = []
            with _temporary_attribute(module, "MARKET_FACTORS", filtered_factors if filtered else original_factors):
                for feature_date in feature_dates:
                    index = positions.get(feature_date)
                    if index is None or index + module.HORIZON >= len(daily):
                        continue
                    current = float(pd.to_numeric(daily[module.YIELD_COLUMNS[0]], errors="coerce").iat[index])
                    future = float(pd.to_numeric(daily[module.YIELD_COLUMNS[0]], errors="coerce").iat[index + module.HORIZON])
                    if not np.isfinite(current) or not np.isfinite(future):
                        continue
                    direction = int(module.predict_direction(daily.iloc[: index + 1].copy()))
                    label = 1 if future / current - 1.0 > 0 else -1
                    rows.append(_row(spec, feature_date, self.inputs.feature_to_target[feature_date], label, direction))
            return pd.DataFrame(rows)

        baseline = branch(False)
        ablation = baseline.copy(deep=True) if spec.model_kind == "rule_only" else branch(True)
        candidate_columns = [f"{source}_ret{lag}" for source in module.YIELD_COLUMNS + original_factors for lag in (1, 5, 10, 20)]
        removed_columns = [column for column in candidate_columns if feature_source(column) in FOREIGN_DAILY_CODES]
        audit = {
            "candidate_count": len(candidate_columns),
            "retained_count": len(candidate_columns) - len(removed_columns),
            "removed_columns": removed_columns,
            "removed_sources": sorted({feature_source(column) for column in removed_columns}),
            "ust_sources_used": sorted({source for source in (feature_source(column) for column in removed_columns) if source in {"DRS00001", "DRS00002"}}),
        }
        status = "not_applicable" if spec.model_kind == "rule_only" else "success"
        return _successful_scheme(spec, baseline, ablation, audit, status=status, reason="规则策略不调用 LGBM" if status == "not_applicable" else "")

    def _run_t5_daily(self, spec: SchemeSpec, feature_dates: Sequence[str]) -> dict[str, Any]:
        assert self.inputs is not None
        module = importlib.import_module(spec.module_path)
        latest = importlib.import_module("schemes.t5_daily.latest_prediction")
        daily = self.inputs.daily.copy()
        daily["date"] = pd.to_datetime(daily["date"], errors="raise")
        positions = {value.strftime("%Y-%m-%d"): index for index, value in enumerate(daily["date"])}
        calendar_dates = [pd.Timestamp(value).strftime("%Y-%m-%d") for value in self.inputs.calendar["rdate"]]
        calendar_pos = {value: index for index, value in enumerate(calendar_dates)}
        rows: list[dict[str, Any]] = []
        for feature_date in feature_dates:
            index = positions.get(feature_date)
            cal_index = calendar_pos.get(feature_date)
            if index is None or cal_index is None or index + module.HORIZON >= len(daily) or cal_index + 1 >= len(calendar_dates):
                continue
            current = float(pd.to_numeric(daily[module.CLOSE_COL], errors="coerce").iat[index])
            future = float(pd.to_numeric(daily[module.CLOSE_COL], errors="coerce").iat[index + module.HORIZON])
            if not np.isfinite(current) or not np.isfinite(future):
                continue
            prediction = latest.predict_latest_for_module(module, daily, calendar_dates[cal_index + 1], n_jobs=max(1, min(self.workers, 4)))
            label = 1 if future / current - 1.0 > 0 else -1
            rows.append(_row(spec, feature_date, self.inputs.feature_to_target[feature_date], label, prediction.vote_pred))
        baseline = pd.DataFrame(rows)
        if hasattr(module, "build_features_multi"):
            features = module.build_features_multi(daily, module.AUX_TENORS)
        else:
            common = importlib.import_module("schemes.t5_daily.core.common_utils")
            features = common.build_features(daily, module.CLOSE_COL, module.AUX1_COL, module.AUX1_NAME, module.AUX2_COL, module.AUX2_NAME, module.SELF_NAME)
        audit = {
            "candidate_count": len(features.columns),
            "retained_count": len(features.columns),
            "removed_columns": [],
            "removed_sources": [],
            "ust_sources_used": [],
        }
        return _successful_scheme(spec, baseline, baseline.copy(deep=True), audit)

    def _detail_rows(self, spec: SchemeSpec, detail: pd.DataFrame, feature_dates: Sequence[str]) -> pd.DataFrame:
        required = {"anchor_date", "true_label", "prediction"}
        if not required.issubset(detail.columns):
            raise RuntimeError(f"{spec.composite_id} detail missing {sorted(required - set(detail.columns))}")
        allowed = set(map(str, feature_dates))
        rows: list[dict[str, Any]] = []
        for record in detail.to_dict("records"):
            feature_date = pd.Timestamp(record["anchor_date"]).strftime("%Y-%m-%d")
            if feature_date not in allowed or pd.isna(record["true_label"]):
                continue
            target_date = self.inputs.feature_to_target.get(feature_date) if self.inputs is not None else None
            if target_date is None or not TARGET_START <= target_date <= TARGET_END:
                continue
            rows.append(_row(spec, feature_date, target_date, int(record["true_label"]), int(record["prediction"])))
        frame = pd.DataFrame(rows)
        if frame.empty:
            raise RuntimeError(f"{spec.composite_id} produced no target-month rows")
        return frame

    def _bundle(self, results: Mapping[str, dict[str, Any]], *, source_unchanged: bool) -> dict[str, Any]:
        metadata = {
            "code_commit": _git_commit(self.project_root),
            "python_version": platform.python_version(),
            "lightgbm_version": _package_version("lightgbm"),
            "pandas_version": pd.__version__,
            "numpy_version": np.__version__,
            "source_hashes": self._source_hashes,
            "source_unchanged": bool(source_unchanged),
            "target_start": TARGET_START,
            "target_end": TARGET_END,
        }
        return {
            "metadata": metadata,
            "manifest_order": list(ACTIVE_SCHEME_IDS),
            "schemes": [results.get(spec.composite_id, _failed_scheme(spec, "not executed")) for spec in SCHEME_SPECS],
        }

    def _write_partial_state(self, results: Mapping[str, dict[str, Any]]) -> None:
        write_json_checkpoint(self.output_root, "run_state.partial.json", self._bundle(results, source_unchanged=verify_hashes(self._source_hashes)))


def _ordered_liwei_groups() -> tuple[str, ...]:
    seen: list[str] = []
    for spec in SCHEME_SPECS:
        if spec.family == "liwei" and spec.cache_group not in seen:
            seen.append(spec.cache_group)
    return tuple(seen)


def _liwei_test_ranges(feature_dates: Sequence[str]) -> tuple[tuple[str, str], ...]:
    current_start = pd.Timestamp(min(feature_dates))
    current_end = pd.Timestamp(max(feature_dates))
    prior_start = current_start - pd.DateOffset(years=1)
    prior_end = (current_end - pd.DateOffset(years=1)) + pd.offsets.MonthEnd(0)
    return ((str(prior_start.date()), str(prior_end.date())), (str(current_start.date()), str(current_end.date())))


def _row(spec: SchemeSpec, feature_date: str, target_date: str, label: int, direction: int) -> dict[str, Any]:
    return {
        "scheme_id": spec.composite_id,
        "target_tenor": spec.tenor,
        "feature_date": str(feature_date),
        "target_date": str(target_date),
        "label": int(label),
        "direction": int(direction),
    }


def _successful_scheme(
    spec: SchemeSpec,
    baseline: pd.DataFrame,
    ablation: pd.DataFrame,
    audit: Mapping[str, Any],
    *,
    status: str = "success",
    reason: str = "",
) -> dict[str, Any]:
    comparisons = [_comparison_payload(value) for value in monthly_comparison(baseline, ablation)]
    left = baseline.sort_values(["feature_date", "target_date"]).reset_index(drop=True)
    right = ablation.sort_values(["feature_date", "target_date"]).reset_index(drop=True)
    if not left[["feature_date", "target_date", "label"]].equals(right[["feature_date", "target_date", "label"]]):
        raise RuntimeError(f"{spec.composite_id} branch rows do not align")
    changed = left.loc[left["direction"].to_numpy() != right["direction"].to_numpy(), "target_date"].astype(str).tolist()
    return {
        "scheme_id": spec.composite_id,
        "status": status,
        "reason": reason,
        "comparisons": comparisons,
        "factor_audit": dict(audit),
        "changed_target_dates": changed,
    }


def _failed_scheme(spec: SchemeSpec, reason: str) -> dict[str, Any]:
    return {
        "scheme_id": spec.composite_id,
        "status": "failed",
        "reason": str(reason),
        "comparisons": [],
        "factor_audit": {},
        "changed_target_dates": [],
    }


def _comparison_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    for branch in ("baseline", "ablation"):
        counts = result[branch]
        result[branch] = asdict(counts) if isinstance(counts, MetricCounts) else dict(counts)
    return result


def _audit_payload(audit: FeatureAudit) -> dict[str, Any]:
    removed_sources = sorted({source for column in audit.removed_columns if (source := feature_source(column))})
    return {
        "candidate_count": len(audit.candidate_columns),
        "retained_count": len(audit.retained_columns),
        "candidate_columns": list(audit.candidate_columns),
        "retained_columns": sorted(audit.retained_columns),
        "removed_columns": list(audit.removed_columns),
        "removed_sources": removed_sources,
        "ust_sources_used": sorted(set(removed_sources) & {"DRS00001", "DRS00002"}),
    }


def _restore_audit(audit: FeatureAudit, payload: Mapping[str, Any]) -> None:
    candidates = [str(value) for value in payload.get("candidate_columns", [])]
    removed = [str(value) for value in payload.get("removed_columns", [])]
    retained = [str(value) for value in payload.get("retained_columns", [])]
    if candidates or removed or retained:
        audit.record(candidates=candidates, removed=removed, retained=retained)


def validate_result_bundle(bundle: Mapping[str, Any]) -> None:
    order = tuple(map(str, bundle.get("manifest_order", [])))
    if order != ACTIVE_SCHEME_IDS:
        raise RuntimeError("run_state manifest does not match frozen 20-scheme scope")
    schemes = list(bundle.get("schemes", []))
    if len(schemes) != 20 or tuple(str(value.get("scheme_id")) for value in schemes) != ACTIVE_SCHEME_IDS:
        raise RuntimeError("run_state scheme results do not match manifest order")
    for scheme in schemes:
        status = str(scheme.get("status"))
        if status not in {"success", "not_applicable", "failed"}:
            raise RuntimeError(f"invalid status for {scheme.get('scheme_id')}: {status}")
        if status != "failed":
            periods = [str(value.get("period")) for value in scheme.get("comparisons", [])]
            if periods != ["2026-04", "2026-05", "2026-06", "2026-04..06"]:
                raise RuntimeError(f"incomplete comparisons for {scheme.get('scheme_id')}")


@contextmanager
def _temporary_attribute(module: Any, name: str, value: Any):
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)


@contextmanager
def _null_context():
    yield


def _atomic_text(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _git_commit(project_root: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project_root, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _package_version(name: str) -> str:
    try:
        module = importlib.import_module(name)
        return str(getattr(module, "__version__", "unknown"))
    except ImportError:
        return "unavailable"
