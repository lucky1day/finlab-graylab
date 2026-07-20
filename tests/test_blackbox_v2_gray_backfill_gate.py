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


class BlackboxGrayBackfillGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_secret = os.environ.get("HARNESS_AUTH_SECRET")
        os.environ["HARNESS_AUTH_SECRET"] = "blackbox-gray-backfill-test-secret"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = self._scaffold_active_scheme()

    def tearDown(self) -> None:
        if self._previous_secret is None:
            os.environ.pop("HARNESS_AUTH_SECRET", None)
        else:
            os.environ["HARNESS_AUTH_SECRET"] = self._previous_secret
        self._tmp.cleanup()

    def _scaffold_active_scheme(self):
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        delivery = self.root / "incoming"
        delivery.mkdir()
        (delivery / "trial_10y.py").write_text("import argparse\n", encoding="utf-8")
        (delivery / "trial_10y.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "scheme_id": "trial_10y",
                    "name": "Trial",
                    "algorithm_version": "1.0.0",
                    "target_tenor": "10Y",
                    "task_type": "T+1",
                    "horizon": 1,
                    "target_rule": "target_date_yield_vs_feature_date_yield",
                }
            ),
            encoding="utf-8",
        )
        scheme_dir = intake_delivery(delivery, schemes_root=self.root / "schemes")
        config_path = scheme_dir / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            .replace("status: paused", "status: active")
            .replace("version_status: draft", "version_status: active"),
            encoding="utf-8",
        )
        return load_scheme_config(config_path)

    def _ctx(self, token: str, *, phase: str = "gray_live") -> GateContext:
        return GateContext(
            scheme_id=self.cfg.scheme_id,
            predict_date="2026-05-26",
            project_root=self.root,
            report_dir=self.root / "reports" / "gray-backfill",
            config=self.cfg,
            authorization=token,
            prediction_phase=phase,
            engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
        )

    def _token(self, *, action: str = "gray_backfill_write") -> str:
        from harness.authorization import issue_token

        return issue_token(
            self.cfg.scheme_id,
            action,
            "2026-05-26",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_latest",
            ttl_seconds=60,
            issued_by="gray-backfill-operator",
        )

    @staticmethod
    def _passed_run():
        return SimpleNamespace(harness_run_id="hr_latest")

    @staticmethod
    def _approval():
        return SimpleNamespace(executable=True, reason="approved")

    @staticmethod
    def _preflight():
        from harness.result import Evidence

        return (
            [
                Evidence("candidate_feature_date", "2026-05-25"),
                Evidence("candidate_target_date", "2026-05-26"),
                Evidence("historical_target_max", "2026-05-23"),
                Evidence("current_generation_id", "full-generation-a"),
                Evidence("current_refresh_date", "2026-07-20"),
                Evidence("existing_target_count", 0),
            ],
            [],
        )

    def test_gray_backfill_uses_distinct_action_and_historical_mode(self) -> None:
        from harness.gates.gray_backfill_gate import GrayBackfillGate

        before = {"t_scheme_runs": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0}
        after = {"t_scheme_runs": 1, "t_scheme_predictions": 1, "t_scheme_run_log": 1}
        run_result = SimpleNamespace(status="success", records_written=1, error_msg=None)
        with (
            patch.object(
                GrayBackfillGate,
                "_mode_preflight",
                return_value=self._preflight(),
                create=True,
            ),
            patch(
                "harness.gates.live_gate._verify_blackbox_passed_all",
                return_value=self._passed_run(),
            ),
            patch(
                "harness.gates.live_gate.read_blackbox_execution_approval",
                return_value=self._approval(),
            ),
            patch(
                "harness.gates.live_gate.snapshot_table_counts",
                side_effect=[before, after],
            ),
            patch(
                "harness.gates.live_gate.snapshot_scheme_counts",
                side_effect=[before, after],
            ),
            patch(
                "harness.gates.live_gate.execute_scheme",
                return_value=run_result,
            ) as execute,
        ):
            result = GrayBackfillGate().run(self._ctx(self._token()))

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(result.gate_name, "gray-backfill")
        self.assertEqual(execute.call_args.kwargs["prediction_phase"], "gray_live")
        self.assertEqual(
            execute.call_args.kwargs["blackbox_snapshot_mode"],
            "historical_as_of_replay",
        )
        self.assertEqual(
            execute.call_args.kwargs["blackbox_expected_generation_id"],
            "full-generation-a",
        )
        self.assertIn("blackbox_record_validator", execute.call_args.kwargs)
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["authorization_action"], "gray_backfill_write")
        self.assertEqual(evidence["existing_target_count"], 0)
        self.assertEqual(
            evidence["authorized_scheme_table_deltas"],
            {"t_scheme_runs": 1, "t_scheme_predictions": 1, "t_scheme_run_log": 1},
        )

    def test_live_and_gray_backfill_tokens_cannot_cross_gates(self) -> None:
        from harness.gates.gray_backfill_gate import GrayBackfillGate
        from harness.gates.live_gate import LiveGate

        with patch("harness.gates.live_gate.execute_scheme") as execute:
            gray_result = GrayBackfillGate().run(
                self._ctx(self._token(action="live_write"))
            )
            live_result = LiveGate().run(self._ctx(self._token()))

        self.assertFalse(gray_result.passed)
        self.assertFalse(live_result.passed)
        self.assertIn("action mismatch", "\n".join(gray_result.errors))
        self.assertIn("action mismatch", "\n".join(live_result.errors))
        execute.assert_not_called()

    def test_gray_backfill_requires_exact_gray_live_phase(self) -> None:
        from harness.gates.gray_backfill_gate import GrayBackfillGate

        with (
            patch.object(
                GrayBackfillGate,
                "_mode_preflight",
                return_value=self._preflight(),
                create=True,
            ),
            patch(
                "harness.gates.live_gate.snapshot_table_counts",
                return_value={"t_scheme_runs": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0},
            ),
            patch(
                "harness.gates.live_gate.snapshot_scheme_counts",
                return_value={"t_scheme_runs": 0, "t_scheme_predictions": 0, "t_scheme_run_log": 0},
            ),
            patch(
                "harness.gates.live_gate._verify_blackbox_passed_all",
                return_value=self._passed_run(),
            ),
            patch(
                "harness.gates.live_gate.read_blackbox_execution_approval",
                return_value=self._approval(),
            ),
            patch("harness.gates.live_gate.execute_scheme") as execute,
        ):
            result = GrayBackfillGate().run(
                self._ctx(self._token(), phase="scheduled_live")
            )

        self.assertFalse(result.passed)
        self.assertIn("requires prediction_phase=gray_live", "\n".join(result.errors))
        execute.assert_not_called()

    def test_preflight_rejects_non_historical_or_overlapping_target(self) -> None:
        from harness.gates.gray_backfill_gate import _gray_backfill_preflight

        cases = [
            (
                "not historical",
                {"generation_id": "full-test", "refresh_date": "2026-05-26"},
                ("2026-05-23", 0),
                "before current DataBridge refresh_date",
            ),
            (
                "history overlap",
                {"generation_id": "full-test", "refresh_date": "2026-07-20"},
                ("2026-05-26", 0),
                "after latest historical target",
            ),
            (
                "duplicate",
                {"generation_id": "full-test", "refresh_date": "2026-07-20"},
                ("2026-05-23", 1),
                "already exists",
            ),
            (
                "missing history",
                {"generation_id": "full-test", "refresh_date": "2026-07-20"},
                (None, 0),
                "latest historical backtest",
            ),
        ]
        metadata = SimpleNamespace(frequency="daily", horizon=1, target_tenor="10Y")
        context = SimpleNamespace(feature_date="2026-05-25", target_date="2026-05-26")
        for label, state, database_state, expected in cases:
            with self.subTest(label=label):
                with (
                    patch(
                        "harness.gates.gray_backfill_gate.read_blackbox_current_state",
                        return_value=state,
                        create=True,
                    ),
                    patch(
                        "harness.gates.gray_backfill_gate.load_metadata",
                        return_value=metadata,
                        create=True,
                    ),
                    patch(
                        "harness.gates.gray_backfill_gate.get_calendar",
                        return_value="calendar",
                        create=True,
                    ),
                    patch(
                        "harness.gates.gray_backfill_gate.build_daily_live_context",
                        return_value=context,
                        create=True,
                    ),
                    patch(
                        "harness.gates.gray_backfill_gate._read_gray_backfill_database_state",
                        return_value=database_state,
                        create=True,
                    ),
                ):
                    _evidence, errors = _gray_backfill_preflight(
                        self._ctx(self._token()),
                        self.cfg,
                        SimpleNamespace(),
                    )

                self.assertIn(expected, "\n".join(errors))

    def test_preflight_exception_disposes_engine(self) -> None:
        from harness.gates.gray_backfill_gate import GrayBackfillGate

        disposed = []
        engine = SimpleNamespace(dispose=lambda: disposed.append(True))
        ctx = replace(self._ctx(self._token()), engine_factory=lambda: engine)
        with patch.object(
            GrayBackfillGate,
            "_mode_preflight",
            side_effect=RuntimeError("preflight exploded"),
        ):
            result = GrayBackfillGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("preflight exploded", "\n".join(result.errors))
        self.assertEqual(disposed, [True])
        self.assertFalse(
            (self.root / "reports" / "harness" / ".used_authorization_tokens.json").exists()
        )

    def test_record_validator_rejects_incomplete_or_changed_provenance(self) -> None:
        from harness.gates.gray_backfill_gate import _validate_gray_backfill_records
        from shared.models import PredictionRecord

        record = PredictionRecord(
            scheme_id=self.cfg.scheme_id,
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-05-26",
            feature_date="2026-05-25",
            target_date="2026-05-26",
            predicted_direction=1,
            extra={
                "data_generation_id": "full-generation-b",
                "source_refresh_date": "2026-07-20",
                "daily_cutoff_key": "2026-05-25",
                "weekly_cutoff_key": "202621",
                "monthly_cutoff_key": "202605",
            },
        )

        with self.assertRaisesRegex(ValueError, "generation"):
            _validate_gray_backfill_records(
                [record],
                expected_generation_id="full-generation-a",
                expected_refresh_date="2026-07-20",
            )


if __name__ == "__main__":
    unittest.main()
