from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


SAMPLE_COLUMNS = (
    "scheme_id",
    "target_tenor",
    "feature_date",
    "target_date",
    "label",
)
PERIODS = ("2026-04", "2026-05", "2026-06")


@dataclass(frozen=True)
class MetricCounts:
    eligible: int
    trades: int
    correct: int

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.trades if self.trades else None

    @property
    def trade_rate(self) -> float | None:
        return self.trades / self.eligible if self.eligible else None


def align_branches(
    baseline: pd.DataFrame,
    ablation: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = set(SAMPLE_COLUMNS) | {"direction"}
    for name, frame in (("baseline", baseline), ("ablation", ablation)):
        missing = sorted(required - set(frame.columns))
        if missing:
            raise RuntimeError(f"{name} rows missing columns: {missing}")
        if frame.duplicated(list(SAMPLE_COLUMNS[:-1])).any():
            raise RuntimeError(f"{name} rows contain duplicate sample keys")
    left = baseline.sort_values(list(SAMPLE_COLUMNS)).reset_index(drop=True)
    right = ablation.sort_values(list(SAMPLE_COLUMNS)).reset_index(drop=True)
    if not left.loc[:, SAMPLE_COLUMNS].equals(right.loc[:, SAMPLE_COLUMNS]):
        raise RuntimeError("baseline and ablation sample keys or labels differ")
    return left, right


def metric_counts(frame: pd.DataFrame) -> MetricCounts:
    direction = pd.to_numeric(frame["direction"], errors="raise").astype(int)
    labels = pd.to_numeric(frame["label"], errors="raise").astype(int)
    traded = direction.ne(0)
    return MetricCounts(
        eligible=int(len(frame)),
        trades=int(traded.sum()),
        correct=int((traded & direction.eq(labels)).sum()),
    )


def monthly_comparison(
    baseline: pd.DataFrame,
    ablation: pd.DataFrame,
) -> list[dict[str, object]]:
    left, right = align_branches(baseline, ablation)
    target_month = pd.to_datetime(
        left["target_date"], errors="raise"
    ).dt.strftime("%Y-%m")
    included = target_month.isin(PERIODS)
    left = left.loc[included].reset_index(drop=True)
    right = right.loc[included].reset_index(drop=True)
    target_month = target_month.loc[included].reset_index(drop=True)

    rows: list[dict[str, object]] = []
    for period in PERIODS:
        mask = target_month.eq(period)
        rows.append(
            _comparison_row(
                period,
                metric_counts(left.loc[mask]),
                metric_counts(right.loc[mask]),
            )
        )
    rows.append(
        _comparison_row(
            "2026-04..06",
            metric_counts(left),
            metric_counts(right),
        )
    )
    return rows


def _comparison_row(
    period: str,
    baseline: MetricCounts,
    ablation: MetricCounts,
) -> dict[str, object]:
    accuracy_delta = (
        None
        if baseline.accuracy is None or ablation.accuracy is None
        else ablation.accuracy - baseline.accuracy
    )
    trade_rate_delta = (
        None
        if baseline.trade_rate is None or ablation.trade_rate is None
        else ablation.trade_rate - baseline.trade_rate
    )
    return {
        "period": period,
        "baseline": baseline,
        "ablation": ablation,
        "accuracy_delta": accuracy_delta,
        "trade_rate_delta": trade_rate_delta,
    }
