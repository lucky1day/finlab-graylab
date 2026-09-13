from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from harness.context import GateContext
from harness.registry import gate_for_name


class BlackboxV2HarnessGateTests(unittest.TestCase):

    def test_producer_prepares_snapshot_and_schemes_only_read_receipt(self) -> None:
        from shared.blackbox_v2.snapshot import SNAPSHOT_FILENAMES
        from shared.input_artifacts import (
            get_ready_blackbox_snapshot,
            invalidate_ready_blackbox_snapshot,
            prepare_blackbox_generation_snapshot,
        )

        frames = _snapshot_frames()
        for code in ("M0041340", "M0041341", "M0041342"):
            frames["monthly_output.csv"][code] = 0.0
            frames["factor_catalog.csv"].loc[len(frames["factor_catalog.csv"])] = {
                "indicators_code": code,
                "frequency": "monthly",
                "factor_version": "V1.0",
            }
        profiles = {
            name: SimpleNamespace(
                sha256=hashlib.sha256(
                    frame.to_csv(index=False, lineterminator="\n").encode(
                        "utf-8"
                    )
                ).hexdigest(),
                business_hash=(str(index + 1) * 64)[:64],
                rows=len(frame),
                columns=len(frame.columns),
                min_key=str(frame.iloc[0, 0]),
                max_key=str(frame.iloc[-1, 0]),
            )
            for index, (name, frame) in enumerate(frames.items())
        }
        state = {
            "generation_id": "generation-shared",
            "refresh_date": "2026-07-15",
            "business_digest": "b" * 64,
            "schema_version": "data-bridge-v1",
            "files": {
                name: {
                    "sha256": profiles[name].sha256,
                    "business_hash": profiles[name].business_hash,
                    "rows": profiles[name].rows,
                    "columns": profiles[name].columns,
                    "min_key": profiles[name].min_key,
                    "max_key": profiles[name].max_key,
                }
                for name in SNAPSHOT_FILENAMES
            },
        }

        dataset = SimpleNamespace(
            schema_version="data-bridge-v1",
            business_digest="b" * 64,
            frames=frames,
            files=profiles,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_root = root / "generation-cache"
            cache_root.mkdir()
            with (
                patch(
                    "shared.input_artifacts._load_blackbox_schema",
                    return_value=(
                        "data-bridge-v1",
                        {name: list(frame.columns) for name, frame in frames.items()},
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "ready generation receipt is unavailable or invalid",
                ):
                    get_ready_blackbox_snapshot(
                        snapshot_date="2026-07-15",
                        cache_root=cache_root,
                    )
                prepared = prepare_blackbox_generation_snapshot(
                    state=state,
                    dataset=dataset,
                    cache_root=cache_root,
                )
                ready = get_ready_blackbox_snapshot(
                    snapshot_date="2026-07-15",
                    cache_root=cache_root,
                )
                expected_source_identity = {
                    "generation_id": state["generation_id"],
                    "refresh_date": state["refresh_date"],
                    "schema_version": state["schema_version"],
                    "business_digest": state["business_digest"],
                    "stable_identity_sha256": "a" * 64,
                    "files": [
                        {"filename": name, **state["files"][name]}
                        for name in sorted(SNAPSHOT_FILENAMES)
                    ],
                }
                matched = get_ready_blackbox_snapshot(
                    snapshot_date="2026-07-15",
                    cache_root=cache_root,
                    expected_source_identity=expected_source_identity,
                )
                self.assertEqual(matched.snapshot_id, ready.snapshot_id)
                with self.assertRaisesRegex(
                    ValueError,
                    "does not match planned authority",
                ):
                    get_ready_blackbox_snapshot(
                        snapshot_date="2026-07-15",
                        cache_root=cache_root,
                        expected_source_identity={
                            **expected_source_identity,
                            "generation_id": "different-generation",
                        },
                    )
                invalidate_ready_blackbox_snapshot(cache_root=cache_root)
                with self.assertRaisesRegex(
                    ValueError,
                    "ready generation receipt is unavailable or invalid",
                ):
                    get_ready_blackbox_snapshot(
                        snapshot_date="2026-07-15",
                        cache_root=cache_root,
                    )
                restored = prepare_blackbox_generation_snapshot(
                    state=state,
                    dataset=dataset,
                    cache_root=cache_root,
                )
                self.assertEqual(restored.snapshot_id, prepared.snapshot_id)

                ready_path = cache_root / "ready-generation.json"
                ready_identity = json.loads(ready_path.read_text(encoding="utf-8"))
                ready_identity["cache_schema_version"] = "retired-cache-version"
                ready_path.chmod(0o644)
                ready_path.write_text(
                    json.dumps(
                        ready_identity,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
                ready_path.chmod(0o444)
                with self.assertRaisesRegex(ValueError, "contract mismatch"):
                    get_ready_blackbox_snapshot(
                        snapshot_date="2026-07-15",
                        cache_root=cache_root,
                    )
                prepare_blackbox_generation_snapshot(
                    state=state,
                    dataset=dataset,
                    cache_root=cache_root,
                )

                damaged = prepared.data_dir / "daily_output.csv"
                damaged.chmod(0o644)
                damaged.write_bytes(damaged.read_bytes() + b"\n")
                damaged.chmod(0o444)
                with self.assertRaisesRegex(
                    ValueError,
                    "no longer matches producer seal",
                ):
                    get_ready_blackbox_snapshot(
                        snapshot_date="2026-07-15",
                        cache_root=cache_root,
                    )

        self.assertEqual(prepared.snapshot_id, ready.snapshot_id)
        self.assertEqual(ready.generation_id, "generation-shared")

    def test_runtime_view_rejects_source_changed_after_producer_seal(self) -> None:
        from shared import input_artifacts
        from shared.blackbox_v2.snapshot import (
            SNAPSHOT_FILENAMES,
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        frames = _snapshot_frames()
        expected_columns = {
            name: list(frame.columns) for name, frame in frames.items()
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns=expected_columns,
                schema_version="data-bridge-v1",
            )
            snapshot = replace(
                snapshot,
                sealed_file_fingerprints=(
                    input_artifacts._snapshot_file_fingerprints(snapshot)
                ),
            )
            bundle = compose_blackbox_input_bundle(
                snapshot,
                factor_input_mode="algorithm_managed",
            )
            with open_blackbox_runtime_view(
                bundle,
                runtime_root=root / "runtime-views",
            ) as runtime_view:
                self.assertEqual(
                    sorted(path.name for path in runtime_view.data_dir.iterdir()),
                    sorted(SNAPSHOT_FILENAMES),
                )

            damaged = snapshot.data_dir / "daily_output.csv"
            damaged.chmod(0o644)
            damaged.write_bytes(damaged.read_bytes() + b"\n")
            damaged.chmod(0o444)
            with self.assertRaisesRegex(
                ValueError,
                "source no longer matches producer seal",
            ):
                with open_blackbox_runtime_view(
                    bundle,
                    runtime_root=root / "runtime-views",
                ):
                    pass

    def test_successful_backtest_is_the_activation_evidence(self) -> None:
        from sqlalchemy import create_engine, text

        from harness.blackbox_v2.gates import verify_passed_blackbox_backtest
        from shared.blackbox_v2.intake import SCRIPT_VALIDATOR_POLICY_DIGEST

        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        summary = {
            "scheme_version": "version-test",
            "manifest_hash": "m" * 64,
            "data_snapshot_id": "snapshot-test",
            "generation_id": "generation-test",
            "runtime_profile": "blackbox-v2-v1",
            "environment_fingerprint": "e" * 64,
            "script_validator_policy_digest": SCRIPT_VALIDATOR_POLICY_DIGEST,
        }
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE t_backtest_runs ("
                "id INTEGER PRIMARY KEY, benchmark_id TEXT, scheme_id TEXT, "
                "data_source TEXT, status TEXT, run_mode TEXT, updated_at TEXT, "
                "summary TEXT, code_hash TEXT, config_hash TEXT, "
                "input_artifact_hash TEXT)"
            )
            connection.execute(
                text(
                    "INSERT INTO t_backtest_runs VALUES "
                    "(1, 'bbv2-test', 'trial_10y', "
                    "'blackbox_v2_current_snapshot_as_of', 'success', 'persist', "
                    "'2026-07-15 00:00:00', :summary, :code_hash, :config_hash, "
                    "'snapshot-test')"
                ),
                {
                    "summary": json.dumps(summary),
                    "code_hash": "c" * 64,
                    "config_hash": "f" * 64,
                },
            )

        passed = verify_passed_blackbox_backtest(
            engine,
            SimpleNamespace(
                scheme_id="trial_10y",
                scheme_version="version-test",
                code_hash="c" * 64,
                config_hash="f" * 64,
                manifest_hash="m" * 64,
            ),
        )

        self.assertEqual(passed.backtest_run_id, 1)
        self.assertEqual(passed.benchmark_id, "bbv2-test")
        self.assertEqual(passed.data_snapshot_id, "snapshot-test")
        self.assertEqual(passed.generation_id, "generation-test")

        summary["script_validator_policy_digest"] = "old-policy"
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE t_backtest_runs SET summary = :summary WHERE id = 1"),
                {"summary": json.dumps(summary)},
            )
        with self.assertRaisesRegex(ValueError, "no successful persisted"):
            verify_passed_blackbox_backtest(
                engine,
                SimpleNamespace(
                    scheme_id="trial_10y",
                    scheme_version="version-test",
                    code_hash="c" * 64,
                    config_hash="f" * 64,
                    manifest_hash="m" * 64,
                ),
            )
        engine.dispose()

    def test_backtest_preflight_reuses_intake_safety_validation(self) -> None:
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery
        from harness.blackbox_v2.gates import validate_canonical_blackbox_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            script = scheme_dir / "delivery" / "trial_10y.py"
            script.chmod(0o644)
            script.write_text("import requests\n", encoding="utf-8")
            cfg = load_scheme_config(scheme_dir / "config.yaml")

            with self.assertRaisesRegex(ValueError, "forbidden import requests"):
                validate_canonical_blackbox_delivery(cfg)

class BlackboxStateRebuildCliTests(unittest.TestCase):
    """通过最高层 CLI 验证状态维护的精确授权范围与无业务写入边界。"""

    def _arguments(self, **overrides: str | None) -> list[str]:
        values = {
            "scheme-id": "trial_10y", "predict-date": "2026-09-07",
            "expected-scheme-version": "version-exact", "approved-by": "operator-test",
            "project-root": str(Path(__file__).resolve().parents[1]),
            **overrides,
        }
        return ["rebuild-blackbox-state", *[
            part for name, value in values.items() if value is not None
            for part in (f"--{name}", value)
        ]]

    def test_required_scope_arguments_are_rejected_before_loading_or_engine(self) -> None:
        from harness.cli import main

        for missing in ("scheme-id", "predict-date", "expected-scheme-version", "approved-by"):
            with (
                self.subTest(missing=missing),
                patch("harness.blackbox_v2.state.load_scheme_config") as load,
                patch("harness.blackbox_v2.state.create_input_engine") as create_engine,
                patch("harness.blackbox_v2.state.run_blackbox_scheme_subprocess") as execute,
                redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as error:
                    main(self._arguments(**{missing: None}))
                self.assertEqual(error.exception.code, 2)
                load.assert_not_called()
                create_engine.assert_not_called()
                execute.assert_not_called()

    def test_invalid_scope_is_rejected_before_loading_or_engine(self) -> None:
        from harness.cli import main

        invalid = (
            ("scheme-id", "../trial_10y"), ("scheme-id", "/trial_10y"),
            ("scheme-id", ""), ("scheme-id", "trial/10y"),
            ("predict-date", "2026-02-30"), ("predict-date", "20260907"),
            ("predict-date", "2026-09-07T00:00:00"),
            ("approved-by", ""), ("approved-by", " \t"),
            ("approved-by", "x" * 201),
        )
        for argument, value in invalid:
            with (
                self.subTest(argument=argument, value=value),
                patch("harness.blackbox_v2.state.load_scheme_config") as load,
                patch("harness.blackbox_v2.state.create_input_engine") as create_engine,
                patch("harness.blackbox_v2.state.run_blackbox_scheme_subprocess") as execute,
                redirect_stdout(io.StringIO()) as output,
            ):
                with self.assertRaises(ValueError):
                    main(self._arguments(**{argument: value}))
                load.assert_not_called()
                create_engine.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(output.getvalue(), "")

    def test_only_matching_incremental_blackbox_version_can_create_engine(self) -> None:
        from harness.cli import main

        invalid_configs = (
            dict(runtime_type="blackbox_v2", incremental_state=True, scheme_version="other-version"),
            dict(runtime_type="native_v1", incremental_state=True, scheme_version="version-exact"),
            dict(runtime_type="blackbox_v2", incremental_state=False, scheme_version="version-exact"),
        )
        for values in invalid_configs:
            with (
                self.subTest(config=values),
                patch("harness.blackbox_v2.state.load_scheme_config", return_value=SimpleNamespace(**values)),
                patch("harness.blackbox_v2.state.create_input_engine") as create_engine,
                patch("harness.blackbox_v2.state.run_blackbox_scheme_subprocess") as execute,
                redirect_stdout(io.StringIO()) as output,
            ):
                with self.assertRaisesRegex(ValueError, "matching incremental Blackbox exact version"):
                    main(self._arguments())
                create_engine.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(output.getvalue(), "")

    def test_rebuild_uses_read_engine_and_does_not_report_prediction_write(self) -> None:
        from harness.cli import main

        cfg = SimpleNamespace(runtime_type="blackbox_v2", incremental_state=True,
                              scheme_version="version-exact")
        engine = Mock(spec_set=["dispose"])
        record = SimpleNamespace(
            feature_date="2026-09-04", target_date="2026-09-07", predicted_direction=-1,
            extra={"state_scope": "persistent", "state_output_sha256": "a" * 64,
                   "state_bytes": 17, "data_snapshot_id": "private-snapshot",
                   "predicted_direction": -1},
        )
        with (
            patch("harness.blackbox_v2.state.load_scheme_config", return_value=cfg),
            patch("harness.blackbox_v2.state.create_input_engine", return_value=engine),
            patch("harness.blackbox_v2.state.run_blackbox_scheme_subprocess", return_value=[record]),
            patch("harness.cli.create_engine_from_env", side_effect=AssertionError("business engine forbidden")),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(self._arguments()), 0)
        self.assertEqual([call[0] for call in engine.mock_calls], ["dispose"])
        self.assertIs(json.loads(output.getvalue())["prediction_written"], False)


def _delivery(path: Path, *, script: str = "import argparse\nimport json\n") -> Path:
    path.mkdir(parents=True)
    (path / "trial_10y.py").write_text(script, encoding="utf-8")
    (path / "trial_10y.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scheme_id": "trial_10y",
                "name": "10Y Trial",
                "algorithm_version": "1.0.0",
                "target_tenor": "10Y",
                "task_type": "T+1",
                "horizon": 1,
                "target_rule": "target_date_yield_vs_feature_date_yield",
                "owner": "ALGO-A",
                "description": "使用期限利差和滚动分类模型形成方向信号。",
            }
        ),
        encoding="utf-8",
    )
    return path


def _snapshot_frames() -> dict[str, pd.DataFrame]:
    return {
        "daily_output.csv": pd.DataFrame(
            {"date": ["2026-07-14", "2026-07-15", "2026-07-16"], "daily_factor": [0.0, 1.0, 2.0]}
        ),
        "weekly_output.csv": pd.DataFrame(
            {"week_id": ["202626", "202627", "202628"], "weekly_factor": [0.0, 1.0, 2.0]}
        ),
        "monthly_output.csv": pd.DataFrame(
            {"month_id": ["202605", "202606", "202607"], "monthly_factor": [0.0, 1.0, 2.0]}
        ),
        "api_wind_date.csv": pd.DataFrame(
            {
                "rdate": ["2026-07-14", "2026-07-15", "2026-07-16"],
                "week_id": ["202627", "202627", "202628"],
            }
        ),
        "factor_catalog.csv": pd.DataFrame(
            {
                "indicators_code": [
                    "daily_factor",
                    "weekly_factor",
                    "monthly_factor",
                ],
                "frequency": ["daily", "weekly", "monthly"],
                "factor_version": ["V1.0", "V1.0", "V1.0"],
            }
        ),
    }


@pytest.mark.parametrize("runtime", ["native_adapter", "unknown"])
def test_backtest_gate_rejects_non_blackbox(tmp_path, runtime) -> None:
    ctx = GateContext(
        scheme_id="trial", predict_date="2026-09-13", project_root=tmp_path,
        config=SimpleNamespace(runtime_type=runtime),
    )
    with pytest.raises(ValueError, match="Native validation gates are retired"):
        gate_for_name("backtest", ctx=ctx)


def test_direct_operation_requires_canonical_dates() -> None:
    from harness.operation import build_direct_operation

    with pytest.raises(ValueError, match="canonical YYYY-MM-DD"):
        build_direct_operation(
            "trial_10y",
            "backtest_persist",
            "2026-8-25",
            scheme_version="version-1",
            issued_by="operator",
        )
