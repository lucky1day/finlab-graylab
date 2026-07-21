from __future__ import annotations

import copy
import json
import os
import pickle
import tempfile
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd


def target_map(calendar: pd.DataFrame, horizon: int = 5) -> dict[str, str]:
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if "rdate" not in calendar.columns:
        raise ValueError("calendar must contain rdate")
    parsed = pd.to_datetime(calendar["rdate"], errors="coerce")
    if parsed.isna().any():
        raise ValueError("calendar contains invalid rdate")
    values = parsed.dt.strftime("%Y-%m-%d").tolist()
    if values != sorted(set(values)):
        raise ValueError("calendar rdate must be unique and increasing")
    return {
        values[index]: values[index + horizon]
        for index in range(len(values) - horizon)
    }


def splice_phase_cache(
    baseline: Mapping[str, Any],
    ablation: Mapping[str, Any],
    *,
    feature_to_target: Mapping[str, str],
    target_start: str,
    target_end: str,
) -> dict[str, Any]:
    baseline_dates, baseline_results = _validated_cache(baseline, "baseline")
    ablation_dates, ablation_results = _validated_cache(ablation, "ablation")
    baseline_keys = [_config_key(result["config"]) for result in baseline_results]
    ablation_by_key = {
        _config_key(result["config"]): result for result in ablation_results
    }
    if len(ablation_by_key) != len(ablation_results):
        raise RuntimeError("ablation cache contains duplicate config")
    if set(baseline_keys) != set(ablation_by_key):
        raise RuntimeError("baseline and ablation cache config sets differ")

    replacement_dates = [
        feature_date
        for feature_date in baseline_dates
        if target_start
        <= str(feature_to_target.get(feature_date, ""))
        <= target_end
    ]
    ablation_position = {
        feature_date: index for index, feature_date in enumerate(ablation_dates)
    }
    missing = [
        feature_date
        for feature_date in replacement_dates
        if feature_date not in ablation_position
    ]
    if missing:
        raise RuntimeError(f"missing ablation dates: {missing[:5]}")
    baseline_position = {
        feature_date: index for index, feature_date in enumerate(baseline_dates)
    }

    merged_results: list[dict[str, Any]] = []
    for baseline_result, key in zip(baseline_results, baseline_keys, strict=True):
        ablation_result = ablation_by_key[key]
        preds = np.asarray(baseline_result["preds"], dtype=np.int32).copy()
        probs = np.asarray(baseline_result["probs"], dtype=np.float64).copy()
        ablation_preds = np.asarray(ablation_result["preds"], dtype=np.int32)
        ablation_probs = np.asarray(ablation_result["probs"], dtype=np.float64)
        for feature_date in replacement_dates:
            baseline_index = baseline_position[feature_date]
            ablation_index = ablation_position[feature_date]
            preds[baseline_index] = ablation_preds[ablation_index]
            probs[baseline_index] = ablation_probs[ablation_index]
        merged_results.append(
            {
                "config": copy.deepcopy(baseline_result["config"]),
                "preds": preds,
                "probs": probs,
            }
        )
    return {
        "test_dates": list(baseline_dates),
        "results": merged_results,
    }


def write_json_checkpoint(
    output_root: str | Path,
    relative_path: str | Path,
    payload: Any,
) -> Path:
    target = _confined_path(output_root, relative_path)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8") + b"\n"
    _atomic_write(target, encoded)
    return target


def write_pickle_checkpoint(
    output_root: str | Path,
    relative_path: str | Path,
    payload: Any,
) -> Path:
    target = _confined_path(output_root, relative_path)
    _atomic_write(target, pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
    return target


def _validated_cache(
    raw: Mapping[str, Any],
    name: str,
) -> tuple[list[str], list[Mapping[str, Any]]]:
    if not isinstance(raw, Mapping):
        raise RuntimeError(f"{name} cache must be a mapping")
    dates = [
        pd.Timestamp(value).strftime("%Y-%m-%d")
        for value in list(raw.get("test_dates") or [])
    ]
    if len(dates) != len(set(dates)):
        raise RuntimeError(f"{name} cache contains duplicate test_dates")
    if dates != sorted(dates):
        raise RuntimeError(f"{name} cache test_dates must be increasing")
    results = list(raw.get("results") or [])
    if not dates or not results:
        raise RuntimeError(f"{name} cache is empty")
    keys: list[str] = []
    for index, result in enumerate(results):
        if not isinstance(result, Mapping) or not isinstance(
            result.get("config"), Mapping
        ):
            raise RuntimeError(f"{name} cache result {index} has invalid config")
        preds = np.asarray(result.get("preds"))
        probs = np.asarray(result.get("probs"))
        if len(preds) != len(dates) or len(probs) != len(dates):
            raise RuntimeError(f"{name} cache result {index} length mismatch")
        if not np.isfinite(probs.astype(np.float64)).all():
            raise RuntimeError(
                f"{name} cache result {index} probabilities must be finite"
            )
        keys.append(_config_key(result["config"]))
    if len(keys) != len(set(keys)):
        raise RuntimeError(f"{name} cache contains duplicate config")
    return dates, results


def _config_key(config: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(config),
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _confined_path(
    output_root: str | Path,
    relative_path: str | Path,
) -> Path:
    root = Path(output_root).resolve()
    target = (root / Path(relative_path)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"checkpoint path is outside experiment root: {target}") from exc
    return target


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
