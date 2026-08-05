from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from harness.gates import signal_gap_fill_gate
from harness.signal_gap_plan import (
    PLAN_SCHEMA_VERSION,
    SignalGapPlanScope,
    canonical_plan_sha256,
)


class SignalGapFillPlanScopeTests(unittest.TestCase):
    def test_fill_replays_the_frozen_scope_during_preflight(self) -> None:
        action = {
            "registry_scheme_id": "alpha__h1__1Y",
            "base_scheme_id": "alpha",
            "runtime_type": "blackbox_v2",
            "frequency": "daily",
            "task_type": "T+1",
            "target_tenor": "1Y",
            "horizon": 1,
            "predict_date": "2026-08-04",
            "feature_date": "2026-08-03",
            "target_date": "2026-08-04",
            "segment": "live",
            "prediction_phase": "gray_live",
            "scheme_version": "version-1",
            "code_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "input_mode": "databridge_v1",
            "business_key_present": False,
            "action": "GRAY_LIVE_GAP",
            "input_authority": {
                "generation_id": "current-1",
                "refresh_date": "2026-08-06",
                "stable_identity_sha256": "c" * 64,
                "cutoff": {"daily_cutoff_key": "2026-08-03"},
            },
        }
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-08-04",
            "as_of_date": "2026-08-05",
            "selection": {
                "target_date_start": "2026-08-04",
                "target_date_end": "2026-08-05",
                "task_types": ["T+1"],
            },
            "actions": [action],
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)
        scopes: list[SignalGapPlanScope] = []

        class _Lock:
            def acquire(self) -> None:
                return None

            def release(self) -> None:
                return None

        def planner(*_args, **kwargs):
            scopes.append(kwargs["scope"])
            return payload

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(payload), encoding="utf-8")
            report = signal_gap_fill_gate.run_signal_gap_fill(
                plan_path=plan_path,
                authorizations=(),
                project_root=root,
                engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
                databridge_config=object(),
                planner=planner,
                config_loader=lambda _path: (_ for _ in ()).throw(
                    ValueError("stop before authorization")
                ),
                singleton_lock_factory=_Lock,
            )

        self.assertEqual(report["failure_code"], "DISCOVERY_IDENTITY_DRIFT")
        self.assertEqual(
            scopes,
            [
                SignalGapPlanScope(
                    target_date_start="2026-08-04",
                    target_date_end="2026-08-05",
                    task_types=("T+1",),
                )
            ],
        )

    def test_frozen_plan_restores_hash_bound_scope_for_replay(self) -> None:
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-08-04",
            "as_of_date": "2026-08-05",
            "selection": {
                "target_date_start": "2026-08-04",
                "target_date_end": "2026-08-05",
                "task_types": ["T+1"],
            },
            "actions": [],
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            frozen = signal_gap_fill_gate._load_frozen_plan(path)

        self.assertEqual(
            signal_gap_fill_gate._scope_from_frozen_plan(frozen),
            SignalGapPlanScope(
                target_date_start="2026-08-04",
                target_date_end="2026-08-05",
                task_types=("T+1",),
            ),
        )

    def test_frozen_plan_rejects_missing_or_malformed_selection(self) -> None:
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-08-04",
            "as_of_date": "2026-08-05",
            "actions": [],
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "scope is invalid",
            ):
                signal_gap_fill_gate._load_frozen_plan(path)

    def test_frozen_plan_rejects_noncanonical_selection_payload(self) -> None:
        payload = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "start_date": "2026-08-04",
            "as_of_date": "2026-08-05",
            "selection": {
                "target_date_start": "2026-08-04",
                "target_date_end": "2026-08-05",
                "task_types": ["T+1", "T+1"],
            },
            "actions": [],
        }
        payload["plan_sha256"] = canonical_plan_sha256(payload)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "plan.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "scope is invalid",
            ):
                signal_gap_fill_gate._load_frozen_plan(path)


if __name__ == "__main__":
    unittest.main()
