from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext
import pandas as pd


class BlackboxV2HarnessGateTests(unittest.TestCase):
    def test_stable_private_file_reader_rejects_path_replacement_during_read(self) -> None:
        from harness.blackbox_v2.gates import (
            _read_stable_private_file,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "state.json"
            replacement = root / "replacement.json"
            target.write_bytes(b'{"old":true}\n')
            replacement.write_bytes(b'{"new":true}\n')
            real_read = os.read
            replaced = False

            def racing_read(descriptor, count):
                nonlocal replaced
                content = real_read(descriptor, count)
                if not replaced:
                    replaced = True
                    os.replace(replacement, target)
                return content

            with (
                patch(
                    "harness.blackbox_v2.gates.os.read",
                    side_effect=racing_read,
                ),
                self.assertRaisesRegex(ValueError, "changed"),
            ):
                _read_stable_private_file(
                    target,
                    label="test state",
                )

    def test_runtime_platform_file_verifier_rejects_write_symlink_and_hardlink(self) -> None:
        from harness.blackbox_v2.gates import (
            _verify_runtime_platform_files,
        )
        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from shared.input_artifacts import open_blackbox_runtime_view

        artifact = freeze_platform_input(
            "api-wind-date-v1",
            pd.DataFrame(
                {
                    "rdate": ["2026-07-15"],
                    "week_id": ["202627"],
                }
            ),
            weekly_cutoff_key="202627",
        )
        for mutation in ("writable", "symlink", "hardlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                snapshot = create_snapshot_from_frames(
                    _snapshot_frames(),
                    output_root=root / "snapshots",
                    expected_columns={
                        name: list(frame.columns)
                        for name, frame in _snapshot_frames().items()
                    },
                    schema_version="data-bridge-v1",
                )
                bundle = compose_blackbox_input_bundle(
                    snapshot,
                    platform_input_ids=["api-wind-date-v1"],
                    platform_input_artifacts=[artifact],
                )
                with open_blackbox_runtime_view(
                    bundle,
                    runtime_root=root / "views",
                ) as view:
                    calendar_path = (
                        view.data_dir / "api_wind_date.csv"
                    )
                    view.data_dir.chmod(0o755)
                    if mutation == "writable":
                        calendar_path.chmod(0o644)
                    else:
                        calendar_path.unlink()
                        external = root / f"{mutation}.csv"
                        external.write_bytes(artifact.content_bytes)
                        external.chmod(0o444)
                        if mutation == "symlink":
                            calendar_path.symlink_to(external)
                        else:
                            os.link(external, calendar_path)
                    view.data_dir.chmod(0o555)

                    with self.assertRaises(ValueError):
                        _verify_runtime_platform_files(
                            bundle,
                            view.data_dir,
                        )

    def test_input_state_captures_declared_platform_input_in_read_only_transaction(self) -> None:
        from harness.blackbox_v2.gates import (
            _ensure_input_state,
            _read_input_state,
        )
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            CutoffKeys,
            create_snapshot_from_frames,
        )

        class Connection:
            def __init__(self) -> None:
                self.commands: list[str] = []
                self.rolled_back = False

            def exec_driver_sql(self, statement):
                self.commands.append(str(statement))

            def rollback(self):
                self.rolled_back = True

        class ConnectionContext:
            def __init__(self, connection) -> None:
                self.connection = connection

            def __enter__(self):
                return self.connection

            def __exit__(self, exc_type, exc, tb):
                return None

        class Engine:
            def __init__(self) -> None:
                self.connection = Connection()
                self.disposed = False

            def connect(self):
                return ConnectionContext(self.connection)

            def dispose(self):
                self.disposed = True

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
                platform_inputs=["api-wind-date-v1"],
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            snapshot = replace(
                create_snapshot_from_frames(
                    _snapshot_frames(),
                    output_root=root / "snapshots",
                    expected_columns={
                        name: list(frame.columns)
                        for name, frame in _snapshot_frames().items()
                    },
                    schema_version="data-bridge-v1",
                ),
                generation_id="generation-test",
                refresh_date="2026-07-15",
            )
            cutoffs = CutoffKeys("2026-07-15", "202627", "202606")
            request = BlackboxRequest(
                request_id="trial-request",
                predict_date="2026-07-15",
                feature_date="2026-07-15",
                target_date="2026-07-16",
                daily_cutoff_key=cutoffs.daily_cutoff_key,
                weekly_cutoff_key=cutoffs.weekly_cutoff_key,
                monthly_cutoff_key=cutoffs.monthly_cutoff_key,
            )
            artifact = freeze_platform_input(
                "api-wind-date-v1",
                pd.DataFrame(
                    {
                        "rdate": ["2026-07-14", "2026-07-15"],
                        "week_id": ["202627", "202627"],
                    }
                ),
                weekly_cutoff_key=cutoffs.weekly_cutoff_key,
                audit_provenance={"source_kind": "harness_database"},
            )
            engine = Engine()
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-15",
                project_root=root,
                report_dir=root / "reports",
                config=config,
                engine_factory=lambda: engine,
            )
            with (
                patch(
                    "harness.blackbox_v2.gates.build_blackbox_input_snapshot",
                    return_value=snapshot,
                ),
                patch(
                    "harness.blackbox_v2.gates.resolve_blackbox_input_cutoffs",
                    return_value=cutoffs,
                ),
                patch(
                    "harness.blackbox_v2.gates._feature_date",
                    return_value="2026-07-15",
                ),
                patch(
                    "harness.blackbox_v2.gates.build_live_request",
                    return_value=request,
                ),
                patch(
                    "harness.blackbox_v2.gates.capture_blackbox_platform_inputs_from_connection",
                    return_value=(artifact,),
                ) as capture,
                patch(
                    "harness.blackbox_v2.gates._data_bridge_provenance",
                    return_value={
                        "generation_id": "generation-test",
                        "refresh_date": "2026-07-15",
                        "refreshed_at": "2026-07-15T00:02:00+08:00",
                        "business_digest": "digest",
                        "runtime_profile": "blackbox-v2-v1",
                        "environment_fingerprint": "e" * 64,
                    },
                ),
                patch("harness.blackbox_v2.gates.get_calendar"),
            ):
                state = _ensure_input_state(ctx)

            self.assertNotEqual(
                state.bundle.combined_snapshot_id,
                snapshot.snapshot_id,
            )
            self.assertEqual(
                state.bundle.platform_input_ids,
                ("api-wind-date-v1",),
            )
            capture.assert_called_once_with(
                config.platform_inputs,
                connection=engine.connection,
                weekly_cutoff_key="202627",
            )
            self.assertEqual(
                engine.connection.commands,
                ["START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"],
            )
            self.assertTrue(engine.connection.rolled_back)
            self.assertTrue(engine.disposed)
            raw_state = json.loads(
                (
                    root
                    / "reports"
                    / "blackbox_v2"
                    / "input_state.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                raw_state["combined_snapshot_id"],
                state.bundle.combined_snapshot_id,
            )
            self.assertEqual(
                raw_state["platform_input_ids"],
                ["api-wind-date-v1"],
            )
            self.assertEqual(
                raw_state["platform_input_artifacts"][0]["sha256"],
                artifact.sha256,
            )
            self.assertEqual(
                raw_state["audit_manifest"]["base_snapshot"][
                    "generation_id"
                ],
                "generation-test",
            )
            self.assertEqual(
                raw_state["audit_manifest"]["base_snapshot"][
                    "refresh_date"
                ],
                "2026-07-15",
            )
            self.assertTrue(
                (
                    root
                    / "reports"
                    / "blackbox_v2"
                    / "input_state.initialized"
                ).is_file()
            )
            self.assertNotIn(
                "content_base64",
                raw_state["platform_input_artifacts"][0],
            )
            self.assertTrue(
                Path(
                    raw_state["platform_input_artifacts"][0][
                        "runtime_content_path"
                    ]
                ).is_file()
            )
            self.assertRegex(
                raw_state["request_sha256"],
                r"^[0-9a-f]{64}$",
            )
            reloaded = _read_input_state(
                root
                / "reports"
                / "blackbox_v2"
                / "input_state.json"
            )
            self.assertEqual(
                reloaded.bundle.combined_snapshot_id,
                state.bundle.combined_snapshot_id,
            )
            self.assertEqual(
                reloaded.bundle.audit_manifest,
                state.bundle.audit_manifest,
            )
            self.assertEqual(
                reloaded.bundle.base_snapshot.generation_id,
                "generation-test",
            )
            self.assertEqual(
                reloaded.bundle.base_snapshot.refresh_date,
                "2026-07-15",
            )
            state.request_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Request"):
                _read_input_state(
                    root
                    / "reports"
                    / "blackbox_v2"
                    / "input_state.json"
                )
            (
                root
                / "reports"
                / "blackbox_v2"
                / "input_state.json"
            ).unlink()
            with self.assertRaisesRegex(ValueError, "initialized"):
                _ensure_input_state(ctx)

    def test_monthly_persist_input_treats_predict_date_as_target_cutoff(
        self,
    ) -> None:
        from harness.blackbox_v2.gates import _ensure_input_state
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.snapshot import (
            CutoffKeys,
            create_snapshot_from_frames,
        )

        class Calendar:
            requested: str | None = None

            def previous_trading_day(self, value):
                self.requested = value
                return "2026-05-29"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _delivery(root / "incoming")
            metadata_path = delivery / "trial_10y.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                {
                    "task_type": "monthly",
                    "horizon": 1,
                    "target_rule": (
                        "target_month_observation_yield_vs_"
                        "feature_month_observation_yield"
                    ),
                }
            )
            metadata_path.write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            scheme_dir = intake_delivery(
                delivery,
                schemes_root=root / "schemes",
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            snapshot = replace(
                create_snapshot_from_frames(
                    _snapshot_frames(),
                    output_root=root / "snapshots",
                    expected_columns={
                        name: list(frame.columns)
                        for name, frame in _snapshot_frames().items()
                    },
                    schema_version="data-bridge-v1",
                ),
                generation_id="generation-test",
                refresh_date="2026-07-15",
            )
            cutoffs = CutoffKeys("2026-05-29", "202622", "202604")
            calendar = Calendar()
            engine = SimpleNamespace(dispose=lambda: None)
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-06-01",
                project_root=root,
                report_dir=root / "reports" / "persist",
                config=config,
                persist_backtest=True,
                engine_factory=lambda: engine,
            )
            with (
                patch(
                    "harness.blackbox_v2.gates."
                    "build_blackbox_input_snapshot",
                    return_value=snapshot,
                ),
                patch(
                    "harness.blackbox_v2.gates."
                    "resolve_blackbox_input_cutoffs",
                    return_value=cutoffs,
                ) as resolve_cutoffs,
                patch(
                    "harness.blackbox_v2.gates."
                    "_data_bridge_provenance",
                    return_value={
                        "generation_id": "generation-test",
                        "refresh_date": "2026-07-15",
                        "refreshed_at": "2026-07-15T00:02:00+08:00",
                        "business_digest": "digest",
                        "runtime_profile": "blackbox-v2-v1",
                        "environment_fingerprint": "e" * 64,
                    },
                ),
                patch(
                    "harness.blackbox_v2.gates.get_calendar",
                    return_value=calendar,
                ),
                patch(
                    "harness.blackbox_v2.gates.build_live_request",
                    side_effect=AssertionError(
                        "persist cutoff must not build a live Request"
                    ),
                ),
            ):
                state = _ensure_input_state(ctx)

        self.assertEqual(calendar.requested, "2026-06-01")
        resolve_cutoffs.assert_called_once_with(
            snapshot,
            feature_date="2026-05-29",
            engine=engine,
        )
        self.assertEqual(state.request.predict_date, "2026-05-29")
        self.assertEqual(state.request.feature_date, "2026-05-29")
        self.assertEqual(state.request.target_date, "2026-06-01")

    def test_non_input_gate_fails_closed_on_missing_or_invalid_input_state(self) -> None:
        from harness.blackbox_v2.gates import (
            INPUT_STATE_INITIALIZED_SEAL,
            _ensure_input_state,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ctx = GateContext(
                scheme_id="trial",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=root / "reports",
            )
            gate_root = ctx.report_dir / "blackbox_v2"
            gate_root.mkdir(parents=True)
            (gate_root / "request.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            with patch(
                "harness.blackbox_v2.gates.build_blackbox_input_snapshot"
            ) as build, self.assertRaisesRegex(ValueError, "residual"):
                _ensure_input_state(ctx)
            build.assert_not_called()

            (gate_root / "request.json").unlink()
            seal_path = gate_root / "input_state.initialized"
            seal_path.write_bytes(INPUT_STATE_INITIALIZED_SEAL)
            with patch(
                "harness.blackbox_v2.gates.build_blackbox_input_snapshot"
            ) as build, self.assertRaisesRegex(ValueError, "initialized"):
                _ensure_input_state(ctx)
            build.assert_not_called()

            state_path = gate_root / "input_state.json"
            state_path.write_text("{not-json", encoding="utf-8")
            with (
                patch(
                    "harness.blackbox_v2.gates.build_blackbox_input_snapshot"
                ) as build,
                self.assertRaises(Exception),
            ):
                _ensure_input_state(ctx)
            build.assert_not_called()

    def test_future_row_probe_appends_parseable_daily_date_after_snapshot_max(self) -> None:
        from harness.blackbox_v2.gates import _append_future_rows

        frames = _snapshot_frames()
        frames["daily_output.csv"]["date"] = pd.to_datetime(
            frames["daily_output.csv"]["date"]
        ).dt.strftime("%Y/%m/%d %H:%M")
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            for filename, frame in frames.items():
                frame.to_csv(data_dir / filename, index=False)

            original_max = pd.to_datetime(frames["daily_output.csv"]["date"]).max()
            counts = _append_future_rows(data_dir)
            mutated = pd.read_csv(data_dir / "daily_output.csv")
            mutated_dates = pd.to_datetime(mutated["date"], errors="raise")

        self.assertEqual(counts["daily_output.csv"], 1)
        self.assertGreater(mutated_dates.iloc[-1], original_max)
        self.assertTrue(mutated_dates.is_monotonic_increasing)

    def test_no_persist_backtest_supports_explicit_certification_sizes_and_batch_invariance(self) -> None:
        from harness.blackbox_v2.gates import BlackboxBacktestGate, InputState
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.requests import write_request
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        for sample_size in (100, 101, 500, 1000):
            with self.subTest(sample_size=sample_size), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                scheme_dir = intake_delivery(
                    _delivery(root / "incoming"),
                    schemes_root=root / "schemes",
                )
                config = load_scheme_config(scheme_dir / "config.yaml")
                snapshot = create_snapshot_from_frames(
                    _snapshot_frames(),
                    output_root=root / "snapshots",
                    expected_columns={
                        name: list(frame.columns)
                        for name, frame in _snapshot_frames().items()
                    },
                    schema_version="data-bridge-v1",
                )
                request = BlackboxRequest(
                    request_id="trial-request",
                    predict_date="2026-07-15",
                    feature_date="2026-07-15",
                    target_date="2026-07-16",
                    daily_cutoff_key="2026-07-15",
                    weekly_cutoff_key="202627",
                    monthly_cutoff_key="202606",
                )
                state = InputState(
                    snapshot,
                    write_request(request, root / "request.json"),
                    request,
                )
                ctx = GateContext(
                    scheme_id=config.scheme_id,
                    predict_date="2026-07-16",
                    project_root=root,
                    report_dir=root / "reports",
                    config=config,
                    backtest_sample_size=sample_size,
                )

                def records_for(**kwargs):
                    return [
                        SimpleNamespace(
                            predicted_direction=index % 3 - 1,
                            extra={"request_id": item.request_id},
                        )
                        for index, item in enumerate(kwargs["requests"])
                    ]

                with (
                    patch(
                        "harness.blackbox_v2.gates._ensure_input_state",
                        return_value=state,
                    ),
                    patch(
                        "harness.blackbox_v2.gates._profile",
                        return_value=RuntimeProfile.for_tests(max_batch_requests=100),
                    ),
                    patch(
                        "harness.blackbox_v2.gates._comparison_requests",
                        return_value=[request],
                    ),
                    patch(
                        "harness.blackbox_v2.gates.run_blackbox_backtest",
                        side_effect=records_for,
                    ) as run_backtest,
                ):
                    result = BlackboxBacktestGate().run(ctx)

                self.assertTrue(result.passed, result.errors)
                self.assertEqual(run_backtest.call_count, 2)
                self.assertEqual(
                    [len(call.kwargs["requests"]) for call in run_backtest.call_args_list],
                    [sample_size, sample_size],
                )
                self.assertEqual(
                    [call.kwargs["profile"].max_batch_requests for call in run_backtest.call_args_list],
                    [100, 80],
                )
                self.assertIs(
                    run_backtest.call_args_list[0].kwargs["budget"],
                    run_backtest.call_args_list[1].kwargs["budget"],
                )
                evidence = {item.key: item.value for item in result.evidence}
                self.assertEqual(evidence["sample_size"], sample_size)
                self.assertTrue(evidence["batch_split_invariant"])
                self.assertLessEqual(
                    evidence["subprocesses_started"],
                    evidence["max_subprocesses"],
                )

    def test_backtest_sample_size_is_no_persist_only(self) -> None:
        from harness.cli import _build_parser
        from harness.blackbox_v2.gates import BlackboxBacktestGate

        args = _build_parser().parse_args(
            [
                "gate",
                "backtest",
                "--scheme-id",
                "trial_10y",
                "--sample-size",
                "500",
            ]
        )
        self.assertEqual(args.sample_size, 500)

        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = GateContext(
                scheme_id="trial_10y",
                predict_date="2026-07-20",
                project_root=Path(tmpdir),
                report_dir=Path(tmpdir) / "reports",
                persist_backtest=True,
                backtest_sample_size=100,
            )
            result = BlackboxBacktestGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("--sample-size", "\n".join(result.errors))

    def test_comparison_requests_normalize_databridge_daily_timestamps(self) -> None:
        from harness.blackbox_v2.gates import _comparison_requests
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _snapshot_frames()
        frames["daily_output.csv"]["date"] = [
            "2026/07/13 00:00",
            "2026/07/14 00:00",
            "2026/07/15 00:00",
        ]
        request = BlackboxRequest(
            request_id="trial-request",
            predict_date="2026-07-15",
            feature_date="2026-07-15",
            target_date="2026-07-16",
            daily_cutoff_key="2026-07-15",
            weekly_cutoff_key="202627",
            monthly_cutoff_key="202606",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            requests = _comparison_requests(request, snapshot.data_dir)

        self.assertEqual(requests[0].daily_cutoff_key, "2026-07-14")
        self.assertEqual(requests[1].daily_cutoff_key, "2026-07-15")

    def test_cleanup_removes_runtime_snapshot_but_keeps_audit_state(self) -> None:
        from harness.blackbox_v2.gates import cleanup_runtime_input

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports"
            gate_root = report_dir / "blackbox_v2"
            snapshot_root = gate_root / "runtime_snapshot" / "snapshot-test"
            (snapshot_root / "data").mkdir(parents=True)
            state_path = gate_root / "input_state.json"
            state_path.write_text(
                json.dumps({"snapshot_root": str(snapshot_root)}),
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="trial",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=report_dir,
            )

            cleanup_runtime_input(ctx)

            self.assertFalse((gate_root / "runtime_snapshot").exists())
            self.assertTrue(state_path.is_file())

    def test_cleanup_preserves_runtime_view_marked_as_uncertain_debris(self) -> None:
        from harness.blackbox_v2.gates import cleanup_runtime_input

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports"
            runtime_root = report_dir / "blackbox_v2" / "runtime_views"
            active_view = (
                runtime_root
                / "active"
                / "blackbox-runtime-uncertain"
            )
            debris_root = runtime_root / "debris"
            active_view.mkdir(parents=True)
            debris_root.mkdir()
            (active_view / "api_wind_date.csv").write_text(
                "rdate,week_id\n",
                encoding="utf-8",
            )
            (debris_root / "blackbox-runtime-uncertain.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="trial",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=report_dir,
            )

            cleanup_runtime_input(ctx)

            self.assertTrue(active_view.is_dir())
            self.assertTrue(
                (
                    debris_root
                    / "blackbox-runtime-uncertain.json"
                ).is_file()
            )

    def test_cleanup_rejects_runtime_symlink_without_touching_external_files(self) -> None:
        from harness.blackbox_v2.gates import cleanup_runtime_input

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports"
            gate_root = report_dir / "blackbox_v2"
            gate_root.mkdir(parents=True)
            external = root / "external"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            (gate_root / "runtime_snapshot").symlink_to(
                external,
                target_is_directory=True,
            )
            ctx = GateContext(
                scheme_id="trial",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=report_dir,
            )

            with self.assertRaisesRegex(ValueError, "symlink"):
                cleanup_runtime_input(ctx)

            self.assertEqual(
                sentinel.read_text(encoding="utf-8"),
                "keep\n",
            )
            self.assertTrue(
                (gate_root / "runtime_snapshot").is_symlink()
            )

    def test_cleanup_rejects_report_or_gate_root_symlink(self) -> None:
        from harness.blackbox_v2.gates import cleanup_runtime_input

        for target in ("report_dir", "gate_root"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                external = root / "external"
                external.mkdir()
                sentinel = external / "sentinel.txt"
                sentinel.write_text("keep\n", encoding="utf-8")
                report_dir = root / "reports"
                if target == "report_dir":
                    report_dir.symlink_to(
                        external,
                        target_is_directory=True,
                    )
                else:
                    report_dir.mkdir()
                    (report_dir / "blackbox_v2").symlink_to(
                        external,
                        target_is_directory=True,
                    )
                ctx = GateContext(
                    scheme_id="trial",
                    predict_date="2026-07-16",
                    project_root=root,
                    report_dir=report_dir,
                )

                with self.assertRaisesRegex(ValueError, "symlink"):
                    cleanup_runtime_input(ctx)

                self.assertEqual(
                    sentinel.read_text(encoding="utf-8"),
                    "keep\n",
                )

    def test_cleanup_removes_platform_runtime_bytes_from_final_state(self) -> None:
        from harness.blackbox_v2.gates import cleanup_runtime_input

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            report_dir = root / "reports"
            gate_root = report_dir / "blackbox_v2"
            runtime_snapshot = gate_root / "runtime_snapshot"
            platform_file = (
                runtime_snapshot
                / "platform_inputs"
                / "api_wind_date.csv"
            )
            platform_file.parent.mkdir(parents=True)
            platform_file.write_text(
                "rdate,week_id\n2026-07-15,202627\n",
                encoding="utf-8",
            )
            state_path = gate_root / "input_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "platform_input_artifacts": [
                            {
                                "artifact_id": "api-wind-date-v1",
                                "sha256": "a" * 64,
                                "runtime_content_path": str(platform_file),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="trial",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=report_dir,
            )

            cleanup_runtime_input(ctx)

            final_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            self.assertFalse(runtime_snapshot.exists())
            self.assertNotIn(
                "runtime_content_path",
                final_state["platform_input_artifacts"][0],
            )
            self.assertNotIn(
                "content_base64",
                state_path.read_text(encoding="utf-8"),
            )

    def test_direct_blackbox_gate_cli_cleans_runtime_snapshot(self) -> None:
        """单 Gate CLI 结束后也必须清理临时三频副本。"""
        from harness.cli import _build_parser, _run_gate
        from harness.result import GateResult, GateStatus
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            report_dir = root / "reports" / "direct-gate"
            args = _build_parser().parse_args(
                [
                    "gate",
                    "backtest",
                    "--scheme-id",
                    "trial_10y",
                    "--predict-date",
                    "2026-07-16",
                    "--project-root",
                    str(root),
                    "--report-dir",
                    str(report_dir),
                ]
            )

            class _SnapshotGate:
                def run(self, ctx):
                    snapshot = (
                        ctx.report_dir
                        / "blackbox_v2"
                        / "runtime_snapshot"
                        / "snapshot-test"
                        / "data"
                    )
                    snapshot.mkdir(parents=True)
                    (snapshot / "daily_output.csv").write_text("date\n", encoding="utf-8")
                    return GateResult(
                        gate_name="backtest",
                        status=GateStatus.PASSED,
                        passed=True,
                        evidence=[],
                        errors=[],
                        started_at="2026-07-16T00:00:00+00:00",
                        finished_at="2026-07-16T00:00:01+00:00",
                    )

            with patch("harness.cli.gate_for_name", return_value=_SnapshotGate()):
                result = _run_gate(args)

            self.assertTrue(result.passed)
            self.assertFalse((report_dir / "blackbox_v2" / "runtime_snapshot").exists())

    def test_comparison_requests_use_distinct_existing_cutoffs(self) -> None:
        from harness.blackbox_v2.gates import _comparison_requests
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames

        frames = _snapshot_frames()
        request = BlackboxRequest(
            request_id="trial-request",
            predict_date="2026-07-15",
            feature_date="2026-07-15",
            target_date="2026-07-16",
            daily_cutoff_key="2026-07-15",
            weekly_cutoff_key="202627",
            monthly_cutoff_key="202606",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=Path(tmpdir),
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            requests = _comparison_requests(request, snapshot.data_dir)

        self.assertEqual(len(requests), 2)
        self.assertEqual({item.daily_cutoff_key for item in requests}, {"2026-07-14", "2026-07-15"})
        self.assertEqual({item.weekly_cutoff_key for item in requests}, {"202626", "202627"})
        self.assertEqual({item.monthly_cutoff_key for item in requests}, {"202605", "202606"})

    def test_automatic_execution_gates_run_end_to_end_without_business_writes(self) -> None:
        from harness.blackbox_v2.gates import (
            BlackboxApiReadinessGate,
            BlackboxBacktestGate,
            BlackboxCompareGate,
            BlackboxDryRunGate,
            BlackboxUnitGate,
            InputState,
            _append_future_rows,
        )
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.requests import write_request
        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from tests.test_blackbox_v2_runner import _SUCCESS_SCRIPT

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming", script=_SUCCESS_SCRIPT),
                schemes_root=root / "schemes",
                platform_inputs=["api-wind-date-v1"],
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            frames = _snapshot_frames()
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            request = BlackboxRequest(
                request_id="trial-request",
                predict_date="2026-07-15",
                feature_date="2026-07-15",
                target_date="2026-07-16",
                daily_cutoff_key="2026-07-15",
                weekly_cutoff_key="202627",
                monthly_cutoff_key="202606",
            )
            request_path = write_request(request, root / "request.json")
            calendar_artifact = freeze_platform_input(
                "api-wind-date-v1",
                pd.DataFrame(
                    {
                        "rdate": ["2026-07-13", "2026-07-14", "2026-07-15"],
                        "week_id": ["202627", "202627", "202627"],
                    }
                ),
                weekly_cutoff_key=request.weekly_cutoff_key,
                audit_provenance={"source_kind": "harness_database"},
            )
            bundle = compose_blackbox_input_bundle(
                snapshot,
                platform_input_ids=config.platform_inputs,
                platform_input_artifacts=[calendar_artifact],
            )
            state = InputState(
                snapshot=snapshot,
                request_path=request_path,
                request=request,
                bundle=bundle,
            )
            ctx = _context(root, config)
            gates = [
                BlackboxUnitGate(),
                BlackboxDryRunGate(),
                BlackboxCompareGate(),
                BlackboxBacktestGate(),
                BlackboxApiReadinessGate(),
            ]
            with patch("harness.blackbox_v2.gates._ensure_input_state", return_value=state):
                with patch(
                    "harness.blackbox_v2.gates._profile",
                    return_value=RuntimeProfile.for_tests(),
                ):
                    results = [gate.run(ctx) for gate in gates]
            def tamper_calendar(data_dir):
                counts = _append_future_rows(data_dir)
                calendar_path = data_dir / "api_wind_date.csv"
                calendar_path.chmod(0o644)
                calendar_path.write_text(
                    "rdate,week_id\n2099-01-01,209901\n",
                    encoding="utf-8",
                )
                return counts

            with (
                patch(
                    "harness.blackbox_v2.gates._ensure_input_state",
                    return_value=state,
                ),
                patch(
                    "harness.blackbox_v2.gates._profile",
                    return_value=RuntimeProfile.for_tests(),
                ),
                patch(
                    "harness.blackbox_v2.gates._append_future_rows",
                    side_effect=tamper_calendar,
                ),
            ):
                tampered_compare = BlackboxCompareGate().run(ctx)
            unit_root = root / "reports" / "blackbox_v2" / "unit"
            self.assertTrue((unit_root / "request" / "invalid_request.json").is_file())
            self.assertFalse((unit_root / "invalid_request.json").exists())
            for result in results:
                evidence = {item.key: item.value for item in result.evidence}
                self.assertEqual(
                    evidence["data_snapshot_id"],
                    bundle.combined_snapshot_id,
                )
                self.assertEqual(
                    evidence["parent_data_snapshot_id"],
                    snapshot.snapshot_id,
                )
                self.assertEqual(
                    evidence["platform_input_ids"],
                    ["api-wind-date-v1"],
                )
                self.assertEqual(
                    evidence["platform_input_hashes"],
                    {"api-wind-date-v1": calendar_artifact.sha256},
                )
            backtest_evidence = {
                item.key: item.value
                for item in results[3].evidence
            }
            self.assertEqual(backtest_evidence["requests"], 100)
            self.assertEqual(backtest_evidence["records"], 100)
            self.assertFalse(backtest_evidence["persist"])
            compare_evidence = {
                item.key: item.value
                for item in results[2].evidence
            }
            self.assertTrue(
                compare_evidence["platform_input_hashes_unchanged"]
            )
            self.assertFalse(
                (root / "reports" / "blackbox_v2" / "runtime_views").exists()
            )

        self.assertTrue(all(result.passed for result in results), [result.errors for result in results])
        self.assertFalse(tampered_compare.passed)
        self.assertIn(
            "platform input",
            "\n".join(tampered_compare.errors).lower(),
        )

    def test_api_readiness_allows_active_scheme_recertification(self) -> None:
        from harness.blackbox_v2.gates import BlackboxApiReadinessGate, InputState
        from scheduler.blackbox_v2_runner import RuntimeProfile
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.contracts import BlackboxRequest
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.requests import write_request
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames
        from tests.test_blackbox_v2_runner import _SUCCESS_SCRIPT

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming", script=_SUCCESS_SCRIPT),
                schemes_root=root / "schemes",
            )
            config = replace(
                load_scheme_config(scheme_dir / "config.yaml"),
                status="active",
                version_status="active",
            )
            frames = _snapshot_frames()
            snapshot = create_snapshot_from_frames(
                frames,
                output_root=root / "snapshots",
                expected_columns={name: list(frame.columns) for name, frame in frames.items()},
                schema_version="data-bridge-v1",
            )
            request = BlackboxRequest(
                request_id="trial-request",
                predict_date="2026-07-20",
                feature_date="2026-07-17",
                target_date="2026-07-24",
                daily_cutoff_key="2026-07-15",
                weekly_cutoff_key="202627",
                monthly_cutoff_key="202606",
            )
            state = InputState(
                snapshot=snapshot,
                request_path=write_request(request, root / "request.json"),
                request=request,
            )
            ctx = _context(root, config)
            with (
                patch("harness.blackbox_v2.gates._ensure_input_state", return_value=state),
                patch(
                    "harness.blackbox_v2.gates._profile",
                    return_value=RuntimeProfile.for_tests(),
                ),
            ):
                result = BlackboxApiReadinessGate().run(ctx)

        evidence = {item.key: item.value for item in result.evidence}
        self.assertTrue(result.passed, result.errors)
        self.assertEqual(evidence["lifecycle_mode"], "active_recertification")
        self.assertTrue(evidence["scheduler_eligible"])
        self.assertTrue(evidence["api_visible"])

    def test_persist_backtest_requires_signed_exact_authorization_and_verifies_deltas(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxBacktestGate, InputState, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.requests import write_request
        from shared.blackbox_v2.platform_inputs import freeze_platform_input
        from shared.blackbox_v2.snapshot import (
            compose_blackbox_input_bundle,
            create_snapshot_from_frames,
        )
        from tests.test_blackbox_v2_backtest_persistence import _cases, _output

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
                platform_inputs=["api-wind-date-v1"],
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            snapshot = create_snapshot_from_frames(
                _snapshot_frames(),
                output_root=root / "snapshots",
                expected_columns={name: list(frame.columns) for name, frame in _snapshot_frames().items()},
                schema_version="data-bridge-v1",
            )
            request = _cases(1)[0].request
            artifact = freeze_platform_input(
                "api-wind-date-v1",
                pd.DataFrame(
                    {
                        "rdate": ["2026-07-15"],
                        "week_id": [request.weekly_cutoff_key],
                    }
                ),
                weekly_cutoff_key=request.weekly_cutoff_key,
            )
            bundle = compose_blackbox_input_bundle(
                snapshot,
                platform_input_ids=config.platform_inputs,
                platform_input_artifacts=[artifact],
            )
            state = InputState(
                snapshot,
                write_request(request, root / "request.json"),
                request,
                bundle,
            )
            passed = PassedAllRun(
                "hr_passed",
                root / "reports" / "all",
                bundle.combined_snapshot_id,
                generation_id="generation-current", runtime_profile="blackbox-v2-v1",
                environment_fingerprint="e" * 64,
            )
            with patch.dict(os.environ, {"HARNESS_AUTH_SECRET": "test-secret"}):
                token = issue_token(
                    config.scheme_id,
                    "backtest_persist",
                    "2026-07-16",
                    scheme_version=config.scheme_version,
                    harness_run_id="hr_passed",
                    ttl_seconds=300,
                    issued_by="platform-test",
                )
                ctx = GateContext(
                    scheme_id=config.scheme_id,
                    predict_date="2026-07-16",
                    project_root=root,
                    report_dir=root / "reports" / "persist",
                    config=config,
                    authorization=token,
                    persist_backtest=True,
                    engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                )
                output = _output(205)
                metric_count = len(output.monthly_metrics)
                historical_runtime_files: list[str] = []

                def run_history(**kwargs):
                    historical_runtime_files.extend(
                        sorted(
                            path.name
                            for path in kwargs[
                                "runtime_data_dir"
                            ].iterdir()
                        )
                    )
                    self.assertEqual(
                        kwargs[
                            "input_bundle"
                        ].combined_snapshot_id,
                        bundle.combined_snapshot_id,
                    )
                    return output

                snapshots = [
                    {"t_backtest_runs": 10, "t_backtest_predictions": 1000, "t_backtest_monthly_metrics": 20},
                    {
                        "t_backtest_runs": 11,
                        "t_backtest_predictions": 1205,
                        "t_backtest_monthly_metrics": 20 + metric_count,
                    },
                ]
                with (
                    patch(
                        "harness.blackbox_v2.gates._ensure_input_state",
                        return_value=state,
                    ) as ensure_state,
                    patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed),
                    patch("harness.blackbox_v2.gates._data_bridge_provenance", return_value={
                        "generation_id": "generation-current",
                        "refresh_date": "2026-07-16",
                        "refreshed_at": "2026-07-16T05:40:00+08:00",
                        "business_digest": "digest",
                        "runtime_profile": "blackbox-v2-v1",
                        "environment_fingerprint": "e" * 64,
                    }),
                    patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64),
                    patch("harness.blackbox_v2.gates.build_historical_cases", return_value=_cases(205)) as build_cases,
                    patch(
                        "harness.blackbox_v2.gates.run_blackbox_historical_backtest",
                        side_effect=run_history,
                    ) as run_history_mock,
                    patch("harness.blackbox_v2.gates.persist_backtest_output_atomic", return_value=301) as persist,
                    patch("harness.blackbox_v2.gates.snapshot_backtest_scope_counts", side_effect=snapshots),
                ):
                    result = BlackboxBacktestGate().run(ctx)

        self.assertTrue(result.passed, result.errors)
        ensure_state.assert_called_once_with(ctx)
        build_cases.assert_called_once()
        self.assertIsNone(build_cases.call_args.kwargs["limit"])
        self.assertEqual(build_cases.call_args.kwargs["target_date_before"], "2026-07-16")
        self.assertEqual(build_cases.call_args.kwargs["predict_date_from"], "2025-01-01")
        run_history_mock.assert_called_once()
        budget = run_history_mock.call_args.kwargs["budget"]
        self.assertEqual(budget.max_subprocesses, 3)
        persist.assert_called_once()
        evidence = {item.key: item.value for item in result.evidence}
        self.assertTrue(evidence["persist"])
        self.assertEqual(evidence["run_id"], 301)
        self.assertEqual(evidence["protected_table_deltas"]["t_backtest_runs"], 1)
        self.assertEqual(evidence["protected_table_deltas"]["t_backtest_predictions"], 205)
        self.assertEqual(
            evidence["protected_table_deltas"]["t_backtest_monthly_metrics"],
            metric_count,
        )
        self.assertEqual(evidence["backtest_start_date"], "2025-01-01")
        self.assertEqual(evidence["batch_count"], 3)
        self.assertEqual(evidence["batch_sizes"], [100, 100, 5])
        self.assertEqual(evidence["replay_semantics"], "current_snapshot_as_of_not_historical_vintage")
        self.assertEqual(
            historical_runtime_files,
            [
                "api_wind_date.csv",
                "daily_output.csv",
                "monthly_output.csv",
                "weekly_output.csv",
            ],
        )
        self.assertEqual(
            evidence["data_snapshot_id"],
            bundle.combined_snapshot_id,
        )

    def test_persist_backtest_token_start_date_must_match_context(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxBacktestGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"HARNESS_AUTH_SECRET": "test-secret"},
        ):
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            token = issue_token(
                config.scheme_id,
                "backtest_persist",
                "2026-07-20",
                scheme_version=config.scheme_version,
                harness_run_id="hr_passed",
                ttl_seconds=300,
                backtest_start_date="2025-01-01",
            )
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-20",
                project_root=root,
                report_dir=root / "reports",
                config=config,
                authorization=token,
                persist_backtest=True,
                backtest_start_date="2025-02-01",
            )
            with patch("harness.blackbox_v2.gates.build_historical_cases") as build_cases:
                result = BlackboxBacktestGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("backtest_start_date mismatch", "\n".join(result.errors))
        build_cases.assert_not_called()

    def test_weekly_persist_backtest_requires_platform_history_start(self) -> None:
        from harness.blackbox_v2.gates import BlackboxBacktestGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _delivery(root / "incoming")
            metadata_path = delivery / "trial_10y.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.update(
                {
                    "task_type": "weekly_point",
                    "horizon": 1,
                    "target_rule": (
                        "target_week_end_yield_vs_feature_week_end_yield"
                    ),
                }
            )
            metadata_path.write_text(
                json.dumps(metadata),
                encoding="utf-8",
            )
            scheme_dir = intake_delivery(
                delivery,
                schemes_root=root / "schemes",
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            engine_calls: list[bool] = []

            def _engine_factory():
                engine_calls.append(True)
                raise AssertionError("weekly start guard must run before DB access")

            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-20",
                project_root=root,
                report_dir=root / "reports",
                config=config,
                persist_backtest=True,
                backtest_start_date="2024-01-01",
                engine_factory=_engine_factory,
            )
            with patch.dict(os.environ, {"HARNESS_AUTH_SECRET": "test-secret"}):
                result = BlackboxBacktestGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn(
            "weekly persisted backtest requires backtest_start_date=2025-01-01",
            "\n".join(result.errors),
        )
        self.assertEqual(engine_calls, [])

    def test_persist_backtest_blocks_unsigned_or_mismatched_token_before_business_work(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxBacktestGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            with patch.dict(os.environ, {"HARNESS_AUTH_SECRET": ""}):
                unsigned = issue_token(
                    config.scheme_id,
                    "backtest_persist",
                    "2026-07-16",
                    scheme_version=config.scheme_version,
                    harness_run_id="hr_passed",
                    ttl_seconds=300,
                )
                unsigned_ctx = GateContext(
                    scheme_id=config.scheme_id,
                    predict_date="2026-07-16",
                    project_root=root,
                    report_dir=root / "reports" / "unsigned",
                    config=config,
                    authorization=unsigned,
                    persist_backtest=True,
                )
                with patch("harness.blackbox_v2.gates.build_historical_cases") as build_cases:
                    unsigned_result = BlackboxBacktestGate().run(unsigned_ctx)
            self.assertFalse(unsigned_result.passed)
            self.assertEqual(unsigned_result.status.value, "blocked")
            build_cases.assert_not_called()

            with patch.dict(os.environ, {"HARNESS_AUTH_SECRET": "test-secret"}):
                mismatch = issue_token(
                    config.scheme_id,
                    "backtest_persist",
                    "2026-07-16",
                    scheme_version="wrong-version",
                    harness_run_id="hr_passed",
                    ttl_seconds=300,
                )
                mismatch_ctx = replace(unsigned_ctx, authorization=mismatch)
                with patch("harness.blackbox_v2.gates.build_historical_cases") as build_cases:
                    mismatch_result = BlackboxBacktestGate().run(mismatch_ctx)
                self.assertFalse(mismatch_result.passed)
                self.assertEqual(mismatch_result.status.value, "blocked")
                self.assertIn("scheme_version", "\n".join(mismatch_result.errors))
                build_cases.assert_not_called()

    def test_persist_backtest_cannot_pass_when_table_deltas_are_zero(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxBacktestGate, InputState, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.requests import write_request
        from shared.blackbox_v2.snapshot import create_snapshot_from_frames
        from tests.test_blackbox_v2_backtest_persistence import _cases, _output

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"HARNESS_AUTH_SECRET": "secret"}):
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            snapshot = create_snapshot_from_frames(
                _snapshot_frames(), output_root=root / "snapshots",
                expected_columns={name: list(frame.columns) for name, frame in _snapshot_frames().items()},
                schema_version="data-bridge-v1",
            )
            request = _cases(1)[0].request
            state = InputState(snapshot, write_request(request, root / "request.json"), request)
            token = issue_token(
                config.scheme_id, "backtest_persist", "2026-07-16",
                scheme_version=config.scheme_version, harness_run_id="hr_passed",
                ttl_seconds=300, issued_by="tester",
            )
            ctx = GateContext(
                config.scheme_id, "2026-07-16", root, root / "reports", config=config,
                authorization=token, persist_backtest=True,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            passed = PassedAllRun(
                "hr_passed",
                root / "all",
                snapshot.snapshot_id,
                generation_id="generation",
                runtime_profile="blackbox-v2-v1",
                environment_fingerprint="e" * 64,
            )
            counts = {"t_backtest_runs": 1, "t_backtest_predictions": 100, "t_backtest_monthly_metrics": 3}
            with (
                patch("harness.blackbox_v2.gates._ensure_input_state", return_value=state),
                patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed),
                patch("harness.blackbox_v2.gates._data_bridge_provenance", return_value={
                    "generation_id": "generation",
                    "runtime_profile": "blackbox-v2-v1",
                    "environment_fingerprint": "e" * 64,
                }),
                patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64),
                patch("harness.blackbox_v2.gates.build_historical_cases", return_value=_cases(100)),
                patch("harness.blackbox_v2.gates.run_blackbox_historical_backtest", return_value=_output()),
                patch("harness.blackbox_v2.gates.persist_backtest_output_atomic", return_value=9),
                patch("harness.blackbox_v2.gates.snapshot_backtest_scope_counts", side_effect=[counts, counts]),
            ):
                result = BlackboxBacktestGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("t_backtest_runs delta must be 1", "\n".join(result.errors))
        self.assertIn("t_backtest_predictions delta must be 100", "\n".join(result.errors))

    def test_persist_backtest_token_must_bind_latest_passed_all_stage_run(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxBacktestGate, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"HARNESS_AUTH_SECRET": "secret"}):
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            token = issue_token(
                config.scheme_id, "backtest_persist", "2026-07-16",
                scheme_version=config.scheme_version, harness_run_id="hr_old",
                ttl_seconds=300, issued_by="tester",
            )
            ctx = GateContext(
                config.scheme_id, "2026-07-16", root, root / "reports", config=config,
                authorization=token, persist_backtest=True,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            latest = PassedAllRun("hr_latest", root / "all", "snapshot-latest")
            with (
                patch("harness.blackbox_v2.gates._verify_passed_all", return_value=latest),
                patch("harness.blackbox_v2.gates.build_historical_cases") as build_cases,
            ):
                result = BlackboxBacktestGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertEqual(result.status.value, "blocked")
        self.assertIn("latest passed all-stage", "\n".join(result.errors))
        build_cases.assert_not_called()

    def test_shadow_register_requires_scoped_token_and_keeps_registry_paused(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxShadowRegisterGate, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            token = issue_token(
                "trial_10y",
                "shadow_register",
                "2026-07-16",
                scheme_version=config.scheme_version,
                harness_run_id="hr_passed",
            )
            ctx = GateContext(
                scheme_id="trial_10y",
                predict_date="2026-07-16",
                project_root=root,
                report_dir=root / "reports" / "shadow",
                config=config,
                authorization=token,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            passed = PassedAllRun(
                harness_run_id="hr_passed",
                report_uri=root / "reports" / "all",
                data_snapshot_id="snapshot-test",
            )
            with patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed):
                with patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64):
                    with patch(
                        "harness.blackbox_v2.gates._read_shadow_state",
                        side_effect=lambda _engine, current: SimpleNamespace(
                            version_status=current.version_status,
                            registry_status="paused",
                        ),
                    ):
                        with patch("harness.blackbox_v2.gates._register_shadow") as register:
                            register.return_value = SimpleNamespace(
                                scheme_version=config.scheme_version,
                                version_status="shadow",
                                registry_status="paused",
                                runtime_type="blackbox_v2",
                                data_snapshot_id="db-snapshot",
                                environment_fingerprint="d" * 64,
                                code_hash="c" * 64,
                                config_hash="f" * 64,
                                manifest_hash="m" * 64,
                            )
                            result = BlackboxShadowRegisterGate().run(ctx)

            updated = load_scheme_config(scheme_dir / "config.yaml")

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(updated.status, "paused")
        self.assertEqual(updated.version_status, "shadow")
        validated_cfg, shadow_cfg = register.call_args.args[1:]
        self.assertEqual(validated_cfg.version_status, "validated")
        self.assertEqual(shadow_cfg.version_status, "shadow")
        self.assertEqual(shadow_cfg.data_snapshot_id, "snapshot-test")
        self.assertEqual(shadow_cfg.environment_fingerprint, "e" * 64)
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["version_status"], "shadow")
        self.assertEqual(evidence["registry_status"], "paused")
        self.assertEqual(evidence["data_snapshot_id"], "db-snapshot")
        self.assertEqual(evidence["environment_fingerprint"], "d" * 64)
        self.assertEqual(evidence["code_hash"], "c" * 64)
        self.assertEqual(evidence["config_hash"], "f" * 64)
        self.assertEqual(evidence["manifest_hash"], "m" * 64)
        self.assertEqual(evidence["journal_phase"], "verified")

    def test_register_shadow_persists_exact_shadow_db_state(self) -> None:
        from harness.blackbox_v2.gates import _register_shadow
        from tests.test_repository_registry import _CaptureEngine, _blackbox_config

        engine = _CaptureEngine()
        validated_cfg = _blackbox_config(status="paused", version_status="validated")
        shadow_cfg = _blackbox_config(status="paused", version_status="shadow")

        state = _register_shadow(engine, validated_cfg, shadow_cfg)

        self.assertEqual(engine.store["version_row"]["status"], "shadow")
        self.assertEqual({row["status"] for row in engine.store["registry_rows"]}, {"paused"})
        self.assertEqual(engine.store["version_row"]["environment_fingerprint"], "e" * 64)
        self.assertEqual(engine.store["version_row"]["data_snapshot_id"], "snapshot-1")
        self.assertEqual(state.version_status, "shadow")
        self.assertEqual(state.registry_status, "paused")

    def test_shadow_failure_restores_exact_config_and_marks_compensated(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxShadowRegisterGate, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config_path = scheme_dir / "config.yaml"
            original = config_path.read_text(encoding="utf-8")
            config = load_scheme_config(config_path)
            token = issue_token(
                config.scheme_id,
                "shadow_register",
                "2026-07-16",
                scheme_version=config.scheme_version,
                harness_run_id="hr_passed",
            )
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-16",
                project_root=root,
                report_dir=root / "reports" / "shadow",
                config=config,
                authorization=token,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            state = {"version": "draft", "registry": "paused"}
            passed = PassedAllRun("hr_passed", root / "reports" / "all", "snapshot-test")

            def read_state(_engine, _cfg):
                return SimpleNamespace(
                    version_status=state["version"],
                    registry_status=state["registry"],
                )

            def compensate(_engine, _cfg, **kwargs):
                state["version"] = kwargs["version_status"]
                state["registry"] = kwargs["registry_status"]

            with (
                patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed),
                patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64),
                patch("harness.blackbox_v2.gates._read_shadow_state", side_effect=read_state),
                patch("harness.blackbox_v2.gates._register_shadow", side_effect=RuntimeError("injected")),
                patch("scheduler.repository.apply_blackbox_lifecycle_state", side_effect=compensate),
            ):
                result = BlackboxShadowRegisterGate().run(ctx)

            evidence = {item.key: item.value for item in result.evidence}
            journal = json.loads(Path(evidence["journal_path"]).read_text(encoding="utf-8"))
            self.assertFalse(result.passed)
            self.assertTrue(evidence["compensated"])
            self.assertEqual(journal["phase"], "compensated")
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertEqual(state, {"version": "draft", "registry": "paused"})

    def test_shadow_register_rejects_prior_active_exact_version(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxShadowRegisterGate, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            token = issue_token(
                config.scheme_id,
                "shadow_register",
                "2026-07-16",
                scheme_version=config.scheme_version,
                harness_run_id="hr_passed",
            )
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-16",
                project_root=root,
                report_dir=root / "reports" / "shadow",
                config=config,
                authorization=token,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            passed = PassedAllRun("hr_passed", root / "reports" / "all", "snapshot-test")
            with (
                patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed),
                patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64),
                patch(
                    "harness.blackbox_v2.gates._read_shadow_state",
                    return_value=SimpleNamespace(version_status="active", registry_status="paused"),
                ),
                patch("harness.blackbox_v2.gates._register_shadow") as register,
            ):
                result = BlackboxShadowRegisterGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("draft or validated", "\n".join(result.errors))
        register.assert_not_called()

    def test_shadow_verification_rejects_config_version_status_mismatch(self) -> None:
        from dataclasses import replace

        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxShadowRegisterGate, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config_path = scheme_dir / "config.yaml"
            config = load_scheme_config(config_path)
            token = issue_token(
                config.scheme_id,
                "shadow_register",
                "2026-07-16",
                scheme_version=config.scheme_version,
                harness_run_id="hr_passed",
            )
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-16",
                project_root=root,
                report_dir=root / "reports" / "shadow",
                config=config,
                authorization=token,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            state = {"version": "draft", "registry": "paused"}
            passed = PassedAllRun("hr_passed", root / "reports" / "all", "snapshot-test")
            real_load = load_scheme_config

            def read_state(_engine, _cfg):
                return SimpleNamespace(
                    version_status=state["version"],
                    registry_status=state["registry"],
                )

            def register(_engine, _validated, _shadow):
                state.update(version="shadow", registry="paused")
                return SimpleNamespace(
                    scheme_version=config.scheme_version,
                    version_status="shadow",
                    registry_status="paused",
                    runtime_type="blackbox_v2",
                    data_snapshot_id="snapshot-test",
                    environment_fingerprint="e" * 64,
                    code_hash=config.code_hash,
                    config_hash=config.config_hash,
                    manifest_hash=config.manifest_hash,
                )

            def load_with_mismatch(path):
                current = real_load(path)
                if current.version_status == "shadow":
                    return replace(current, version_status="draft")
                return current

            def compensate(_engine, _cfg, **kwargs):
                state.update(version=kwargs["version_status"], registry=kwargs["registry_status"])

            with (
                patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed),
                patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64),
                patch("harness.blackbox_v2.gates._read_shadow_state", side_effect=read_state),
                patch("harness.blackbox_v2.gates._register_shadow", side_effect=register),
                patch("harness.blackbox_v2.gates.load_scheme_config", side_effect=load_with_mismatch),
                patch("scheduler.repository.apply_blackbox_lifecycle_state", side_effect=compensate),
            ):
                result = BlackboxShadowRegisterGate().run(ctx)

            restored = real_load(config_path)

        self.assertFalse(result.passed)
        self.assertEqual((restored.status, restored.version_status), ("paused", "draft"))
        self.assertEqual(state, {"version": "draft", "registry": "paused"})

    def test_shadow_version_drift_after_db_write_restores_original_exact_version(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.gates import BlackboxShadowRegisterGate, PassedAllRun
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery
        from shared.blackbox_v2.lifecycle import load_journal

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config_path = scheme_dir / "config.yaml"
            config = load_scheme_config(config_path)
            original_version = config.scheme_version
            token = issue_token(
                config.scheme_id,
                "shadow_register",
                "2026-07-16",
                scheme_version=original_version,
                harness_run_id="hr_passed",
            )
            ctx = GateContext(
                scheme_id=config.scheme_id,
                predict_date="2026-07-16",
                project_root=root,
                report_dir=root / "reports" / "shadow",
                config=config,
                authorization=token,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
            )
            passed = PassedAllRun("hr_passed", root / "reports" / "all", "snapshot-test")
            states = {original_version: "draft"}
            registry = {"status": "paused"}
            compensation_versions: list[str] = []

            def read_state(_engine, current):
                return SimpleNamespace(
                    version_status=states.get(current.scheme_version, "draft"),
                    registry_status=registry["status"],
                )

            def register(_engine, _validated, current):
                self.assertEqual(current.scheme_version, original_version)
                states[original_version] = "shadow"
                script = scheme_dir / "delivery" / f"{config.scheme_id}.py"
                script.chmod(0o644)
                script.write_text(
                    "import argparse\nimport json\n# changed after target DB write\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    scheme_version=original_version,
                    version_status="shadow",
                    registry_status="paused",
                )

            def compensate(_engine, current, **kwargs):
                compensation_versions.append(current.scheme_version)
                states[current.scheme_version] = kwargs["version_status"]
                registry["status"] = kwargs["registry_status"]

            with (
                patch("harness.blackbox_v2.gates._verify_passed_all", return_value=passed),
                patch("harness.blackbox_v2.gates._environment_fingerprint", return_value="e" * 64),
                patch("harness.blackbox_v2.gates._read_shadow_state", side_effect=read_state),
                patch("harness.blackbox_v2.gates._register_shadow", side_effect=register),
                patch("scheduler.repository.apply_blackbox_lifecycle_state", side_effect=compensate),
            ):
                result = BlackboxShadowRegisterGate().run(ctx)

            changed = load_scheme_config(config_path)
            evidence = {item.key: item.value for item in result.evidence}
            journal = load_journal(Path(evidence["journal_path"]))

        self.assertFalse(result.passed)
        self.assertIn("canonical version changed", "\n".join(result.errors))
        self.assertNotEqual(changed.scheme_version, original_version)
        self.assertEqual((changed.status, changed.version_status), ("paused", "draft"))
        self.assertEqual(states[original_version], "draft")
        self.assertNotEqual(states.get(changed.scheme_version), "shadow")
        self.assertEqual(compensation_versions, [original_version])
        self.assertEqual(journal.phase, "compensated")

    def test_static_gate_accepts_exact_two_file_delivery(self) -> None:
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(_delivery(root / "incoming"), schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            result = BlackboxStaticGate().run(_context(root, config))

        self.assertTrue(result.passed, result.errors)

    def test_static_gate_tolerates_pycache_left_by_execution(self) -> None:
        """执行过方案后遗留的 __pycache__ 不得让复验误判交付结构不合规。

        CPython 在 import delivery 模块时会在 scheme/delivery 目录写入
        `__pycache__`，它不属于上游交付；若参与精确集合比较，任何跑过
        dry-run 或实盘的方案都无法再通过 StaticGate。
        """
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            # 模拟一次真实执行留下的 bytecode 缓存。
            for target in (scheme_dir, scheme_dir / "delivery"):
                cache_dir = target / "__pycache__"
                cache_dir.mkdir()
                (cache_dir / "delivery.cpython-312.pyc").write_bytes(b"\x00")
            config = load_scheme_config(scheme_dir / "config.yaml")
            result = BlackboxStaticGate().run(_context(root, config))

        self.assertTrue(result.passed, result.errors)

    def test_static_gate_still_rejects_unexpected_delivery_entry(self) -> None:
        """忽略 __pycache__ 不得放宽对其它多余交付文件的拒绝。"""
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )
            (scheme_dir / "delivery" / "extra.py").write_text(
                "", encoding="utf-8"
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            result = BlackboxStaticGate().run(_context(root, config))

        self.assertFalse(result.passed)
        self.assertIn(
            "delivery must contain exactly",
            "\n".join(result.errors),
        )

    def test_static_gate_records_declared_platform_input_provider(self) -> None:
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = intake_delivery(
                _delivery(root / "incoming"),
                schemes_root=root / "schemes",
                platform_inputs=["api-wind-date-v1"],
            )
            config = load_scheme_config(scheme_dir / "config.yaml")
            result = BlackboxStaticGate().run(_context(root, config))

        evidence = {item.key: item.value for item in result.evidence}
        self.assertTrue(result.passed, result.errors)
        self.assertEqual(
            evidence["platform_inputs"],
            ["api-wind-date-v1"],
        )

    def test_static_gate_rejects_network_and_database_imports(self) -> None:
        from harness.blackbox_v2.gates import BlackboxStaticGate
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _delivery(root / "incoming", script="import socket\nimport sqlalchemy\n")
            scheme_dir = intake_delivery(delivery, schemes_root=root / "schemes")
            config = load_scheme_config(scheme_dir / "config.yaml")
            result = BlackboxStaticGate().run(_context(root, config))

        self.assertFalse(result.passed)
        self.assertIn("forbidden import socket", "\n".join(result.errors))
        self.assertIn("forbidden import sqlalchemy", "\n".join(result.errors))


def _context(root: Path, config) -> GateContext:
    return GateContext(
        scheme_id="trial_10y",
        predict_date="2026-07-16",
        project_root=root,
        report_dir=root / "reports",
        config=config,
    )


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
    }


if __name__ == "__main__":
    unittest.main()
