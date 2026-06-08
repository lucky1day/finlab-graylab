from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .lgbm_predictor import PredictionResult


@dataclass(frozen=True)
class ModelArtifactPaths:
    model_path: Path
    metadata_path: Path


def _metadata(result: PredictionResult) -> dict:
    return {
        "rdate": result.rdate,
        "target_date": result.target_date,
        "feature_date": result.feature_date,
        "tenor": result.tenor,
        "frequency": result.frequency,
        "pred_label": result.pred_label,
        "prob_up": result.prob_up,
        "threshold_used": result.threshold_used,
        "base_pred": result.base_pred,
        "base_decision": result.base_decision,
        "vote_sum": result.vote_sum,
        "decision": result.decision,
        "feature_columns": result.feature_columns,
        "feature_origin_map": result.feature_origin_map,
        "train_start": result.train_start,
        "train_end": result.train_end,
        "config": asdict(result.config),
    }


def save_model_artifacts(result: PredictionResult, base_dir: str | Path) -> ModelArtifactPaths:
    target_dir = Path(base_dir) / result.target_date
    target_dir.mkdir(parents=True, exist_ok=True)
    model_path = target_dir / f"{result.frequency}_model.txt"
    metadata_path = target_dir / f"{result.frequency}_metadata.json"
    if result.model is not None:
        # LightGBM's C save_model can fail on non-ASCII Windows paths. Writing
        # the model string through Python keeps local and production paths safe.
        _write_utf8(model_path, result.model.booster_.model_to_string())
    else:
        _write_utf8(model_path, "cold_fallback_no_model\n")
    _write_utf8(metadata_path, json.dumps(_metadata(result), ensure_ascii=False, indent=2))
    return ModelArtifactPaths(model_path=model_path, metadata_path=metadata_path)


def _write_utf8(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(content)
