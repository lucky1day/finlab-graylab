from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import pickle
import platform
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import fcntl

import numpy as np
import pandas as pd

from shared.artifact_paths import BACKTEST_ARTIFACT_ROOT, safe_path_part


CACHE_SCHEMA_VERSION = 1
PHASE_A_CACHE_ABI_VERSION = "liwei_0616.phase_a.v1"
DEFAULT_CACHE_ROOT = BACKTEST_ARTIFACT_ROOT / "runtime_cache" / "liwei_0616"


@dataclass(frozen=True)
class PhaseACacheSpec:
    """liwei_0616 一组可共享 baseline cache 的固定身份。"""

    cache_family: str
    tenor: str
    baselines: tuple[str, ...]
    baseline_configs: Mapping[str, Mapping[str, Any]]
    source_ic_screen_start: str
    horizon: int
    purge_gap: int


def prepare_phase_a_caches(
    *,
    spec: PhaseACacheSpec,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    test_ranges: tuple[tuple[str, str], ...],
    train_missing: Callable[[str, tuple[tuple[str, str], ...]], Mapping[str, Any]],
    cache_root: str | Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """读取或扩展一组 baseline 的 Phase A cache。"""
    root = _cache_root(cache_root)
    caches: dict[str, dict[str, Any]] = {}
    baseline_audits: dict[str, dict[str, Any]] = {}
    for baseline in spec.baselines:
        config = spec.baseline_configs.get(baseline)
        if config is None:
            raise KeyError(f"missing baseline config: {baseline}")
        requested_dates = _requested_dates(daily_df, config, test_ranges)
        if not requested_dates:
            raise ValueError(f"baseline {baseline} has no requested test dates")
        path = _cache_path(root, spec, baseline)
        fingerprint = _baseline_fingerprint(spec, baseline)
        with _exclusive_lock(path.with_suffix(".lock")):
            envelope = _load_envelope(path)
            cached = None
            if envelope is not None:
                identity_ok = envelope.get("baseline_fingerprint") == fingerprint
                input_ok = identity_ok and _input_prefix_matches(
                    envelope.get("input_prefix"),
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                )
                if identity_ok and input_ok:
                    try:
                        cached = _validate_phase_a_cache(envelope.get("phase_a_cache"))
                    except (TypeError, ValueError):
                        _quarantine_invalid_cache(path)
                        envelope = None
                else:
                    _quarantine_invalid_cache(path)
                    envelope = None
            cached_dates = set(cached["test_dates"]) if cached is not None else set()
            missing_dates = [day for day in requested_dates if day not in cached_dates]

            if cached is None:
                status = "cold_build"
            elif missing_dates:
                status = "extended"
            else:
                status = "hit"

            merged = cached
            if missing_dates:
                missing_ranges = tuple((day, day) for day in missing_dates)
                new_cache = _validate_phase_a_cache(train_missing(baseline, missing_ranges))
                merged = _merge_phase_a_caches(cached, new_cache)
                envelope = _build_envelope(
                    spec,
                    baseline,
                    fingerprint,
                    merged,
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    created_at=(envelope or {}).get("created_at"),
                )
                _atomic_pickle_dump(path, envelope)
            if merged is None:
                raise RuntimeError(f"baseline {baseline} cache is empty after preparation")
        caches[baseline] = merged
        baseline_audits[baseline] = {
            "status": status,
            "watermark": max(merged["test_dates"]),
            "missing_dates": missing_dates,
            "fingerprint": fingerprint,
        }

    statuses = {str(item["status"]) for item in baseline_audits.values()}
    overall_status = "cold_build" if "cold_build" in statuses else "extended" if "extended" in statuses else "hit"
    all_missing = sorted(
        {
            str(day)
            for item in baseline_audits.values()
            for day in item["missing_dates"]
        }
    )
    return caches, {
        "status": overall_status,
        "watermark": max(str(item["watermark"]) for item in baseline_audits.values()),
        "missing_dates": all_missing,
        "version": PHASE_A_CACHE_ABI_VERSION,
        "fingerprint": _audit_fingerprint(baseline_audits),
        "baselines": baseline_audits,
    }


def _cache_root(cache_root: str | Path | None) -> Path:
    configured = cache_root or os.getenv("LIWEI_0616_PHASE_A_CACHE_ROOT") or DEFAULT_CACHE_ROOT
    return Path(configured)


def _cache_path(root: Path, spec: PhaseACacheSpec, baseline: str) -> Path:
    return root / safe_path_part(spec.tenor.lower()) / f"{safe_path_part(baseline)}.pkl"


def _requested_dates(
    daily_df: pd.DataFrame,
    baseline_config: Mapping[str, Any],
    test_ranges: tuple[tuple[str, str], ...],
) -> list[str]:
    close_column = str(baseline_config.get("close") or "")
    if "date" not in daily_df.columns or close_column not in daily_df.columns:
        raise ValueError(f"daily input missing date or close column {close_column}")
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    valid = dates.notna() & daily_df[close_column].notna()
    in_range = pd.Series(False, index=daily_df.index)
    for start, end in test_ranges:
        in_range |= dates.between(str(start), str(end), inclusive="both")
    return sorted(set(dates[valid & in_range].astype(str).tolist()))


def _baseline_fingerprint(spec: PhaseACacheSpec, baseline: str) -> str:
    payload = {
        "abi": PHASE_A_CACHE_ABI_VERSION,
        "cache_family": spec.cache_family,
        "tenor": spec.tenor,
        "baseline": baseline,
        "config": spec.baseline_configs[baseline],
        "source_ic_screen_start": spec.source_ic_screen_start,
        "horizon": spec.horizon,
        "purge_gap": spec.purge_gap,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "lightgbm": _package_version("lightgbm"),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _audit_fingerprint(audits: Mapping[str, Mapping[str, Any]]) -> str:
    payload = {name: item["fingerprint"] for name, item in sorted(audits.items())}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _load_envelope(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
    except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError, TypeError):
        _quarantine_invalid_cache(path)
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        _quarantine_invalid_cache(path)
        return None
    return payload


def _validate_phase_a_cache(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("phase_a_cache must be a mapping")
    dates = [str(day) for day in value.get("test_dates", [])]
    if not dates or len(dates) != len(set(dates)):
        raise ValueError("phase_a_cache test_dates must be non-empty and unique")
    results = value.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("phase_a_cache results must be a non-empty list")
    normalized_results: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, Mapping) or not isinstance(item.get("config"), Mapping):
            raise ValueError("phase_a_cache result missing config")
        preds = np.asarray(item.get("preds"), dtype=np.int32)
        probs = np.asarray(item.get("probs"), dtype=np.float64)
        if preds.shape != (len(dates),) or probs.shape != (len(dates),):
            raise ValueError("phase_a_cache result arrays do not match test_dates")
        normalized_results.append(
            {
                "config": dict(item["config"]),
                "preds": preds.copy(),
                "probs": probs.copy(),
            }
        )
    return {"test_dates": dates, "results": normalized_results}


def _merge_phase_a_caches(
    cached: Mapping[str, Any] | None,
    new: Mapping[str, Any],
) -> dict[str, Any]:
    if cached is None:
        return _validate_phase_a_cache(new)
    old = _validate_phase_a_cache(cached)
    fresh = _validate_phase_a_cache(new)
    old_by_config = {_canonical_json(item["config"]): item for item in old["results"]}
    new_by_config = {_canonical_json(item["config"]): item for item in fresh["results"]}
    if set(old_by_config) != set(new_by_config):
        raise ValueError("phase_a_cache config sets do not match")
    merged_dates = sorted(set(old["test_dates"]) | set(fresh["test_dates"]))
    merged_results: list[dict[str, Any]] = []
    for old_item in old["results"]:
        key = _canonical_json(old_item["config"])
        new_item = new_by_config[key]
        pred_by_date = dict(zip(old["test_dates"], old_item["preds"], strict=True))
        prob_by_date = dict(zip(old["test_dates"], old_item["probs"], strict=True))
        for index, day in enumerate(fresh["test_dates"]):
            if day in pred_by_date:
                if int(pred_by_date[day]) != int(new_item["preds"][index]) or not np.isclose(
                    float(prob_by_date[day]), float(new_item["probs"][index]), rtol=0.0, atol=0.0
                ):
                    raise ValueError(f"conflicting Phase A cache row for {day}")
                continue
            pred_by_date[day] = new_item["preds"][index]
            prob_by_date[day] = new_item["probs"][index]
        merged_results.append(
            {
                "config": dict(old_item["config"]),
                "preds": np.asarray([pred_by_date[day] for day in merged_dates], dtype=np.int32),
                "probs": np.asarray([prob_by_date[day] for day in merged_dates], dtype=np.float64),
            }
        )
    return {"test_dates": merged_dates, "results": merged_results}


def _build_envelope(
    spec: PhaseACacheSpec,
    baseline: str,
    fingerprint: str,
    phase_a_cache: Mapping[str, Any],
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    created_at: str | None,
) -> dict[str, Any]:
    now = _utc_now()
    watermark = max(str(day) for day in phase_a_cache["test_dates"])
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "cache_family": spec.cache_family,
        "tenor": spec.tenor,
        "baseline": baseline,
        "baseline_fingerprint": fingerprint,
        "watermark": watermark,
        "input_prefix": _input_prefix_state(
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            watermark=watermark,
        ),
        "phase_a_cache": _validate_phase_a_cache(phase_a_cache),
        "created_at": created_at or now,
        "updated_at": now,
    }


def _input_prefix_state(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    watermark: str,
) -> dict[str, Any]:
    weekly_bound = _max_bound(weekly_df, "week_id")
    monthly_bound = _max_bound(monthly_df, "month_id")
    bounds = {"daily": watermark, "weekly": weekly_bound, "monthly": monthly_bound}
    return {
        "bounds": bounds,
        "fingerprints": {
            "daily": _frame_prefix_fingerprint(daily_df, "date", watermark),
            "weekly": _frame_prefix_fingerprint(weekly_df, "week_id", weekly_bound),
            "monthly": _frame_prefix_fingerprint(monthly_df, "month_id", monthly_bound),
        },
    }


def _input_prefix_matches(
    value: Any,
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    bounds = value.get("bounds")
    fingerprints = value.get("fingerprints")
    if not isinstance(bounds, Mapping) or not isinstance(fingerprints, Mapping):
        return False
    current = {
        "daily": _frame_prefix_fingerprint(daily_df, "date", bounds.get("daily")),
        "weekly": _frame_prefix_fingerprint(weekly_df, "week_id", bounds.get("weekly")),
        "monthly": _frame_prefix_fingerprint(monthly_df, "month_id", bounds.get("monthly")),
    }
    return current == {name: str(fingerprints.get(name)) for name in current}


def _max_bound(df: pd.DataFrame, key: str) -> str | int | None:
    if key not in df.columns or df.empty:
        return None
    values = _normalized_key(df[key], key).dropna()
    if values.empty:
        return None
    value = values.max()
    return int(value) if key == "week_id" else str(value)


def _frame_prefix_fingerprint(df: pd.DataFrame, key: str, bound: Any) -> str:
    if key not in df.columns:
        raise ValueError(f"input frame missing prefix key {key}")
    work = df.copy()
    work[key] = _normalized_key(work[key], key)
    if bound is not None:
        normalized_bound = _normalized_bound(bound, key)
        work = work[work[key].notna() & (work[key] <= normalized_bound)]
    work = work.sort_values(key).reset_index(drop=True)
    columns = sorted(str(column) for column in work.columns)
    work = work[columns]
    payload = {
        "key": key,
        "bound": _normalized_bound(bound, key) if bound is not None else None,
        "columns": columns,
        "dtypes": [str(work[column].dtype) for column in columns],
        "row_hashes": pd.util.hash_pandas_object(work, index=False).astype("uint64").tolist(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _normalized_key(values: pd.Series, key: str) -> pd.Series:
    if key == "date":
        return pd.to_datetime(values, errors="coerce").dt.strftime("%Y-%m-%d")
    if key == "week_id":
        return pd.to_numeric(values, errors="coerce").astype("Int64")
    return values.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)


def _normalized_bound(value: Any, key: str) -> str | int:
    if key == "week_id":
        return int(value)
    if key == "date":
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    return str(value).removesuffix(".0")


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_pickle_dump(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temp_path = Path(handle.name)
            pickle.dump(dict(payload), handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        with temp_path.open("rb") as handle:
            verified = pickle.load(handle)
        if not isinstance(verified, dict) or verified.get("schema_version") != CACHE_SCHEMA_VERSION:
            raise ValueError("atomic cache verification failed")
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _quarantine_invalid_cache(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    invalid_path = path.with_name(f"{path.name}.invalid-{stamp}-{os.getpid()}")
    os.replace(path, invalid_path)
    return invalid_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
