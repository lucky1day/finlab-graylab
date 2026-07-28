from __future__ import annotations

from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd
import pytest

from shared.liwei_0616_cache_projection import (
    AuxiliaryDependencyProjection,
    build_auxiliary_dependency_projection,
)


def _proof_files(tmp_path: Path) -> tuple[Path, Path]:
    core_file = tmp_path / "v31_common.py"
    alignment_file = tmp_path / "data_alignment.py"
    core_file.write_bytes(b"exact core bytes\n")
    alignment_file.write_bytes(b"exact alignment bytes\n")
    return core_file, alignment_file


def _daily(*dates: object) -> pd.DataFrame:
    return pd.DataFrame({"date": list(dates), "daily_unused": range(len(dates))})


def _weekly() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "week_id": [202630, 202631],
            "used_weekly": [2.5, 900.0],
            "unused_weekly": [10.0, 20.0],
        }
    )


def _monthly() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "month_id": ["202606", "202608"],
            "used_monthly": [4.5, 800.0],
            "unused_monthly": [30.0, 40.0],
        }
    )


def _exact_prepare_model_frames(
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    daily_dates: pd.Series,
    date_to_week: Mapping[str, int | str] | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    normalized_dates = pd.Series(pd.to_datetime(daily_dates), copy=True)
    assert normalized_dates.is_monotonic_increasing
    assert daily_df["date"].equals(normalized_dates)

    weekly_by_id = weekly_df.set_index("week_id")["used_weekly"]
    weekly_values: list[float] = []
    for day in normalized_dates:
        week_id = (
            None
            if date_to_week is None
            else date_to_week.get(day.strftime("%Y-%m-%d"))
        )
        weekly_values.append(
            0.0 if week_id is None else float(weekly_by_id.get(int(week_id), 0.0))
        )

    monthly_by_id = monthly_df.assign(
        month_id=monthly_df["month_id"].astype(str)
    ).set_index("month_id")["used_monthly"]
    monthly_values = [
        float(
            monthly_by_id.get(
                (day.to_period("M") - 1).strftime("%Y%m"),
                0.0,
            )
        )
        for day in normalized_dates
    ]
    return (
        pd.DataFrame({"used_weekly": weekly_values}),
        pd.DataFrame({"used_monthly": monthly_values}),
    )


def _exact_build_wkmo_features(
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "wk_used_weekly_val": weekly_df["used_weekly"].to_numpy(),
            "mo_used_monthly_val": monthly_df["used_monthly"].to_numpy(),
        }
    )


def _build(
    *,
    tmp_path: Path,
    daily_df: pd.DataFrame | None = None,
    weekly_df: pd.DataFrame | None = None,
    monthly_df: pd.DataFrame | None = None,
    date_to_week: Mapping[str, int | str] | None = None,
    proof_files: tuple[Path, ...] | None = None,
    build_wkmo_features=_exact_build_wkmo_features,
) -> AuxiliaryDependencyProjection:
    return build_auxiliary_dependency_projection(
        daily_df=daily_df
        if daily_df is not None
        else _daily("2026-07-27", "2026-07-24"),
        weekly_df=weekly_df if weekly_df is not None else _weekly(),
        monthly_df=monthly_df if monthly_df is not None else _monthly(),
        date_to_week=date_to_week
        if date_to_week is not None
        else {
            "2026-07-24": 202630,
            "2026-07-27": 202630,
        },
        prepare_model_frames=_exact_prepare_model_frames,
        build_wkmo_features=build_wkmo_features,
        proof_files=proof_files
        if proof_files is not None
        else _proof_files(tmp_path),
    )


def test_projection_excludes_unused_and_future_auxiliary_values(
    tmp_path: Path,
) -> None:
    proof_files = _proof_files(tmp_path)
    baseline = _build(tmp_path=tmp_path, proof_files=proof_files)

    changed_weekly = _weekly()
    changed_weekly.loc[:, "unused_weekly"] = [101.0, 202.0]
    changed_weekly.loc[changed_weekly["week_id"] == 202631, "used_weekly"] = 999.0
    changed_monthly = _monthly()
    changed_monthly.loc[:, "unused_monthly"] = [303.0, 404.0]
    changed_monthly.loc[changed_monthly["month_id"] == "202608", "used_monthly"] = 888.0
    changed = _build(
        tmp_path=tmp_path,
        weekly_df=changed_weekly,
        monthly_df=changed_monthly,
        proof_files=proof_files,
    )

    assert baseline.frame.columns.tolist() == [
        "date",
        "wk_used_weekly_val",
        "mo_used_monthly_val",
    ]
    assert baseline.frame["date"].tolist() == ["2026-07-24", "2026-07-27"]
    assert baseline.frame["date"].max() == "2026-07-27"
    assert not any("unused" in column for column in baseline.frame.columns)
    pd.testing.assert_frame_equal(baseline.frame, changed.frame)
    assert baseline.content_sha256 == changed.content_sha256


def test_projection_records_explicit_and_fallback_week_mapping_modes(
    tmp_path: Path,
) -> None:
    proof_files = _proof_files(tmp_path)
    explicit = _build(tmp_path=tmp_path, proof_files=proof_files)
    fallback = build_auxiliary_dependency_projection(
        daily_df=_daily("2026-07-24", "2026-07-27"),
        weekly_df=_weekly(),
        monthly_df=_monthly(),
        date_to_week=None,
        prepare_model_frames=_exact_prepare_model_frames,
        build_wkmo_features=_exact_build_wkmo_features,
        proof_files=proof_files,
    )

    assert explicit.proof["date_to_week_mode"] == "explicit"
    assert fallback.proof["date_to_week_mode"] == "fallback"
    assert explicit.proof["date_to_week_sha256"]
    assert fallback.proof["date_to_week_sha256"]
    assert explicit.content_sha256 != fallback.content_sha256


@pytest.mark.parametrize(
    ("daily_df", "message"),
    [
        (pd.DataFrame({"value": [1]}), "date"),
        (_daily("not-a-date"), "invalid"),
        (_daily("2026-07-24", "2026-07-24 15:00:00"), "duplicate"),
    ],
)
def test_projection_rejects_invalid_daily_grids(
    tmp_path: Path,
    daily_df: pd.DataFrame,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _build(tmp_path=tmp_path, daily_df=daily_df)


def test_projection_preserves_callback_output_across_year_and_missing_periods(
    tmp_path: Path,
) -> None:
    projection = _build(
        tmp_path=tmp_path,
        daily_df=_daily("2026-01-02", "2025-12-31"),
        weekly_df=pd.DataFrame(
            {
                "week_id": [202553],
                "used_weekly": [12.25],
                "unused_weekly": [1.0],
            }
        ),
        monthly_df=pd.DataFrame(
            {
                "month_id": ["202511"],
                "used_monthly": [6.75],
                "unused_monthly": [2.0],
            }
        ),
        date_to_week={"2025-12-31": "202553"},
    )

    assert projection.frame.to_dict("records") == [
        {
            "date": "2025-12-31",
            "wk_used_weekly_val": 12.25,
            "mo_used_monthly_val": 6.75,
        },
        {
            "date": "2026-01-02",
            "wk_used_weekly_val": 0.0,
            "mo_used_monthly_val": 0.0,
        },
    ]


def test_date_to_week_mapping_order_does_not_change_sha256(
    tmp_path: Path,
) -> None:
    proof_files = _proof_files(tmp_path)
    first = _build(
        tmp_path=tmp_path,
        date_to_week={
            "2026-07-27": 202630,
            "2026-07-24": "202630",
        },
        proof_files=proof_files,
    )
    second = _build(
        tmp_path=tmp_path,
        date_to_week={
            "2026-07-24": 202630,
            "2026-07-27": "202630",
        },
        proof_files=proof_files,
    )

    assert first.proof["date_to_week_sha256"] == second.proof[
        "date_to_week_sha256"
    ]
    assert first.content_sha256 == second.content_sha256


def test_proof_file_byte_change_changes_projection_sha256(
    tmp_path: Path,
) -> None:
    proof_files = _proof_files(tmp_path)
    before = _build(tmp_path=tmp_path, proof_files=proof_files)

    proof_files[0].write_bytes(b"changed exact core bytes\n")
    after = _build(tmp_path=tmp_path, proof_files=proof_files)

    assert before.frame.equals(after.frame)
    assert before.content_sha256 != after.content_sha256


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_projection_rejects_unreadable_proof_files(
    tmp_path: Path,
    kind: str,
) -> None:
    invalid_path = tmp_path / "invalid-proof"
    if kind == "directory":
        invalid_path.mkdir()

    with pytest.raises(ValueError, match="proof file"):
        _build(tmp_path=tmp_path, proof_files=(invalid_path,))


def test_projection_hashes_exact_float_values_without_rounding(
    tmp_path: Path,
) -> None:
    def feature_builder(
        weekly_df: pd.DataFrame,
        monthly_df: pd.DataFrame,
    ) -> pd.DataFrame:
        frame = _exact_build_wkmo_features(weekly_df, monthly_df)
        frame.loc[0, "wk_used_weekly_val"] = np.nextafter(2.5, np.inf)
        return frame

    proof_files = _proof_files(tmp_path)
    baseline = _build(tmp_path=tmp_path, proof_files=proof_files)
    one_ulp_changed = _build(
        tmp_path=tmp_path,
        proof_files=proof_files,
        build_wkmo_features=feature_builder,
    )

    assert baseline.content_sha256 != one_ulp_changed.content_sha256


def test_repeated_projection_build_is_deterministic(tmp_path: Path) -> None:
    proof_files = _proof_files(tmp_path)
    first = _build(tmp_path=tmp_path, proof_files=proof_files)
    second = _build(tmp_path=tmp_path, proof_files=proof_files)

    pd.testing.assert_frame_equal(first.frame, second.frame)
    assert first.proof == second.proof
    assert first.content_sha256 == second.content_sha256
    assert first.proof["columns"] == list(first.frame.columns)
    assert first.proof["dtypes"] == [
        str(first.frame[column].dtype) for column in first.frame.columns
    ]
    assert first.proof["daily_grid_sha256"]
