from __future__ import annotations

import csv
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from harness.native_successor_migration import (
    _comparator_source_sha256,
    _execute_controlled_comparison,
    _validate_cutover_deployment_matrix,
    _validate_successor_execution_evidence,
    build_native_successor_equivalence_receipt,
    load_native_successor_waves,
    select_native_successor_wave,
    validate_control_plane_evidence,
)
from harness.native_successor_comparator_runner import (
    V28_ADDITIVE_MONTHLY_COLUMNS,
    _exclusive_upper_bound_after_feature,
    _run_v28,
    main as comparator_main,
)
from scheduler.discovery import load_scheme_config
from scheduler.repository import (
    NativeSuccessorBacktestEvidence,
    NativeSuccessorTarget,
    _validate_native_successor_equivalence_evidence,
    _validate_native_successor_target_configs,
    apply_native_successor_migration,
    canonical_native_successor_plan,
    native_successor_plan_sha256,
    read_native_successor_migration_plan,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _full_request(scheme_id: str) -> dict[str, str]:
    return {
        "request_id": f"{scheme_id}:2026-05-22:2026-05-22:2026-05-29",
        "predict_date": "2026-05-22",
        "feature_date": "2026-05-22",
        "target_date": "2026-05-29",
        "daily_cutoff_key": "2026-05-22",
        "weekly_cutoff_key": "202621",
        "monthly_cutoff_key": "202605",
    }


def test_locked_mapping_contains_26_to_30_identities() -> None:
    waves = load_native_successor_waves(
        PROJECT_ROOT / "deploy" / "native_to_blackbox_migration_v1.json"
    )
    targets = [target for wave in waves.values() for target in wave.targets]
    assert len({target.old_base_scheme_id for target in targets}) == 26
    assert len({target.new_base_scheme_id for target in targets}) == 30
    assert len(targets) == 30
    assert waves["W1A"].ecs_mode == "active"
    assert waves["W4A"].ecs_mode == "not_deployed_mac3_only"
    with pytest.raises(ValueError, match="requires --old-scheme-id"):
        select_native_successor_wave(waves, "W3C")
    selected = select_native_successor_wave(
        waves,
        "W3C",
        old_scheme_id="liwei_0616_5y_auc_static_all_k3_div_k10",
    )
    assert len(selected.targets) == 1


def test_t5_comparator_converts_feature_cutoff_to_exclusive_upper_bound() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2025-01-23", "2025-01-24", "2025-02-05"]
            )
        }
    )

    assert _exclusive_upper_bound_after_feature(daily, "2025-01-24") == (
        "2025-02-05"
    )
    with pytest.raises(ValueError, match="absent from daily input"):
        _exclusive_upper_bound_after_feature(daily, "2025-01-25")
    with pytest.raises(ValueError, match="one row after feature_date"):
        _exclusive_upper_bound_after_feature(daily, "2025-02-05")


@pytest.mark.parametrize(
    ("old_scheme_id", "new_scheme_id", "target_tenor"),
    (
        ("daily_5y_2_v28", "daily_5y_2_v28_bbv2", "5Y"),
        ("daily_7y_1_v28", "daily_7y_1_v28_bbv2", "7Y"),
    ),
)
def test_w2_comparator_routes_to_dedicated_v28_runner(
    tmp_path: Path,
    old_scheme_id: str,
    new_scheme_id: str,
    target_tenor: str,
) -> None:
    request = _full_request(new_scheme_id)
    requests_path = tmp_path / "requests.csv"
    with requests_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(request))
        writer.writeheader()
        writer.writerow(request)
    output = tmp_path / "result.csv"
    result = [
        {
            key: request[key]
            for key in ("request_id", "predict_date", "feature_date", "target_date")
        }
        | {"predicted_direction": 1}
    ]
    with (
        patch(
            "harness.native_successor_comparator_runner._run_v28",
            return_value=result,
        ) as run_v28,
        patch(
            "harness.native_successor_comparator_runner._run_daily",
            side_effect=AssertionError("W2 must not use the t5_daily comparator"),
        ),
    ):
        assert comparator_main(
            [
                "--old-scheme-id",
                old_scheme_id,
                "--new-scheme-id",
                new_scheme_id,
                "--target-tenor",
                target_tenor,
                "--requests",
                str(requests_path),
                "--data-dir",
                str(tmp_path),
                "--output",
                str(output),
            ]
        ) == 0
    run_v28.assert_called_once_with(
        [request], data_dir=tmp_path, old_scheme_id=old_scheme_id
    )
    assert pd.read_csv(output)["request_id"].tolist() == [request["request_id"]]


def test_w2_comparator_rejects_unapproved_identity(tmp_path: Path) -> None:
    output = tmp_path / "result.csv"
    with pytest.raises(ValueError, match="approved W1/W2 targets"):
        comparator_main(
            [
                "--old-scheme-id",
                "daily_5y_2_v28",
                "--new-scheme-id",
                "t5_daily_5y_bbv2",
                "--target-tenor",
                "5Y",
                "--requests",
                str(tmp_path / "missing.csv"),
                "--data-dir",
                str(tmp_path),
                "--output",
                str(output),
            ]
        )
    assert not output.exists()


def test_v28_comparator_batches_by_month_cutoff_and_preserves_request_order(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "generation"
    data_dir.mkdir()
    pd.DataFrame(
        {
            "date": [
                "2026-04-30",
                "2026-05-04",
                "2026-05-11",
                "2026-06-01",
                "2026-07-01",
            ],
            "factor": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    ).to_csv(data_dir / "daily_output.csv", index=False)
    pd.DataFrame(
        {
            "week_id": [202617, 202618, 202619, 202622, 202627],
            "factor": range(5),
        }
    ).to_csv(data_dir / "weekly_output.csv", index=False)
    pd.DataFrame(
        {
            "month_id": [202504, 202605, 202606, 202607],
            "factor": range(4),
            "M0041340": range(4),
            "M0041341": range(4),
            "M0041342": range(4),
        }
    ).to_csv(data_dir / "monthly_output.csv", index=False)
    pd.DataFrame(
        {
            "rdate": [
                "2026-04-30",
                "2026-05-04",
                "2026-05-11",
                "2026-06-01",
                "2026-07-01",
            ],
            "week_id": [202617, 202618, 202619, 202622, 202627],
        }
    ).to_csv(data_dir / "api_wind_date.csv", index=False)
    pd.DataFrame(
        {
            "indicators_code": ["factor"],
            "frequency": ["daily"],
            "factor_version": ["v1"],
        }
    ).to_csv(data_dir / "factor_catalog.csv", index=False)
    june = {
        "request_id": "daily_5y_2_v28_bbv2:2026-06-02:2026-06-01:2026-06-08",
        "predict_date": "2026-06-02",
        "feature_date": "2026-06-01",
        "target_date": "2026-06-08",
        "daily_cutoff_key": "2026-06-01",
        "weekly_cutoff_key": "202622",
        "monthly_cutoff_key": "202606",
    }
    may = {
        "request_id": "daily_5y_2_v28_bbv2:2026-05-05:2026-05-04:2026-05-12",
        "predict_date": "2026-05-05",
        "feature_date": "2026-05-04",
        "target_date": "2026-05-12",
        "daily_cutoff_key": "2026-05-04",
        "weekly_cutoff_key": "202618",
        "monthly_cutoff_key": "202605",
    }
    may_late = {
        "request_id": "daily_5y_2_v28_bbv2:2026-05-12:2026-05-11:2026-05-19",
        "predict_date": "2026-05-12",
        "feature_date": "2026-05-11",
        "target_date": "2026-05-19",
        "daily_cutoff_key": "2026-05-11",
        "weekly_cutoff_key": "202619",
        "monthly_cutoff_key": "202605",
    }
    calls: list[dict[str, object]] = []

    def fake_window(**kwargs) -> pd.DataFrame:
        calls.append(kwargs)
        end = str(kwargs["window_end"])
        if end == "2026-06-01":
            return pd.DataFrame(
                {"anchor_date": [end], "prediction": [1]}
            )
        return pd.DataFrame(
            {
                "anchor_date": ["2026-05-04", "2026-05-11"],
                "prediction": [-1, 0],
            }
        )

    with patch(
        "schemes.daily_5y_2_v28.inference.run_v28_for_feature_window",
        side_effect=fake_window,
    ):
        rows = _run_v28(
            [june, may_late, may],
            data_dir=data_dir,
            old_scheme_id="daily_5y_2_v28",
        )
    assert [row["request_id"] for row in rows] == [
        june["request_id"],
        may_late["request_id"],
        may["request_id"],
    ]
    assert [row["predicted_direction"] for row in rows] == [1, 0, -1]
    assert len(calls) == 2
    assert [call["window_start"] for call in calls] == [
        "2026-05-01",
        "2026-06-01",
    ]
    assert [call["window_end"] for call in calls] == [
        "2026-05-11",
        "2026-06-01",
    ]
    for call, expected_daily, expected_weekly, expected_monthly in (
        (calls[0], "2026-05-11", 202619, 202605),
        (calls[1], "2026-06-01", 202622, 202606),
    ):
        assert call["daily_df"]["date"].max() == pd.Timestamp(expected_daily)
        assert int(call["weekly_df"]["week_id"].max()) == expected_weekly
        assert int(call["monthly_df"]["month_id"].max()) == expected_monthly
        assert max(call["date_to_week"]) == expected_daily
        assert not V28_ADDITIVE_MONTHLY_COLUMNS.intersection(
            call["monthly_df"].columns
        )
        assert call["require_labels"] is False
        assert call["n_workers"] == 8
    bad_request = {**june, "weekly_cutoff_key": "202621"}
    requests_path = tmp_path / "bad-requests.csv"
    with requests_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(bad_request))
        writer.writeheader()
        writer.writerow(bad_request)
    output = tmp_path / "bad-result.csv"
    with pytest.raises(ValueError, match="cutoff is absent or inconsistent"):
        comparator_main(
            [
                "--old-scheme-id",
                "daily_5y_2_v28",
                "--new-scheme-id",
                "daily_5y_2_v28_bbv2",
                "--target-tenor",
                "5Y",
                "--requests",
                str(requests_path),
                "--data-dir",
                str(data_dir),
                "--output",
                str(output),
            ]
        )
    assert not output.exists()


def test_receipt_source_closure_includes_w2_v28_native_execution_files() -> None:
    with patch(
        "harness.native_successor_migration._sha256_file",
        return_value="a" * 64,
    ) as sha256_file:
        assert len(_comparator_source_sha256()) == 64
    paths = {
        str(call.args[0].relative_to(PROJECT_ROOT))
        for call in sha256_file.call_args_list
    }
    assert {
        "schemes/daily_5y_2_v28/inference.py",
        "schemes/daily_5y_2_v28/core/v28_common.py",
        "schemes/daily_5y_2_v28/core/data_alignment.py",
        "schemes/daily_7y_1_v28/inference.py",
        "schemes/daily_7y_1_v28/core/v28_common.py",
        "schemes/daily_7y_1_v28/core/data_alignment.py",
    } <= paths


def test_controlled_comparator_builds_zero_difference_receipt(
    tmp_path: Path,
) -> None:
    waves = load_native_successor_waves(
        PROJECT_ROOT / "deploy" / "native_to_blackbox_migration_v1.json"
    )
    target = waves["W1A"].targets[0]
    wave = SimpleNamespace(
        wave="W-test-compare",
        switch_mode="wave",
        targets=(target,),
    )
    data_dir = tmp_path / "generation"
    data_dir.mkdir()
    for filename in (
        "daily_output.csv",
        "weekly_output.csv",
        "monthly_output.csv",
        "api_wind_date.csv",
        "factor_catalog.csv",
    ):
        (data_dir / filename).write_text(f"{filename}\n", encoding="utf-8")
    result = [
        {
            "request_id": (
                f"{target.new_base_scheme_id}:2026-05-22:"
                "2026-05-22:2026-05-25"
            ),
            "predict_date": "2026-05-22",
            "feature_date": "2026-05-22",
            "target_date": "2026-05-25",
            "predicted_direction": 1,
        }
    ]
    requests_path = tmp_path / "requests.csv"
    with requests_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "request_id",
                "predict_date",
                "feature_date",
                "target_date",
                "daily_cutoff_key",
                "weekly_cutoff_key",
                "monthly_cutoff_key",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {key: result[0][key] for key in result[0] if key != "predicted_direction"}
            | {
                "daily_cutoff_key": "2026-05-22",
                "weekly_cutoff_key": "202621",
                "monthly_cutoff_key": "202605",
            }
        )
    bundle = {
        "schema_version": "native-successor-comparison-input-v1",
        "wave": wave.wave,
        "generation_id": "generation-1",
        "data_snapshot_id": "snapshot-1",
        "data_dir": str(data_dir),
        "comparisons": [
            {
                "old_base_scheme_id": target.old_base_scheme_id,
                "new_base_scheme_id": target.new_base_scheme_id,
                "target_tenor": target.target_tenor,
                "requests_path": str(requests_path),
            }
        ],
    }
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    with (
        patch(
            "harness.native_successor_migration.load_environment_fingerprint",
            return_value="e" * 64,
        ),
        patch(
            "harness.native_successor_migration._capture_native_runtime_identity",
            return_value={
                "conda_env": "forecast_env",
                "environment_fingerprint": "8" * 64,
            },
        ),
        patch(
            "harness.native_successor_migration._execute_controlled_comparison",
            return_value=(result, result),
        ),
    ):
        receipt = build_native_successor_equivalence_receipt(
            project_root=PROJECT_ROOT,
            wave=wave,
            comparison_bundle_path=bundle_path,
        )
    assert receipt["producer"]["tool"] == (
        "native-successor-controlled-comparator"
    )
    assert len(receipt["producer"]["comparator_source_sha256"]) == 64
    assert set(receipt) == {
        "schema_version", "wave", "producer", "runtime_environment_fingerprint",
        "targets",
    }
    assert receipt["targets"][0]["native_runtime_profile"] == "native"
    assert set(receipt["targets"][0]["input_identity"]) == {
        "generation_id", "data_snapshot_id", "data_files_sha256",
    }
    assert receipt["targets"][0]["request_count"] == 1
    assert receipt["targets"][0]["native_result_sha256"] == (
        receipt["targets"][0]["successor_result_sha256"]
    )


def test_controlled_comparator_rejects_direction_difference(tmp_path: Path) -> None:
    waves = load_native_successor_waves(
        PROJECT_ROOT / "deploy" / "native_to_blackbox_migration_v1.json"
    )
    target = waves["W1A"].targets[0]
    wave = SimpleNamespace(
        wave="W-test-compare",
        switch_mode="wave",
        targets=(target,),
    )
    data_dir = tmp_path / "generation"
    data_dir.mkdir()
    for filename in (
        "daily_output.csv",
        "weekly_output.csv",
        "monthly_output.csv",
        "api_wind_date.csv",
        "factor_catalog.csv",
    ):
        (data_dir / filename).write_text(filename, encoding="utf-8")
    base_result = {
        "request_id": (
            f"{target.new_base_scheme_id}:2026-05-22:2026-05-22:2026-05-25"
        ),
        "predict_date": "2026-05-22",
        "feature_date": "2026-05-22",
        "target_date": "2026-05-25",
    }
    requests_path = tmp_path / "requests.csv"
    with requests_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "request_id",
                "predict_date",
                "feature_date",
                "target_date",
                "daily_cutoff_key",
                "weekly_cutoff_key",
                "monthly_cutoff_key",
            ),
        )
        writer.writeheader()
        writer.writerow(
            base_result
            | {
                "daily_cutoff_key": "2026-05-22",
                "weekly_cutoff_key": "202621",
                "monthly_cutoff_key": "202605",
            }
        )
    native_rows = [{**base_result, "predicted_direction": -1}]
    successor_rows = [{**base_result, "predicted_direction": 1}]
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(
        json.dumps(
            {
                "schema_version": "native-successor-comparison-input-v1",
                "wave": wave.wave,
                "generation_id": "generation-1",
                "data_snapshot_id": "snapshot-1",
                "data_dir": str(data_dir),
                "comparisons": [
                    {
                        "old_base_scheme_id": target.old_base_scheme_id,
                        "new_base_scheme_id": target.new_base_scheme_id,
                        "target_tenor": target.target_tenor,
                            "requests_path": str(requests_path),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with (
        patch(
            "harness.native_successor_migration.load_environment_fingerprint",
            return_value="e" * 64,
        ),
        patch(
            "harness.native_successor_migration._capture_native_runtime_identity",
            return_value={
                "conda_env": "forecast_env",
                "environment_fingerprint": "8" * 64,
            },
        ),
        patch(
            "harness.native_successor_migration._execute_controlled_comparison",
            return_value=(native_rows, successor_rows),
        ),
        pytest.raises(ValueError, match="non-zero mismatches"),
    ):
        build_native_successor_equivalence_receipt(
            project_root=PROJECT_ROOT,
            wave=wave,
            comparison_bundle_path=bundle_path,
        )


def test_controlled_comparator_uses_offline_safety_budget(
    tmp_path: Path,
) -> None:
    requests_path = tmp_path / "requests.csv"
    requests_path.write_text("request_id\nrequest-1\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result_fields = (
        "request_id,predict_date,feature_date,target_date,"
        "predicted_direction\n"
    )
    result_row = "request-1,2026-09-04,2026-09-04,2026-09-11,1\n"

    def run_native(command: list[str], **kwargs: object) -> SimpleNamespace:
        output = Path(command[command.index("--output") + 1])
        output.write_text(result_fields + result_row, encoding="utf-8")
        assert kwargs["timeout"] == 7200
        return SimpleNamespace(returncode=0, stderr="")

    def run_successor(**kwargs: object) -> None:
        output = Path(kwargs["output_path"])
        output.parent.mkdir()
        output.write_text(
            result_fields + result_row,
            encoding="utf-8",
        )
        assert kwargs["timeout_sec"] == 7200

    target = NativeSuccessorTarget(
        old_base_scheme_id="old",
        new_base_scheme_id="new",
        task_type="T+5",
        target_tenor="5Y",
        target_rule=None,
        old_horizon=5,
        new_horizon=5,
    )
    with (
        patch(
            "harness.native_successor_migration._conda_executable",
            return_value=Path("/opt/conda/bin/conda"),
        ),
        patch(
            "harness.native_successor_migration.subprocess.run",
            side_effect=run_native,
        ),
        patch(
            "scheduler.blackbox_v2_runner.execute_blackbox_cli",
            side_effect=run_successor,
        ),
    ):
        native_rows, successor_rows = _execute_controlled_comparison(
            project_root=tmp_path,
            target=target,
            new_config=SimpleNamespace(delivery_script=tmp_path / "predict.py"),
            requests_path=requests_path,
            data_dir=data_dir,
        )

    assert native_rows == successor_rows


def test_cutover_matrix_requires_old_removed_and_successor_added(
    tmp_path: Path,
) -> None:
    wave = SimpleNamespace(
        targets=(
            NativeSuccessorTarget(
                old_base_scheme_id="old",
                new_base_scheme_id="new",
                task_type="T+1",
                target_tenor="5Y",
                target_rule=None,
                old_horizon=1,
                new_horizon=1,
            ),
        )
    )
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    matrix = deploy / "scheme_deployment_matrix_v1.json"
    matrix.write_text(
        __import__("json").dumps(
            {
                "schema_version": "scheme-deployment-matrix-v1",
                "schemes": {
                    "old": ["mac3-production", "aliyun-gray"],
                    "new": ["mac3-production", "aliyun-gray"],
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="old_still_deployed"):
        _validate_cutover_deployment_matrix(
            tmp_path,
            wave=wave,
            deployment_target="aliyun-gray",
        )
    matrix.write_text(
        __import__("json").dumps(
            {
                "schema_version": "scheme-deployment-matrix-v1",
                "schemes": {
                    "old": ["mac3-production"],
                    "new": ["mac3-production", "aliyun-gray"],
                },
            }
        ),
        encoding="utf-8",
    )
    _validate_cutover_deployment_matrix(
        tmp_path,
        wave=wave,
        deployment_target="aliyun-gray",
    )


@pytest.mark.parametrize("wave_id", ["W1A", "W1B"])
def test_w1_mapping_normalizes_native_rule_to_blackbox_task_contract(
    wave_id: str,
) -> None:
    waves = load_native_successor_waves(
        PROJECT_ROOT / "deploy" / "native_to_blackbox_migration_v1.json"
    )
    wave = waves[wave_id]
    old_ids = {target.old_base_scheme_id for target in wave.targets}
    new_ids = {target.new_base_scheme_id for target in wave.targets}
    old_configs = {
        scheme_id: load_scheme_config(
            PROJECT_ROOT / "schemes" / scheme_id / "config.yaml"
        )
        for scheme_id in old_ids
    }
    new_configs = {
        scheme_id: load_scheme_config(
            PROJECT_ROOT / "schemes" / scheme_id / "config.yaml"
        )
        for scheme_id in new_ids
    }

    _validate_native_successor_target_configs(
        wave.targets,
        old_configs=old_configs,
        new_configs=new_configs,
    )


def _fixture():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    old = SimpleNamespace(
        scheme_id="native_multi",
        scheme_version="native-v1",
        runtime_type="native_adapter",
        code_hash="a" * 64,
        config_hash="b" * 64,
        manifest_hash="c" * 64,
        task_type="weekly_point",
        target_rule="next_week_last",
        horizon=6,
        frequency="weekly",
        schedule=SimpleNamespace(
            cron="0 8 * * 5",
            timezone="Asia/Shanghai",
        ),
        tenors=["5Y"],
    )
    new = SimpleNamespace(
        scheme_id="native_multi_bbv2",
        scheme_version="bb-v1",
        runtime_type="blackbox_v2",
        algorithm_version="1.0.0",
        contract_version="2.0",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        data_snapshot_id="snapshot-1",
        code_hash="d" * 64,
        config_hash="f" * 64,
        manifest_hash="1" * 64,
        task_type="weekly_point",
        target_rule="target_week_end_yield_vs_feature_week_end_yield",
        horizon=1,
        frequency="weekly",
        schedule=SimpleNamespace(
            cron="0 8 * * 5",
            timezone="Asia/Shanghai",
        ),
        tenors=["5Y"],
    )
    target = NativeSuccessorTarget(
        old_base_scheme_id=old.scheme_id,
        new_base_scheme_id=new.scheme_id,
        task_type="weekly_point",
        target_tenor="5Y",
        target_rule="next_week_last",
        old_horizon=6,
        new_horizon=1,
    )
    evidence = NativeSuccessorBacktestEvidence(
        backtest_run_id=42,
        benchmark_id="bbv2-migration",
        data_snapshot_id="snapshot-1",
        generation_id="generation-1",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        code_hash=new.code_hash,
        config_hash=new.config_hash,
        manifest_hash=new.manifest_hash,
        validator_policy_digest="validator-1",
    )
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE t_schema_migrations ("
            "version INTEGER PRIMARY KEY, state TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO t_schema_migrations (version, state) VALUES (24, 'APPLIED')"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_versions ("
            "scheme_id TEXT NOT NULL, scheme_version TEXT NOT NULL, "
            "runtime_type TEXT, algorithm_version TEXT, contract_version TEXT, "
            "runtime_profile TEXT, environment_fingerprint TEXT, "
            "data_snapshot_id TEXT, code_hash TEXT, config_hash TEXT, "
            "manifest_hash TEXT, git_commit TEXT, status TEXT, created_by TEXT, "
            "approved_by TEXT, approved_at TEXT, "
            "PRIMARY KEY (scheme_id, scheme_version))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_registry ("
            "scheme_id TEXT PRIMARY KEY, base_scheme_id TEXT, name TEXT, owner TEXT, "
            "description TEXT, horizon INTEGER, task_type TEXT, runtime_type TEXT, "
            "tenors TEXT, frequency TEXT, target_tenor TEXT, schedule_cron TEXT, "
            "schedule_timezone TEXT, status TEXT, deployed_at TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_runs ("
            "run_id INTEGER PRIMARY KEY, scheme_id TEXT, predict_date TEXT, "
            "status TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_backtest_runs ("
            "id INTEGER PRIMARY KEY, benchmark_id TEXT, scheme_id TEXT, "
            "data_source TEXT, status TEXT, run_mode TEXT, code_hash TEXT, "
            "config_hash TEXT, input_artifact_hash TEXT, summary TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_backtest_predictions ("
            "id INTEGER PRIMARY KEY, run_id INTEGER, scheme_id TEXT, "
            "target_tenor TEXT, horizon INTEGER, predict_date TEXT, feature_date TEXT, "
            "target_date TEXT, label INTEGER, predicted_direction INTEGER, "
            "confidence REAL, source_row TEXT, extra TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE t_scheme_predictions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, "
            "backtest_run_id INTEGER, scheme_version TEXT, scheme_id TEXT, "
            "target_tenor TEXT, horizon INTEGER, predict_date TEXT, feature_date TEXT, "
            "target_date TEXT, predicted_direction INTEGER, "
            "backtest_actual_direction INTEGER, confidence REAL, "
            "model_version TEXT, extra TEXT)"
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, code_hash, config_hash, "
                "manifest_hash, status) VALUES "
                "(:scheme_id, :scheme_version, 'native_adapter', :code_hash, "
                ":config_hash, :manifest_hash, 'active')"
            ),
            vars(old),
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES "
                "('native_multi__h6__5Y', :scheme_id, 'Old display', 'owner-a', "
                "'old description', 6, 'weekly_point', 'native_adapter', '[\"5Y\"]', "
                "'weekly', '5Y', '0 8 * * 5', 'Asia/Shanghai', 'active', '2026-01-01')"
            ),
            {"scheme_id": old.scheme_id},
        )
        summary = {
            "scheme_version": new.scheme_version,
            "manifest_hash": new.manifest_hash,
            "script_validator_policy_digest": evidence.validator_policy_digest,
            "data_snapshot_id": evidence.data_snapshot_id,
            "generation_id": evidence.generation_id,
            "runtime_profile": evidence.runtime_profile,
            "environment_fingerprint": evidence.environment_fingerprint,
            "request_count": 1,
            "backtest_start_date": "2025-01-01",
            "target_date_before": "2026-06-01",
            "actual_predict_date_min": "2026-05-22",
            "actual_predict_date_max": "2026-05-22",
            "actual_target_date_min": "2026-05-29",
            "actual_target_date_max": "2026-05-29",
        }
        conn.execute(
            text(
                "INSERT INTO t_backtest_runs VALUES "
                "(42, :benchmark_id, :scheme_id, "
                "'blackbox_v2_current_snapshot_as_of', 'success', 'persist', "
                ":code_hash, :config_hash, :input_hash, :summary)"
            ),
            {
                "benchmark_id": evidence.benchmark_id,
                "scheme_id": new.scheme_id,
                "code_hash": new.code_hash,
                "config_hash": new.config_hash,
                "input_hash": evidence.data_snapshot_id,
                "summary": __import__("json").dumps(summary),
            },
        )
        conn.execute(
            text(
                "INSERT INTO t_backtest_predictions "
                "(id, run_id, scheme_id, target_tenor, horizon, predict_date, "
                "feature_date, target_date, label, predicted_direction, confidence, "
                "source_row, extra) VALUES "
                "(1, 42, :scheme_id, '5Y', 1, '2026-05-22', '2026-05-22', "
                "'2026-05-29', 1, -1, NULL, :source_row, '{}')"
            ),
            {
                "scheme_id": new.scheme_id,
                "source_row": json.dumps(
                    _full_request(new.scheme_id) | {"actual": {}}
                ),
            },
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_predictions "
                "(run_id, backtest_run_id, scheme_version, scheme_id, "
                "target_tenor, horizon, "
                "predict_date, feature_date, target_date, predicted_direction, "
                "model_version, extra) VALUES "
                "(NULL, 99, :scheme_version, :scheme_id, '5Y', 6, "
                "'2026-05-22', '2026-05-22', '2026-05-29', 1, "
                ":scheme_version, '{}')"
            ),
            {
                "scheme_id": old.scheme_id,
                "scheme_version": old.scheme_version,
            },
        )
    return engine, old, new, target, evidence


def _sqlite_upsert(conn, cfg, *, trusted_status, approved_by, approved_at):
    conn.execute(
        text(
            "INSERT INTO t_scheme_versions "
            "(scheme_id, scheme_version, runtime_type, algorithm_version, "
            "contract_version, runtime_profile, environment_fingerprint, "
            "data_snapshot_id, code_hash, config_hash, manifest_hash, status, "
            "created_by, approved_by, approved_at) VALUES "
            "(:scheme_id, :scheme_version, :runtime_type, :algorithm_version, "
            ":contract_version, :runtime_profile, :environment_fingerprint, "
            ":data_snapshot_id, :code_hash, :config_hash, :manifest_hash, :status, "
            "'test', :approved_by, :approved_at) "
            "ON CONFLICT(scheme_id, scheme_version) DO UPDATE SET "
            "status=excluded.status, approved_by=excluded.approved_by, "
            "approved_at=excluded.approved_at"
        ),
        {
            **vars(cfg),
            "status": trusted_status,
            "approved_by": approved_by,
            "approved_at": approved_at.isoformat(),
        },
    )
    return cfg.scheme_version


def _inputs(old, new, target, evidence):
    control_plane_evidence = {
            "schema_version": "native-successor-control-plane-evidence-v1",
            "wave": "W-test",
            "deployment_target": "aliyun-gray",
            "host": {
                "hostname": "isolated-test",
                "current_link": "/opt/bond-factor-lab/current",
                "release_root": "/opt/bond-factor-lab/releases/test",
            },
            "release": {
                "current_commit": "a" * 40,
                "archive_sha256": "b" * 64,
                "install_record_sha256": "c" * 64,
                "comparator_source_sha256": "6" * 64,
            },
            "databridge": {
                "generation_id": "generation-1",
                "data_snapshot_id": "snapshot-1",
                "business_digest": "d" * 64,
                "files": {
                    filename: str(index) * 64
                    for index, filename in enumerate(
                        (
                            "daily_output.csv",
                            "weekly_output.csv",
                            "monthly_output.csv",
                            "api_wind_date.csv",
                            "factor_catalog.csv",
                        ),
                        start=1,
                    )
                },
            },
            "native_runtime": {
                "conda_env": "forecast_env",
                "environment_fingerprint": "8" * 64,
            },
            "scheduler": {
                "control_plane": "systemd_one_shot",
                "cadence": "weekly",
                "timer_fenced": True,
                "unique_writer": True,
                "installed_unit_hash": "e" * 64,
                "observed_state_sha256": "a" * 64,
            },
            "dashboard": {
                "active_scheme_set_sha256": "f" * 64,
                "response_sha256": "1" * 64,
            },
            "captured_at": "2026-09-05T00:00:00Z",
            "verified_by": "native-successor-controlled-capture-v1",
        }
    control_plane_evidence["_capture_sha256"] = hashlib.sha256(
        json.dumps(
            control_plane_evidence,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    full_request = _full_request(target.new_base_scheme_id)
    request = {
        key: full_request[key]
        for key in ("request_id", "predict_date", "feature_date", "target_date")
    }
    request_sha256 = hashlib.sha256(
        canonical_native_successor_plan({"requests": [request]}).encode("utf-8")
    ).hexdigest()
    result_sha256 = hashlib.sha256(
        canonical_native_successor_plan(
            {"results": [{**request, "predicted_direction": -1}]}
        ).encode("utf-8")
    ).hexdigest()
    equivalence_evidence = {
        "schema_version": "native-successor-equivalence-v2",
        "wave": "W-test",
        "producer": {
            "tool": "native-successor-controlled-comparator",
            "tool_version": "2",
            "comparator_source_sha256": "6" * 64,
            "generated_at": "2026-09-05T00:00:00Z",
        },
        "runtime_environment_fingerprint": evidence.environment_fingerprint,
        "targets": [
            {
                "old_base_scheme_id": target.old_base_scheme_id,
                "new_base_scheme_id": target.new_base_scheme_id,
                "task_type": target.task_type,
                "target_tenor": target.target_tenor,
                "old_horizon": target.old_horizon,
                "new_horizon": target.new_horizon,
                "old_code_hash": old.code_hash,
                "new_code_hash": new.code_hash,
                "input_identity": {
                    "generation_id": evidence.generation_id,
                    "data_snapshot_id": evidence.data_snapshot_id,
                    "data_files_sha256": dict(
                        control_plane_evidence["databridge"]["files"]
                    ),
                },
                "native_runtime_profile": "native",
                "native_runtime_environment_fingerprint": "8" * 64,
                "request_artifact_sha256": hashlib.sha256(
                    canonical_native_successor_plan(
                        {"requests": [full_request]}
                    ).encode("utf-8")
                ).hexdigest(),
                "request_sha256": request_sha256,
                "request_count": 1,
                "native_result_sha256": result_sha256,
                "successor_result_sha256": result_sha256,
                "request_id_mismatch_count": 0,
                "predict_date_mismatch_count": 0,
                "feature_date_mismatch_count": 0,
                "target_date_mismatch_count": 0,
                "direction_mismatch_count": 0,
            }
        ],
    }
    return {
        "wave": "W-test",
        "targets": (target,),
        "old_configs": {old.scheme_id: old},
        "new_configs": {new.scheme_id: new},
        "backtests": {new.scheme_id: evidence},
        "equivalence_evidence": equivalence_evidence,
        "control_plane_evidence": control_plane_evidence,
    }


def _apply_inputs(inputs, *, recaptured=None):
    payload = dict(inputs)
    control_plane = payload.pop("control_plane_evidence")
    payload["control_plane_evidence_reader"] = lambda: (
        recaptured if recaptured is not None else control_plane
    )
    return payload


def _mixed_input_fixture():
    engine, old, new, target, evidence = _fixture()
    old.tenors = ["5Y", "10Y"]
    other = deepcopy(new)
    other.scheme_id = "native_multi_10y_bbv2"
    other.tenors = ["10Y"]
    other_target = replace(
        target, new_base_scheme_id=other.scheme_id, target_tenor="10Y",
    )
    other_evidence = replace(
        evidence, backtest_run_id=43, benchmark_id="bbv2-migration-10y",
    )
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO t_scheme_registry SELECT "
            "'native_multi__h6__10Y', base_scheme_id, name, owner, description, "
            "horizon, task_type, runtime_type, '[\"10Y\"]', frequency, '10Y', "
            "schedule_cron, schedule_timezone, status, deployed_at "
            "FROM t_scheme_registry WHERE target_tenor='5Y'"
        ))
        conn.execute(text(
            "INSERT INTO t_backtest_runs SELECT 43, :benchmark_id, :scheme_id, "
            "data_source, status, run_mode, code_hash, config_hash, "
            "input_artifact_hash, summary FROM t_backtest_runs WHERE id=42"
        ), {"benchmark_id": other_evidence.benchmark_id, "scheme_id": other.scheme_id})
        conn.execute(text(
            "INSERT INTO t_backtest_predictions SELECT 2, 43, :scheme_id, '10Y', "
            "horizon, predict_date, feature_date, target_date, label, "
            "predicted_direction, confidence, :source_row, extra "
            "FROM t_backtest_predictions WHERE id=1"
        ), {
            "scheme_id": other.scheme_id,
            "source_row": json.dumps(_full_request(other.scheme_id) | {"actual": {}}),
        })
        conn.execute(text(
            "INSERT INTO t_scheme_predictions SELECT NULL, run_id, backtest_run_id, "
            "scheme_version, scheme_id, '10Y', horizon, predict_date, feature_date, "
            "target_date, predicted_direction, backtest_actual_direction, confidence, "
            "model_version, extra FROM t_scheme_predictions WHERE target_tenor='5Y'"
        ))
    inputs = _inputs(old, new, target, evidence)
    other_inputs = _inputs(old, other, other_target, other_evidence)
    inputs["targets"] = (target, other_target)
    inputs["new_configs"].update(other_inputs["new_configs"])
    inputs["backtests"].update(other_inputs["backtests"])
    inputs["equivalence_evidence"]["targets"].extend(
        other_inputs["equivalence_evidence"]["targets"]
    )
    frozen = inputs["equivalence_evidence"]["targets"][0]
    frozen["input_identity"]["generation_id"] = "frozen-algorithm-generation"
    frozen["input_identity"]["data_snapshot_id"] = "frozen-algorithm-snapshot"
    frozen["input_identity"]["data_files_sha256"]["daily_output.csv"] = "a" * 64
    frozen["request_artifact_sha256"] = "b" * 64
    frozen["native_result_sha256"] = frozen["successor_result_sha256"] = "c" * 64
    return engine, inputs


def _rehash_control_plane(control_plane):
    control_plane.pop("_capture_sha256", None)
    control_plane["_capture_sha256"] = hashlib.sha256(
        json.dumps(
            control_plane,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return control_plane


def test_mysql_preflight_uses_read_only_repeatable_read_snapshot() -> None:
    connection = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = connection
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"),
        connect=MagicMock(return_value=context),
    )
    with patch(
        "scheduler.repository._native_successor_migration_plan_conn",
        return_value={"schema_version": "test"},
    ):
        plan = read_native_successor_migration_plan(
            engine,
            wave="W-test",
            targets=(),
            old_configs={},
            new_configs={},
            backtests={},
            equivalence_evidence={},
            control_plane_evidence={"bound": True},
        )
    assert plan == {"schema_version": "test"}
    assert [call.args[0] for call in connection.exec_driver_sql.call_args_list] == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
    ]
    connection.rollback.assert_called_once_with()


def test_control_plane_evidence_requires_fenced_matching_cadence() -> None:
    engine, old, new, target, evidence = _fixture()
    del engine
    control = _inputs(
        old,
        new,
        target,
        evidence,
    )["control_plane_evidence"]
    wave = SimpleNamespace(wave="W-test", cadence="weekly", ecs_mode="active")
    now = datetime(2026, 9, 5, 0, 10, tzinfo=timezone.utc)
    assert validate_control_plane_evidence(control, wave=wave, now=now) == control
    invalid = __import__("copy").deepcopy(control)
    invalid["scheduler"]["timer_fenced"] = False
    _rehash_control_plane(invalid)
    with pytest.raises(ValueError, match="timer to be fenced"):
        validate_control_plane_evidence(invalid, wave=wave, now=now)
    with pytest.raises(ValueError, match="older than 15 minutes"):
        validate_control_plane_evidence(
            control,
            wave=wave,
            now=datetime(2026, 9, 5, 0, 16, tzinfo=timezone.utc),
        )
    future = __import__("copy").deepcopy(control)
    future["captured_at"] = "2026-09-05T00:11:00Z"
    _rehash_control_plane(future)
    with pytest.raises(ValueError, match="must not be in the future"):
        validate_control_plane_evidence(future, wave=wave, now=now)


def test_successor_backtest_environment_must_match_current_manifest() -> None:
    cfg = SimpleNamespace(
        scheme_id="candidate_bbv2",
        runtime_profile="blackbox-v2-v1",
    )
    passed = SimpleNamespace(
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="a" * 64,
    )
    with pytest.raises(RuntimeError, match="environment fingerprint mismatch"):
        _validate_successor_execution_evidence(
            cfg,
            passed,
            current_environment_fingerprint="b" * 64,
        )


def test_cutover_rollback_and_recutover_reuse_exact_facts() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    plan = read_native_successor_migration_plan(engine, **inputs)
    assert plan["action"] == "cutover"
    assert set(plan["database"]) == {
        "database_identity_sha256",
        "schema_migration_version",
    }
    assert plan["database"]["schema_migration_version"] == 24
    digest = native_successor_plan_sha256(plan)
    recaptured = __import__("copy").deepcopy(inputs["control_plane_evidence"])
    recaptured["captured_at"] = "2026-09-05T00:00:01Z"
    _rehash_control_plane(recaptured)
    with patch(
        "scheduler.repository._upsert_scheme_version_conn",
        side_effect=_sqlite_upsert,
    ):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=digest,
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs, recaptured=recaptured),
        )
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT status FROM t_scheme_versions WHERE scheme_id='native_multi'")
        ).scalar_one() == "retired"
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == 1
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi'"
            )
        ).scalar_one() == 1
        successor = conn.execute(
            text(
                "SELECT name, owner, horizon, runtime_type, status "
                "FROM t_scheme_registry WHERE base_scheme_id='native_multi_bbv2'"
            )
        ).one()
        assert tuple(successor) == (
            "Old display",
            "owner-a",
            1,
            "blackbox_v2",
            "active",
        )

    inputs["control_plane_evidence"]["databridge"][
        "generation_id"
    ] = "generation-2"
    inputs["control_plane_evidence"]["databridge"][
        "data_snapshot_id"
    ] = "snapshot-2"
    rollback_plan = read_native_successor_migration_plan(engine, **inputs)
    assert rollback_plan["action"] == "rollback"
    with patch(
        "scheduler.repository._upsert_scheme_version_conn",
        side_effect=_sqlite_upsert,
    ):
        apply_native_successor_migration(
            engine,
            action="rollback",
            expected_plan_sha256=native_successor_plan_sha256(rollback_plan),
            approved_by="operator-b",
            approved_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    with engine.begin() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == 1
        assert conn.execute(
            text(
                "SELECT status FROM t_scheme_registry "
                "WHERE base_scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == "archived"

    retry_plan = read_native_successor_migration_plan(engine, **inputs)
    with patch(
        "scheduler.repository._upsert_scheme_version_conn",
        side_effect=_sqlite_upsert,
    ):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(retry_plan),
            approved_by="operator-c",
            approved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    with engine.begin() as conn:
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == 1


def test_stale_plan_hash_and_mid_transaction_failure_leave_state_unchanged() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    with pytest.raises(RuntimeError, match="plan changed"):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256="0" * 64,
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    plan = read_native_successor_migration_plan(engine, **inputs)
    with (
        patch(
            "scheduler.repository._upsert_scheme_version_conn",
            side_effect=_sqlite_upsert,
        ),
        patch(
            "scheduler.repository._upsert_successor_registry_from_old_conn",
            side_effect=RuntimeError("forced failure"),
        ),
        pytest.raises(RuntimeError, match="forced failure"),
    ):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(plan),
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    with engine.begin() as conn:
        assert conn.execute(
            text("SELECT status FROM t_scheme_versions WHERE scheme_id='native_multi'")
        ).scalar_one() == "active"
        assert conn.execute(
            text("SELECT COUNT(*) FROM t_scheme_versions WHERE scheme_id='native_multi_bbv2'")
        ).scalar_one() == 0
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi_bbv2'"
            )
        ).scalar_one() == 0
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM t_scheme_predictions "
                "WHERE scheme_id='native_multi'"
            )
        ).scalar_one() == 1


def test_cutover_preserves_historical_active_native_version_rows() -> None:
    engine, old, new, target, evidence = _fixture()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, code_hash, config_hash, "
                "manifest_hash, status) VALUES "
                "(:scheme_id, 'historical-native-v0', 'native_adapter', "
                "'old-code', 'old-config', NULL, 'active')"
            ),
            {"scheme_id": old.scheme_id},
        )
    inputs = _inputs(old, new, target, evidence)
    plan = read_native_successor_migration_plan(engine, **inputs)
    with patch(
        "scheduler.repository._upsert_scheme_version_conn",
        side_effect=_sqlite_upsert,
    ):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(plan),
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )
    with engine.begin() as conn:
        statuses = dict(
            conn.execute(
                text(
                    "SELECT scheme_version, status FROM t_scheme_versions "
                    "WHERE scheme_id = :scheme_id"
                ),
                {"scheme_id": old.scheme_id},
            ).all()
        )
    assert statuses == {
        "historical-native-v0": "active",
        old.scheme_version: "retired",
    }


def test_preflight_rejects_other_active_successor_version() -> None:
    engine, old, new, target, evidence = _fixture()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, code_hash, config_hash, "
                "manifest_hash, status) VALUES "
                "(:scheme_id, 'other-bb-version', 'blackbox_v2', "
                "'other-code', 'other-config', 'other-manifest', 'active')"
            ),
            {"scheme_id": new.scheme_id},
        )
    with pytest.raises(RuntimeError, match="unexpected active exact version"):
        read_native_successor_migration_plan(
            engine,
            **_inputs(old, new, target, evidence),
        )


def test_registry_schedule_drift_blocks_cutover() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    plan = read_native_successor_migration_plan(engine, **inputs)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE t_scheme_registry SET schedule_cron = '5 8 * * 5' "
                "WHERE base_scheme_id = :scheme_id"
            ),
            {"scheme_id": old.scheme_id},
        )
    with pytest.raises(RuntimeError, match="Registry identity mismatch"):
        apply_native_successor_migration(
            engine,
            action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(plan),
            approved_by="operator-a",
            approved_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            **_apply_inputs(inputs),
        )


@pytest.mark.parametrize(
    ("version", "state"),
    [(23, "APPLIED"), (24, "APPLYING"), (25, "APPLIED")],
)
def test_preflight_rejects_non_024_or_incomplete_schema(
    version: int,
    state: str,
) -> None:
    engine, old, new, target, evidence = _fixture()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM t_schema_migrations"))
        conn.execute(
            text(
                "INSERT INTO t_schema_migrations (version, state) "
                "VALUES (:version, :state)"
            ),
            {"version": version, "state": state},
        )
    with pytest.raises(RuntimeError, match="schema migration 024"):
        read_native_successor_migration_plan(
            engine,
            **_inputs(old, new, target, evidence),
        )


def test_preflight_binds_backtest_to_current_databridge_identity() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["control_plane_evidence"]["databridge"][
        "generation_id"
    ] = "different-generation"
    with pytest.raises(RuntimeError, match="DataBridge identity differs"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_requires_zero_difference_equivalence_receipt() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["equivalence_evidence"]["targets"][0][
        "direction_mismatch_count"
    ] = 1
    with pytest.raises(RuntimeError, match="mismatch count is non-zero"):
        read_native_successor_migration_plan(engine, **inputs)


def test_mixed_equivalence_inputs_survive_atomic_cutover_rollback_and_recutover() -> None:
    engine, inputs = _mixed_input_fixture()
    receipt = inputs["equivalence_evidence"]
    plan = read_native_successor_migration_plan(engine, **inputs)
    assert {item["target_tenor"]: item for item in plan["equivalence"]["targets"]} == {
        item["target_tenor"]: item for item in receipt["targets"]
    }
    assert {item["generation_id"] for item in plan["backtests"]} == {"generation-1"}
    approved_digest = native_successor_plan_sha256(plan)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE t_backtest_predictions SET source_row=:source_row WHERE run_id=42"),
            {"source_row": json.dumps(_full_request("native_multi_bbv2") | {
                "weekly_cutoff_key": "202620", "actual": {},
            })},
        )
    changed_plan = read_native_successor_migration_plan(engine, **inputs)
    assert native_successor_plan_sha256(changed_plan) != approved_digest
    with pytest.raises(RuntimeError, match="plan.*(changed|mismatch|differs)"):
        apply_native_successor_migration(
            engine, **_apply_inputs(inputs), action="cutover",
            expected_plan_sha256=approved_digest, approved_by="test-operator",
            approved_at=datetime.now(timezone.utc),
        )
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_registry WHERE status='active'"
        )).scalar_one() == 2
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_predictions WHERE scheme_id='native_multi_bbv2'"
        )).scalar_one() == 0
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER reject_second_successor BEFORE INSERT ON t_scheme_registry "
            "WHEN NEW.runtime_type='blackbox_v2' AND NEW.target_tenor='10Y' "
            "BEGIN SELECT RAISE(ABORT, 'forced second target failure'); END"
        )
    with (
        patch("scheduler.repository._upsert_scheme_version_conn", side_effect=_sqlite_upsert),
        pytest.raises(IntegrityError, match="forced second target failure"),
    ):
        apply_native_successor_migration(
            engine, **_apply_inputs(inputs), action="cutover",
            expected_plan_sha256=native_successor_plan_sha256(changed_plan),
            approved_by="test-operator", approved_at=datetime.now(timezone.utc),
        )
    with engine.begin() as conn:
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_registry WHERE status='active'"
        )).scalar_one() == 2
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_versions WHERE runtime_type='blackbox_v2'"
        )).scalar_one() == 0
        assert conn.execute(text(
            "SELECT COUNT(*) FROM t_scheme_predictions WHERE scheme_id<>'native_multi'"
        )).scalar_one() == 0
        conn.exec_driver_sql("DROP TRIGGER reject_second_successor")
    for action in ("cutover", "rollback", "cutover"):
        plan = read_native_successor_migration_plan(engine, **inputs)
        assert plan["action"] == action
        with patch(
            "scheduler.repository._upsert_scheme_version_conn",
            side_effect=_sqlite_upsert,
        ):
            apply_native_successor_migration(
                engine,
                **_apply_inputs(inputs),
                action=action,
                expected_plan_sha256=native_successor_plan_sha256(plan),
                approved_by="test-operator",
                approved_at=datetime.now(timezone.utc),
            )
        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT runtime_type FROM t_scheme_registry WHERE status='active'"
            )).scalars().all() == [
                "native_adapter" if action == "rollback" else "blackbox_v2"
            ] * 2
            assert conn.execute(text(
                "SELECT scheme_id, target_tenor, predicted_direction "
                "FROM t_scheme_predictions ORDER BY scheme_id, target_tenor"
            )).all() == [
                ("native_multi", "10Y", 1),
                ("native_multi", "5Y", 1),
                ("native_multi_10y_bbv2", "10Y", -1),
                ("native_multi_bbv2", "5Y", -1),
            ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("direction", "standardized result digests"),
        ("cutoff", "complete Request digest"),
        ("file", "differs from current DataBridge"),
        ("input_missing", "target fields"),
        ("input_field_missing", "input_identity fields"),
        ("input_file_missing", "all five DataBridge files"),
        ("profile_missing", "target fields"),
        ("profile_invalid", "runtime profile is invalid"),
        ("profile_fingerprint", "Native runtime differs"),
        ("profile_outside_family", "complete locked W3A family"),
        ("legacy_top_input", "evidence fields"),
    ],
)
def test_mixed_equivalence_inputs_fail_closed_per_target(
    change: str, message: str,
) -> None:
    engine, inputs = _mixed_input_fixture()
    receipt = inputs["equivalence_evidence"]
    same_input = receipt["targets"][1]
    if change == "direction":
        same_input["native_result_sha256"] = "a" * 64
        same_input["successor_result_sha256"] = "a" * 64
    elif change == "cutoff":
        same_input["request_artifact_sha256"] = "b" * 64
    elif change == "file":
        same_input["input_identity"]["data_files_sha256"]["daily_output.csv"] = "a" * 64
    elif change == "input_missing":
        same_input.pop("input_identity")
    elif change == "input_field_missing":
        same_input["input_identity"].pop("data_snapshot_id")
    elif change == "input_file_missing":
        same_input["input_identity"]["data_files_sha256"].pop("daily_output.csv")
    elif change == "profile_missing":
        same_input.pop("native_runtime_profile")
    elif change == "profile_invalid":
        same_input["native_runtime_profile"] = "forecast_env"
    elif change == "profile_fingerprint":
        receipt["targets"][0]["native_runtime_environment_fingerprint"] = "e" * 64
    elif change == "profile_outside_family":
        same_input["native_runtime_profile"] = "blackbox-v2-v1"
        same_input["native_runtime_environment_fingerprint"] = "e" * 64
    elif change == "legacy_top_input":
        receipt.update(same_input["input_identity"])
    with pytest.raises(RuntimeError, match=message):
        read_native_successor_migration_plan(engine, **inputs)


@pytest.mark.parametrize("target_index", [0, 1])
@pytest.mark.parametrize(
    "change", ["generation", "snapshot", "file", "environment"],
)
def test_plan_authorization_binds_each_target_input_and_environment(
    target_index: int, change: str,
) -> None:
    engine, inputs = _mixed_input_fixture()
    approved_plan = read_native_successor_migration_plan(engine, **inputs)
    approved_digest = native_successor_plan_sha256(approved_plan)
    item = inputs["equivalence_evidence"]["targets"][target_index]
    if change == "generation":
        item["input_identity"]["generation_id"] = "other-generation"
    elif change == "snapshot":
        item["input_identity"]["data_snapshot_id"] = "other-snapshot"
    elif change == "file":
        item["input_identity"]["data_files_sha256"]["daily_output.csv"] = "f" * 64
    elif change == "environment":
        item["native_runtime_environment_fingerprint"] = "9" * 64
    # 同输入文件或不匹配环境会先拒绝；其余合法变化必须改变授权摘要。
    if change == "environment" or (change == "file" and target_index == 1):
        message = "Native runtime differs|differs from current DataBridge"
    else:
        changed_plan = read_native_successor_migration_plan(engine, **inputs)
        assert native_successor_plan_sha256(changed_plan) != approved_digest
        message = "plan changed"
    with pytest.raises(RuntimeError, match=message):
        apply_native_successor_migration(
            engine, **_apply_inputs(inputs), action="cutover",
            expected_plan_sha256=approved_digest, approved_by="test-operator",
            approved_at=datetime.now(timezone.utc),
        )


@pytest.mark.parametrize("scope_change", [None, "wave", "partial", "target", "environment"])
def test_reviewed_reference_runtime_is_bound_to_locked_family(scope_change: str | None) -> None:
    targets = load_native_successor_waves(
        PROJECT_ROOT / "deploy" / "native_to_blackbox_migration_v1.json"
    )["W3A"].targets
    engine, old_template, new_template, _, backtest = _fixture()
    inputs = None
    fact_rows = {}
    for target in targets:
        old = SimpleNamespace(**(vars(old_template) | {
            "scheme_id": target.old_base_scheme_id,
        }))
        new = SimpleNamespace(**(vars(new_template) | {
            "scheme_id": target.new_base_scheme_id,
        }))
        item_inputs = _inputs(old, new, target, backtest)
        if inputs is None:
            inputs = item_inputs
        else:
            for key in ("old_configs", "new_configs", "backtests"):
                inputs[key].update(item_inputs[key])
            inputs["equivalence_evidence"]["targets"].extend(
                item_inputs["equivalence_evidence"]["targets"]
            )
        full_request = _full_request(new.scheme_id)
        fact_rows[new.scheme_id] = [{
            **full_request, "_source_request": full_request,
            "target_tenor": target.target_tenor, "horizon": target.new_horizon,
            "predicted_direction": -1,
        }]
    engine.dispose()
    assert inputs is not None
    inputs["targets"] = targets
    inputs["wave"] = inputs["equivalence_evidence"]["wave"] = "W3A"
    evidence = inputs.pop("equivalence_evidence")
    for item in evidence["targets"]:
        item["native_runtime_profile"] = "blackbox-v2-v1"
        item["native_runtime_environment_fingerprint"] = "e" * 64
    inputs.update(successor_fact_rows=fact_rows, require_current_databridge=True)
    if scope_change == "wave":
        inputs["wave"] = evidence["wave"] = "W3B"
    elif scope_change == "partial":
        inputs["targets"] = targets[:1]
        evidence["targets"] = evidence["targets"][:1]
    elif scope_change == "target":
        inputs["targets"] = (replace(targets[0], new_horizon=1), targets[1])
        evidence["targets"][0]["new_horizon"] = 1
    elif scope_change == "environment":
        evidence["targets"][0]["native_runtime_environment_fingerprint"] = "8" * 64
    if scope_change is not None:
        message = (
            "Native runtime differs" if scope_change == "environment"
            else "complete locked W3A family"
        )
        with pytest.raises(RuntimeError, match=message):
            _validate_native_successor_equivalence_evidence(evidence, **inputs)
        return
    normalized = _validate_native_successor_equivalence_evidence(evidence, **inputs)
    approved_digest = native_successor_plan_sha256({"equivalence": normalized})
    for item in evidence["targets"]:
        item["native_runtime_profile"] = "native"
        item["native_runtime_environment_fingerprint"] = "8" * 64
        normalized = _validate_native_successor_equivalence_evidence(evidence, **inputs)
        changed_digest = native_successor_plan_sha256({"equivalence": normalized})
        assert changed_digest != approved_digest
        approved_digest = changed_digest


def test_distinct_input_does_not_allow_native_successor_result_mismatch() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    item = inputs["equivalence_evidence"]["targets"][0]
    item["input_identity"]["generation_id"] = "frozen-algorithm-generation"
    item["input_identity"]["data_snapshot_id"] = "frozen-algorithm-snapshot"
    item["native_result_sha256"] = "c" * 64
    with pytest.raises(RuntimeError, match="standardized result digests"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_binds_equivalence_to_complete_persisted_fact_set() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["equivalence_evidence"]["targets"][0]["request_count"] = 2
    with pytest.raises(RuntimeError, match="persisted backtest facts"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_binds_all_request_cutoffs_to_persisted_source_row() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    changed = _full_request(new.scheme_id) | {
        "weekly_cutoff_key": "202620",
        "actual": {},
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE t_backtest_predictions SET source_row = :source_row "
                "WHERE run_id = 42"
            ),
            {"source_row": json.dumps(changed)},
        )
    with pytest.raises(RuntimeError, match="complete Request digest"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_recomputes_standardized_result_digest() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["equivalence_evidence"]["targets"][0][
        "native_result_sha256"
    ] = "7" * 64
    inputs["equivalence_evidence"]["targets"][0][
        "successor_result_sha256"
    ] = "7" * 64
    with pytest.raises(RuntimeError, match="standardized result digests"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_binds_comparator_source_to_current_release() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["equivalence_evidence"]["producer"][
        "comparator_source_sha256"
    ] = "b" * 64
    with pytest.raises(RuntimeError, match="producer identity mismatch"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_binds_native_environment_to_current_control_plane() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    inputs["control_plane_evidence"]["native_runtime"][
        "environment_fingerprint"
    ] = "9" * 64
    with pytest.raises(RuntimeError, match="Native runtime differs"):
        read_native_successor_migration_plan(engine, **inputs)


def test_first_cutover_binds_equivalence_to_current_five_files() -> None:
    engine, old, new, target, evidence = _fixture()
    inputs = _inputs(old, new, target, evidence)
    item = inputs["equivalence_evidence"]["targets"][0]
    item["input_identity"]["data_files_sha256"]["daily_output.csv"] = "a" * 64
    with pytest.raises(RuntimeError, match="differs from current DataBridge"):
        read_native_successor_migration_plan(engine, **inputs)


def test_preflight_rejects_backtest_rows_in_gray_live_range() -> None:
    engine, old, new, target, evidence = _fixture()
    full_request = _full_request(target.new_base_scheme_id) | {
        "request_id": (
            f"{target.new_base_scheme_id}:2026-05-22:2026-05-22:2026-06-01"
        ),
        "target_date": "2026-06-01",
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE t_backtest_predictions SET target_date = '2026-06-01', "
                "source_row = :source_row "
                "WHERE run_id = 42"
            ),
            {"source_row": json.dumps(full_request | {"actual": {}})},
        )
        raw = conn.execute(
            text("SELECT summary FROM t_backtest_runs WHERE id = 42")
        ).scalar_one()
        summary = json.loads(raw)
        summary["actual_target_date_min"] = "2026-06-01"
        summary["actual_target_date_max"] = "2026-06-01"
        conn.execute(
            text("UPDATE t_backtest_runs SET summary = :summary WHERE id = 42"),
            {"summary": json.dumps(summary)},
        )
    with pytest.raises(RuntimeError, match="crosses gray-live target boundary"):
        read_native_successor_migration_plan(
            engine,
            **_inputs(old, new, target, evidence),
        )


def test_preflight_records_historical_grid_date_drift() -> None:
    engine, old, new, target, evidence = _fixture()
    full_request = _full_request(target.new_base_scheme_id) | {
        "request_id": (
            f"{target.new_base_scheme_id}:2026-05-22:2026-05-21:2026-05-29"
        ),
        "feature_date": "2026-05-21",
        "daily_cutoff_key": "2026-05-21",
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE t_backtest_predictions SET feature_date = '2026-05-21', "
                "source_row = :source_row "
                "WHERE run_id = 42"
            ),
            {"source_row": json.dumps(full_request | {"actual": {}})},
        )
    inputs = _inputs(old, new, target, evidence)
    request = {
        key: full_request[key]
        for key in ("request_id", "predict_date", "feature_date", "target_date")
    }
    item = inputs["equivalence_evidence"]["targets"][0]
    item["request_artifact_sha256"] = hashlib.sha256(
        canonical_native_successor_plan(
            {"requests": [full_request]}
        ).encode("utf-8")
    ).hexdigest()
    item["request_sha256"] = hashlib.sha256(
        canonical_native_successor_plan({"requests": [request]}).encode("utf-8")
    ).hexdigest()
    result_sha256 = hashlib.sha256(
        canonical_native_successor_plan(
            {"results": [{**request, "predicted_direction": -1}]}
        ).encode("utf-8")
    ).hexdigest()
    item["native_result_sha256"] = result_sha256
    item["successor_result_sha256"] = result_sha256
    plan = read_native_successor_migration_plan(engine, **inputs)
    coverage = plan["grid_coverage"][0]
    assert coverage["old_base_scheme_id"] == old.scheme_id
    assert coverage["new_base_scheme_id"] == new.scheme_id
    assert coverage["old_date_count"] == 1
    assert coverage["new_date_count"] == 1
    assert coverage["old_date_identity_sha256"] != coverage[
        "new_date_identity_sha256"
    ]
    assert coverage["data_vintage_drift"] is True


def test_preflight_rejects_truncated_successor_backtest_summary() -> None:
    engine, old, new, target, evidence = _fixture()
    with engine.begin() as conn:
        raw = conn.execute(
            text("SELECT summary FROM t_backtest_runs WHERE id = 42")
        ).scalar_one()
        summary = json.loads(raw)
        summary["request_count"] = 2
        conn.execute(
            text("UPDATE t_backtest_runs SET summary = :summary WHERE id = 42"),
            {"summary": json.dumps(summary)},
        )
    with pytest.raises(ValueError, match="backtest coverage summary mismatch"):
        read_native_successor_migration_plan(
            engine,
            **_inputs(old, new, target, evidence),
        )
