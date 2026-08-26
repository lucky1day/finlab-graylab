from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


def test_create_input_engine_uses_current_database_configuration() -> None:
    from shared import input_artifacts

    default_engine = object()
    source_engine = object()
    source_config = object()
    with patch.object(
        input_artifacts._data_service,
        "create_sqlalchemy_engine",
        side_effect=(default_engine, source_engine),
    ) as factory:
        assert input_artifacts.create_input_engine() is default_engine
        assert input_artifacts.create_input_engine(
            database_config=source_config
        ) is source_engine

    assert factory.call_args_list[0].kwargs == {}
    assert factory.call_args_list[1].kwargs == {
        "db_config": source_config
    }


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
    path = input_artifacts.input_artifact_path(
        scheme_id="daily_demo",
        frequency="daily",
        predict_date="2026-07-24",
        output_root=Path("/persistent/inputs"),
    )
    assert path == root / "daily_demo" / "daily_output_2026-07-24.csv"

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
    assert all("input_generation_id" not in item.metadata for item in artifacts)
    assert list(tmp_path.rglob("*.json")) == []


def test_native_builder_emits_harness_only_audit_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import input_artifacts

    audit_root = tmp_path / "audit"
    audit_root.mkdir(mode=0o700)
    audit_root.chmod(0o700)
    monkeypatch.setenv(
        input_artifacts.NATIVE_INPUT_AUDIT_ROOT_ENV,
        str(audit_root.resolve()),
    )
    frame = pd.DataFrame(
        [{"date": "2026-07-23", "TB1YWI0C": 1.2}]
    )
    with patch.object(
        input_artifacts._data_service,
        "build_daily_output_from_db",
        return_value=frame,
    ):
        artifact = input_artifacts.build_daily_input_artifact(
            scheme_id="daily_demo",
            predict_date="2026-07-24",
            start_date="2026-07-23",
            end_date="2026-07-23",
            engine=object(),
            output_root=tmp_path / "inputs",
        )

    receipt_path = audit_root / "daily.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert receipt["path"] == str(artifact.path)
    assert receipt["source"] == artifact.source
    assert receipt["data_version"] == artifact.data_version
    assert receipt["content_hash"] == artifact.content_hash
    assert receipt["date_coverage"]["end"] == "2026-07-23"
    assert receipt["metadata"]["end_date"] == "2026-07-23"
