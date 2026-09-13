from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


def test_ephemeral_input_root_is_private_and_must_be_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import input_artifacts

    root = tmp_path.resolve()
    monkeypatch.setenv(
        input_artifacts.EPHEMERAL_NATIVE_INPUT_ROOT_ENV,
        str(root),
    )
    monkeypatch.delenv("BFL_SOURCE_DB_CONFIG_PATH", raising=False)
    path = input_artifacts.input_artifact_path(
        scheme_id="daily_demo",
        frequency="daily",
        predict_date="2026-07-24",
        output_root=Path("/persistent/inputs"),
    )
    assert path == root / "views" / "daily_demo" / "daily_output_2026-07-24.csv"

    monkeypatch.setenv(
        input_artifacts.EPHEMERAL_NATIVE_INPUT_ROOT_ENV,
        "relative",
    )
    with pytest.raises(ValueError, match="absolute"):
        input_artifacts.input_artifact_path(
            scheme_id="daily_demo",
            frequency="daily",
            predict_date="2026-07-24",
        )


def test_atomic_artifact_failure_preserves_previous_file(
    tmp_path: Path,
) -> None:
    from shared import input_artifacts

    frame = pd.DataFrame(
        [{"date": "2026-07-23", "TB1YWI0C": 1.2}]
    )
    final_path = input_artifacts.input_artifact_path(
        scheme_id="daily_demo",
        frequency="daily",
        predict_date="2026-07-24",
        output_root=tmp_path,
    )
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous-complete\n")

    def fail_after_partial_write(_frame, path) -> None:
        Path(path).write_bytes(b"partial\n")
        raise OSError("disk full")

    with (
        patch.object(
            input_artifacts._data_service,
            "build_daily_output_from_db",
            return_value=frame,
        ),
        patch.object(
            input_artifacts._data_service,
            "save_daily_output",
            side_effect=fail_after_partial_write,
        ),
        pytest.raises(OSError, match="disk full"),
    ):
        input_artifacts.build_daily_input_artifact(
            scheme_id="daily_demo",
            predict_date="2026-07-24",
            start_date="2026-07-23",
            end_date="2026-07-23",
            engine=object(),
            output_root=tmp_path,
        )

    assert final_path.read_bytes() == b"previous-complete\n"
    assert list(final_path.parent.glob(f".{final_path.name}.*.tmp")) == []


def test_all_native_builders_use_current_database(
    tmp_path: Path,
) -> None:
    from shared import input_artifacts

    engine = object()
    daily = pd.DataFrame([{"date": "2026-07-23", "D": 1.0}])
    weekly = pd.DataFrame([{"week_id": 202629, "W": 2.0}])
    monthly = pd.DataFrame([{"month_id": "202607", "M": 3.0}])
    with (
        patch.object(
            input_artifacts._data_service,
            "build_daily_output_from_db",
            return_value=daily,
        ) as daily_builder,
        patch.object(
            input_artifacts._data_service,
            "build_weekly_output_from_db",
            return_value=weekly,
        ) as weekly_builder,
        patch.object(
            input_artifacts._data_service,
            "build_monthly_output_from_db",
            return_value=monthly,
        ) as monthly_builder,
    ):
        artifacts = (
            input_artifacts.build_daily_input_artifact(
                scheme_id="daily_demo",
                predict_date="2026-07-24",
                start_date="2026-07-23",
                end_date="2026-07-23",
                engine=engine,
                output_root=tmp_path,
            ),
            input_artifacts.build_weekly_input_artifact(
                scheme_id="weekly_demo",
                predict_date="2026-07-24",
                start_week=202629,
                end_week=202629,
                as_of_date="2026-07-23",
                engine=engine,
                output_root=tmp_path,
            ),
            input_artifacts.build_monthly_input_artifact(
                scheme_id="monthly_demo",
                predict_date="2026-07-24",
                start_date="2026-07-01",
                end_date="2026-07-23",
                engine=engine,
                output_root=tmp_path,
            ),
        )

    assert daily_builder.call_args.kwargs["engine"] is engine
    assert weekly_builder.call_args.kwargs["engine"] is engine
    assert monthly_builder.call_args.kwargs["engine"] is engine
    assert all(item.path.is_file() for item in artifacts)
    assert list(tmp_path.rglob("*.json")) == []


def test_ephemeral_native_input_concurrent_call_builds_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import input_artifacts

    monkeypatch.setenv(
        input_artifacts.EPHEMERAL_NATIVE_INPUT_ROOT_ENV,
        str(tmp_path.resolve()),
    )
    monkeypatch.delenv("BFL_SOURCE_DB_CONFIG_PATH", raising=False)
    frame = pd.DataFrame([{"date": "2026-07-23", "D": 1.0}])

    def run(scheme_id: str):
        return input_artifacts.build_daily_input_artifact(
            scheme_id=scheme_id,
            predict_date="2026-07-24",
            start_date="2026-01-01",
            end_date="2026-07-23",
            engine=object(),
        )

    with patch.object(
        input_artifacts._data_service,
        "build_daily_output_from_db",
        return_value=frame,
    ) as builder:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = tuple(pool.map(run, ("first", "second")))

    builder.assert_called_once()
    assert first.path != second.path
    assert first.path.stat().st_ino == second.path.stat().st_ino
    assert first.path.stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("boundary", ["date_range", "source_database"])
def test_ephemeral_input_keeps_distinct_sources_separate(tmp_path, monkeypatch, boundary):
    from shared import input_artifacts

    monkeypatch.setenv(input_artifacts.EPHEMERAL_NATIVE_INPUT_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv("BFL_SOURCE_DB_CONFIG_PATH", raising=False)
    if boundary == "source_database":
        monkeypatch.setenv("BFL_SOURCE_DB_CONFIG_PATH", "/private/source.json")
    frames = [pd.DataFrame([{"date": "2026-07-23", "D": value}]) for value in (1.0, 2.0)]
    with patch.object(input_artifacts._data_service, "build_daily_output_from_db", side_effect=frames):
        artifacts = [
            input_artifacts.build_daily_input_artifact(
                scheme_id=scheme_id, predict_date="2026-07-24",
                start_date=("2025-01-01" if boundary == "date_range" and index else "2026-01-01"),
                end_date="2026-07-23", engine=object(),
            )
            for index, scheme_id in enumerate(("first", "second"))
        ]
    assert [pd.read_csv(item.path)["D"].iloc[0] for item in artifacts] == [1.0, 2.0]
