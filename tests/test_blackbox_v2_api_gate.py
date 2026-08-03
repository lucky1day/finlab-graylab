from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.blackbox_v2.api_gate import BLACKBOX_BACKTEST_DATA_SOURCE, BlackboxApiGate
from harness.context import GateContext
from harness.probes.api_probe import DEFAULT_MAX_RESPONSE_BYTES


BASE_SCHEME_ID = "blackbox_trial"
REGISTRY_ID = "blackbox_trial__h1__10Y"
SCHEME_VERSION = "scheme-version"


class BlackboxApiGateTest(unittest.TestCase):
    def test_reads_backtest_card_using_verified_benchmark_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = _context(Path(tmpdir))
            with (
                patch(
                    "harness.blackbox_v2.api_gate._read_active_registry",
                    return_value=_registry_row(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_backtest_evidence",
                    return_value=_expected_backtest(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_live_evidence",
                    return_value=_expected_live(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.service_fingerprint_secret",
                    return_value=object(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=_service_instance(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.fetch_json",
                    side_effect=_successful_api_responses(),
                ) as fetch_json,
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertTrue(result.passed, result.errors)
        factor_lab_calls = _factor_lab_calls(fetch_json)
        self.assertEqual(len(factor_lab_calls), 1)
        self.assertEqual(
            factor_lab_calls[0].args[0],
            "http://api.test/api/backtests/factor-lab?benchmark_id=verified-benchmark",
        )
        self.assertEqual(
            factor_lab_calls[0].kwargs["max_response_bytes"],
            DEFAULT_MAX_RESPONSE_BYTES,
        )

    def test_fails_closed_without_persisted_backtest_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = _context(Path(tmpdir))
            with (
                patch(
                    "harness.blackbox_v2.api_gate._read_active_registry",
                    return_value=_registry_row(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_backtest_evidence",
                    side_effect=ValueError("persisted evidence is absent"),
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_live_evidence",
                    return_value=_expected_live(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.service_fingerprint_secret",
                    return_value=object(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=_service_instance(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.fetch_json",
                    side_effect=_successful_api_responses(),
                ) as fetch_json,
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertTrue(
            any("persisted backtest evidence probe failed" in error for error in result.errors),
            result.errors,
        )
        self.assertEqual(_factor_lab_calls(fetch_json), [])

    def test_fails_closed_when_persisted_evidence_has_blank_benchmark_id(self) -> None:
        expected_backtest = _expected_backtest()
        expected_backtest["benchmark_id"] = "   "
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = _context(Path(tmpdir))
            with (
                patch(
                    "harness.blackbox_v2.api_gate._read_active_registry",
                    return_value=_registry_row(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_backtest_evidence",
                    return_value=expected_backtest,
                ),
                patch(
                    "harness.blackbox_v2.api_gate._read_expected_live_evidence",
                    return_value=_expected_live(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.service_fingerprint_secret",
                    return_value=object(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.build_service_instance_identity",
                    return_value=_service_instance(),
                ),
                patch(
                    "harness.blackbox_v2.api_gate.fetch_json",
                    side_effect=_successful_api_responses(),
                ) as fetch_json,
            ):
                result = BlackboxApiGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertTrue(
            any(
                "persisted backtest evidence has no valid benchmark_id" in error
                for error in result.errors
            ),
            result.errors,
        )
        self.assertEqual(_factor_lab_calls(fetch_json), [])


def _context(project_root: Path) -> GateContext:
    metadata_path = project_root / "blackbox_trial.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scheme_id": BASE_SCHEME_ID,
                "name": "Blackbox trial",
                "algorithm_version": "1.0",
                "target_tenor": "10Y",
                "task_type": "T+1",
                "horizon": 1,
                "target_rule": "target_date_yield_vs_feature_date_yield",
            }
        ),
        encoding="utf-8",
    )
    config = SimpleNamespace(
        runtime_type="blackbox_v2",
        delivery_metadata=metadata_path,
        scheme_id=BASE_SCHEME_ID,
        runtime_profile="blackbox-v2-v1",
        scheme_version=SCHEME_VERSION,
    )
    engine = SimpleNamespace(dispose=lambda: None)
    return GateContext(
        scheme_id=BASE_SCHEME_ID,
        predict_date="2026-07-17",
        project_root=project_root,
        report_dir=project_root / "reports",
        config=config,
        engine_factory=lambda: engine,
        prediction_phase="gray_live",
        api_base_url="http://api.test",
        api_instance_nonce="unit-test-nonce",
    )


def _registry_row() -> dict[str, object]:
    return {
        "scheme_id": REGISTRY_ID,
        "base_scheme_id": BASE_SCHEME_ID,
        "target_tenor": "10Y",
        "task_type": "T+1",
        "horizon": 1,
        "status": "active",
        "runtime_type": "blackbox_v2",
    }


def _expected_backtest() -> dict[str, object]:
    return {
        "run_id": 456,
        "benchmark_id": "verified-benchmark",
        "scheme_version": SCHEME_VERSION,
        "harness_run_id": "harness-run-id",
        "data_snapshot_id": "backtest-snapshot",
    }


def _expected_live() -> dict[str, object]:
    return {
        "run_id": 123,
        "predict_date": "2026-07-17",
        "feature_date": "2026-07-16",
        "target_date": "2026-07-18",
        "request_id": "blackbox_trial:2026-07-17:2026-07-16:2026-07-18",
        "data_snapshot_id": "live-snapshot",
    }


def _service_instance() -> dict[str, str]:
    return {
        "fingerprint_version": "1",
        "fingerprint": "unit-test-fingerprint",
    }


def _successful_api_responses() -> list[tuple[dict[str, object], int]]:
    expected_live = _expected_live()
    daily_row = {
        "scheme_id": REGISTRY_ID,
        "base_scheme_id": BASE_SCHEME_ID,
        "target_tenor": "10Y",
        "horizon": 1,
        "prediction_phase": "gray_live",
        "scheme_version": SCHEME_VERSION,
        "predicted_direction": 1,
        "actual_direction": 1,
        "is_correct": True,
        **expected_live,
    }
    expected_backtest = _expected_backtest()
    return [
        ({"status": "ok", "service_instance": _service_instance()}, 200),
        ({"schemes": [_registry_row()]}, 200),
        (
            {
                "scheme_id": REGISTRY_ID,
                "base_scheme_id": BASE_SCHEME_ID,
                "target_tenor": "10Y",
                "task_type": "T+1",
                "daily_rows": [daily_row],
                "monthly_metrics": [{"metric_samples": 1}],
                "summary": {"metric_samples": 1},
            },
            200,
        ),
        (
            {
                "schemes": [
                    {
                        "scheme_id": REGISTRY_ID,
                        "base_scheme_id": BASE_SCHEME_ID,
                        "target_tenor": "10Y",
                        "task_type": "T+1",
                        "horizon": 1,
                        "data_source": BLACKBOX_BACKTEST_DATA_SOURCE,
                        "runtime_type": "blackbox_v2",
                        "daily_rows": [{"date": "2026-07-16"}],
                        "monthly_metrics": [{"month": "2026-07"}],
                        **expected_backtest,
                    }
                ]
            },
            200,
        ),
    ]


def _factor_lab_calls(fetch_json) -> list:
    return [
        call
        for call in fetch_json.call_args_list
        if "/api/backtests/factor-lab" in call.args[0]
    ]


if __name__ == "__main__":
    unittest.main()
