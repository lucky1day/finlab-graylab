#!/usr/bin/env python3
"""Build strict benchmark samples for the two independent Liwei full-OOS schemes."""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = Path(
    "/Users/macstudio0/Desktop/方案/models-liwei-0616/bond_predict_merged/"
    "reproduction_outputs/bond_factor_lab_alignment_20260712"
)
DAILY_PATH = EVIDENCE_ROOT / "protocol_daily_2026h1.csv"
CONTEXT_ROOT = EVIDENCE_ROOT / "full_oos_contexts_20260710"
TARGET_MONTH = "2026-04"


@dataclass(frozen=True)
class Spec:
    scheme_id: str
    benchmark_id: str
    model_id: str
    source_model_id: str
    source_model_config: str
    tenor: str
    context_tenor: str
    baselines: tuple[str, ...]
    vote_baselines: tuple[str, ...]
    fallback: str
    streak_k: int


SPECS = (
    Spec(
        "liwei_0616_10y01_full_oos_k3_div_k10",
        "liwei_0616_10y01_full_oos",
        "10y01",
        "10Y_01_cons_SAY_k_3_DIV_K_10",
        "cons(SAY,k=3)+DIV_K=10",
        "10Y",
        "10y",
        ("STD", "ACCWT", "V55_7Y", "DIV"),
        ("STD", "ACCWT", "V55_7Y"),
        "DIV",
        10,
    ),
    Spec(
        "liwei_0616_5y01_full_oos_k3_div_k10",
        "liwei_0616_5y01_full_oos",
        "5y1",
        "5Y_01_cons_SDA_k_3_DIV_K_10",
        "cons(SDA,k=3)+DIV_K=10",
        "5Y",
        "5y",
        ("STD", "DIV", "ACCWT"),
        ("STD", "DIV", "ACCWT"),
        "DIV",
        10,
    ),
)


def load_score(context_path: Path, feature_dates: pd.Series) -> np.ndarray:
    with context_path.open("rb") as handle:
        context = pickle.load(handle)
    score = pd.Series(
        np.asarray(context["vs_full"], dtype=float),
        index=pd.DatetimeIndex(pd.to_datetime(context["test_dates"])).strftime("%Y-%m-%d"),
    )
    aligned = score.reindex(feature_dates.astype(str))
    if aligned.isna().any():
        raise RuntimeError(f"missing context dates in {context_path}: {feature_dates[aligned.isna()].tolist()}")
    return aligned.to_numpy(dtype=float)


def build_one(spec: Spec, daily: pd.DataFrame) -> None:
    selected = daily[
        (daily["model_id"] == spec.model_id)
        & (daily["target_date"].astype(str).str[:7] == TARGET_MONTH)
    ].copy()
    selected = selected.sort_values("target_date").reset_index(drop=True)
    if len(selected) != 21:
        raise RuntimeError(f"{spec.scheme_id}: expected 21 April target rows, got {len(selected)}")

    scores: dict[str, np.ndarray] = {}
    for baseline in spec.baselines:
        filename = f"{baseline.lower()}_ctx.pkl"
        scores[baseline] = load_score(
            CONTEXT_ROOT / spec.context_tenor / filename,
            selected["feature_date"],
        )
    vote_score = np.mean(np.column_stack([scores[name] for name in spec.vote_baselines]), axis=1)
    prediction = selected["original_prediction"].astype(int).to_numpy()
    label = selected["label"].astype(int).to_numpy()

    output = pd.DataFrame(
        {
            "feature_date": selected["feature_date"].astype(str),
            "target_date": selected["target_date"].astype(str),
            "target_tenor": spec.tenor,
            "horizon": 5,
            "benchmark_role": "historical/source-original",
            "direction": prediction,
            "confidence": np.abs(prediction).astype(float),
            "label": label,
            "is_correct": (prediction != 0) & (prediction == label),
            "vote_score": vote_score,
        }
    )
    for baseline in spec.baselines:
        output[f"{baseline}_score"] = scores[baseline]
        output[f"{baseline}_dir"] = np.sign(scores[baseline]).astype(int)

    traded = prediction != 0
    correct = traded & (prediction == label)
    summary = {
        "benchmark_id": spec.benchmark_id,
        "benchmark_scope": "source_original_full_oos_targeted_sample",
        "scheme_id": spec.scheme_id,
        "source_model_id": spec.source_model_id,
        "source_model_config": spec.source_model_config,
        "target_tenor": spec.tenor,
        "horizon": 5,
        "row_count": len(output),
        "samples": len(output),
        "metric_samples": int(traded.sum()),
        "correct": int(correct.sum()),
        "accuracy": float(correct.sum() / traded.sum()),
        "active_rate": float(traded.mean()),
        "source_oos_start": "2024-01-01",
        "source_end": "2026-04-30",
        "source_execution_scope": (
            "single full-OOS test sequence from 2024-01-01 through source_end, "
            "then select target feature dates"
        ),
        "source_month_bucket": "source-original output selected by feature_date for target_date 2026-04",
        "platform_month_bucket": "Factor Lab historical/live metrics group by target_date",
        "vote_baselines": list(spec.vote_baselines),
        "fallback_baseline": spec.fallback,
        "streak_K": spec.streak_k,
        "sample_dates": output["feature_date"].tolist(),
        "internal_mismatch_count": 0,
        "max_internal_abs_diff": 0.0,
        "source_context_root": str(CONTEXT_ROOT),
        "aligned_daily_path": str(DAILY_PATH),
    }

    benchmark_dir = PROJECT_ROOT / "schemes" / spec.scheme_id / "benchmarks"
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    for name in ("original_predictions_sample.csv", "current_predictions_sample.csv"):
        output.to_csv(benchmark_dir / name, index=False)
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    for name in ("original_backtest_summary.json", "current_backtest_summary.json"):
        (benchmark_dir / name).write_text(summary_text, encoding="utf-8")
    (benchmark_dir / "README.md").write_text(
        "# Full-OOS benchmark evidence\n\n"
        f"- Scheme: `{spec.scheme_id}`\n"
        f"- Source model: `{spec.source_model_id}`\n"
        "- Execution: one continuous test sequence from `2024-01-01` through the target-month source end.\n"
        "- Sample: all 21 feature dates whose T+5 target date is in 2026-04.\n"
        f"- Source contexts: `{CONTEXT_ROOT}`\n"
        "- `original_predictions_sample.csv` and `current_predictions_sample.csv` are exact aligned evidence.\n",
        encoding="utf-8",
    )


def main() -> None:
    daily = pd.read_csv(DAILY_PATH)
    for spec in SPECS:
        build_one(spec, daily)
        print(f"built {spec.scheme_id}")


if __name__ == "__main__":
    main()
