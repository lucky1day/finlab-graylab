from __future__ import annotations

import ast
import csv
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from shared.blackbox_v2.intake import validate_delivery


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUCCESSORS = (
    "weekly_5y_direct_0529_bbv2",
    "weekly_7y_cross_d_overlay_0529_bbv2",
    "weekly_10y_d_overlay_0529_bbv2",
)
RESULT_FIELDS = {
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
}


def _load_successor(scheme_id: str) -> ModuleType:
    path = (
        PROJECT_ROOT
        / "schemes"
        / scheme_id
        / "delivery"
        / f"{scheme_id}.py"
    )
    module_name = f"_test_{scheme_id}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    prior = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior
    return module


@pytest.fixture(scope="module")
def modules() -> dict[str, ModuleType]:
    return {scheme_id: _load_successor(scheme_id) for scheme_id in SUCCESSORS}


@pytest.mark.parametrize("scheme_id", SUCCESSORS)
def test_delivery_is_strict_self_contained_two_file_package(scheme_id: str) -> None:
    delivery = PROJECT_ROOT / "schemes" / scheme_id / "delivery"
    metadata, script, metadata_path = validate_delivery(delivery)

    assert metadata.scheme_id == scheme_id
    assert metadata.task_type == "weekly_point"
    assert metadata.horizon == 1
    assert {script.name, metadata_path.name} == {
        f"{scheme_id}.py",
        f"{scheme_id}.json",
    }

    tree = ast.parse(script.read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_roots.add((node.module or "").split(".", 1)[0])
    assert imported_roots.isdisjoint(
        {"shared", "schemes", "scheduler", "backtests", "subprocess", "multiprocessing"}
    )


def _synthetic_rule_input() -> pd.DataFrame:
    position = np.arange(120, dtype=float)
    return pd.DataFrame(
        {
            "week_id": np.arange(202401, 202521),
            "TB1YWI3C": 1.2 + 0.01 * np.sin(position / 3),
            "TB3YWI3C": 1.4 + 0.015 * np.cos(position / 5),
            "TB5YWI3C": 1.6 + 0.02 * np.sin(position / 7),
            "TB7YWI3C": 1.8 + 0.025 * np.cos(position / 9),
            "TB0YWI3C": 2.0 + 0.03 * np.sin(position / 11),
        }
    )


def test_5y_and_7y_readable_cores_preserve_native_directions(
    modules: dict[str, ModuleType],
) -> None:
    from schemes.weekly_5y_direct_0529.core.rule_vote import build_rule_vote
    from schemes.weekly_7y_cross_d_overlay_0529.core.cross_d_overlay import (
        build_cross_d_overlay,
    )

    weekly = _synthetic_rule_input()
    native_5y = build_rule_vote(weekly).loc[:, ["week_id", "final_pred_label"]]
    successor_5y = modules["weekly_5y_direct_0529_bbv2"]._run_algorithm(weekly)
    assert successor_5y.to_records(index=False).tolist() == [
        (int(row.week_id), int(row.final_pred_label))
        for row in native_5y.itertuples(index=False)
    ]

    native_7y = build_cross_d_overlay(weekly).loc[
        :, ["week_id", "cross_d_pred_label"]
    ]
    successor_7y = modules["weekly_7y_cross_d_overlay_0529_bbv2"]._run_algorithm(
        weekly
    )
    assert successor_7y.to_records(index=False).tolist() == [
        (int(row.week_id), int(row.cross_d_pred_label))
        for row in native_7y.itertuples(index=False)
    ]


def _transport_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    calendar = pd.DataFrame(
        {
            "rdate": pd.to_datetime(
                ["2026-01-02", "2026-01-09", "2026-01-16", "2026-01-23"]
            ),
            "week_id": [202601, 202602, 202603, 202604],
        }
    )
    daily = pd.DataFrame({"date": calendar["rdate"]})
    weekly = pd.DataFrame(
        {
            "week_id": [202601, 202602, 202603, 202604],
            "TB1YWI3C": [1.1, 1.2, 1.3, 1.4],
            "TB3YWI3C": [1.3, 1.4, 1.5, 1.6],
            "TB5YWI3C": [1.5, 1.6, 1.7, 1.8],
            "TB7YWI3C": [1.7, 1.8, 1.9, 2.0],
            "TB0YWI3C": [1.9, 2.0, 2.1, 2.2],
        }
    )
    return calendar, daily, weekly


def _request(week_id: int, feature: str, target: str) -> dict[str, str]:
    return {
        "request_id": f"request-{week_id}",
        "predict_date": (
            pd.Timestamp(feature) + pd.Timedelta(days=1)
        ).date().isoformat(),
        "feature_date": feature,
        "target_date": target,
        "daily_cutoff_key": feature,
        "weekly_cutoff_key": str(week_id),
        "monthly_cutoff_key": feature[:7].replace("-", ""),
    }


@pytest.mark.parametrize("scheme_id", SUCCESSORS)
def test_weekly_successor_accepts_more_than_100_backtest_requests(
    scheme_id: str,
    modules: dict[str, ModuleType],
    tmp_path: Path,
) -> None:
    module = modules[scheme_id]
    requests_path = tmp_path / f"{scheme_id}-101.csv"
    request = _request(202603, "2026-01-16", "2026-01-23")
    with requests_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=module.REQUEST_FIELDS)
        writer.writeheader()
        for index in range(101):
            writer.writerow(request | {"request_id": f"request-{index}"})

    assert len(module.load_requests(requests_path)) == 101


@pytest.mark.parametrize("scheme_id", SUCCESSORS)
def test_transport_preserves_request_order_exact_fields_and_future_isolation(
    scheme_id: str,
    modules: dict[str, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = modules[scheme_id]
    calendar, daily, weekly = _transport_frames()
    requests = [
        _request(202603, "2026-01-16", "2026-01-23"),
        _request(202601, "2026-01-02", "2026-01-09"),
    ]

    observed_maxima: list[int] = []

    def fake_algorithm(frame: pd.DataFrame) -> pd.DataFrame:
        observed_maxima.append(int(frame["week_id"].max()))
        return pd.DataFrame(
            {
                "week_id": frame["week_id"],
                "predicted_direction": np.where(frame["week_id"] % 2, 1, -1),
            }
        )

    monkeypatch.setattr(module, "_run_algorithm", fake_algorithm)
    expected = module.generate(requests, (calendar, daily, weekly))
    actual = module.generate(
        list(reversed(requests)),
        (calendar, daily, weekly),
    )

    assert [row["request_id"] for row in actual] == [
        request["request_id"] for request in reversed(requests)
    ]
    assert {row["request_id"]: row for row in actual} == {
        row["request_id"]: row for row in expected
    }
    assert all(set(row) == RESULT_FIELDS for row in actual)
    assert observed_maxima == [202603, 202603]

    without_future = module.generate(
        [requests[1]],
        (calendar.iloc[:3].copy(), daily.iloc[:3].copy(), weekly.iloc[:3].copy()),
    )
    with_future = module.generate([requests[1]], (calendar, daily, weekly))
    assert without_future == with_future
    assert observed_maxima[-2:] == [202601, 202601]


def test_10y_model2_only_builds_segments_available_at_request_cutoff(
    modules: dict[str, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = modules["weekly_10y_d_overlay_0529_bbv2"]
    base = pd.DataFrame(
        {
            "date": pd.to_datetime(["2022-12-30", "2025-03-28"]),
            "week_id": [202252, 202513],
            module.TARGET_RATE_COL: [2.0, 2.1],
            "TB1YWI3C": [1.0, 1.1],
            "TB5YWI3C": [1.5, 1.6],
        }
    )
    observed: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []

    monkeypatch.setattr(
        module,
        "load_engineered_frame",
        lambda weekly: (base, []),
    )

    def fake_segment(
        base_df: pd.DataFrame,
        features: list[str],
        name: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        observed.append((name, start, end))
        return pd.DataFrame(
            {
                "segment": [name],
                "week_id": [202513],
                "date": [pd.Timestamp("2025-03-28")],
                "actual_label": [np.nan],
                "future_return": [np.nan],
                "model2_prob_up": [0.5],
                "model2_pred_label": [-1],
                "selected_feature_count": [0],
            }
        )

    monkeypatch.setattr(module, "build_model2_segment", fake_segment)
    module.build_model2_predictions(pd.DataFrame())

    assert [item[0] for item in observed] == ["2023_2024", "2025H1"]
    assert observed[-1][2] == pd.Timestamp("2025-03-28")


@pytest.mark.parametrize("scheme_id", SUCCESSORS)
def test_invalid_request_fails_without_stdout_or_output(
    scheme_id: str,
    modules: dict[str, ModuleType],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = modules[scheme_id]
    request = tmp_path / f"{scheme_id}-invalid.json"
    request.write_text(json.dumps({"request_id": "incomplete"}), encoding="utf-8")
    data_dir = tmp_path / f"{scheme_id}-data"
    data_dir.mkdir()
    output = tmp_path / f"{scheme_id}-output.json"

    assert module.main(
        [
            "predict",
            "--request",
            str(request),
            "--data-dir",
            str(data_dir),
            "--output",
            str(output),
        ]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))
