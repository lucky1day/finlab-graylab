from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_daily_coordinator_mvp_mysql import (
    _MVPMySQLServices,
    _controlled_executor_patches,
)
from tests.test_daily_real_replay_gate import (
    _generation_fixture,
)
from tests.test_daily_v2_coordinator_mysql import _MutableClock


class _AdvancingClock:
    def __init__(self, value: datetime) -> None:
        self._value = value
        self._lock = threading.Lock()

    def now_utc(self) -> datetime:
        with self._lock:
            self._value += timedelta(microseconds=1)
            return self._value


def _assert_replay_epoch(frozen, *, label: str, engine=None) -> None:
    del label
    if dict(frozen or {}) != {
        "epoch": 999_999,
        "mode": "ledger",
        "record_sha256": "9" * 64,
    }:
        raise RuntimeError("controlled replay epoch drifted")
    if engine is None:
        raise RuntimeError("controlled replay engine is missing")


@unittest.skipUnless(
    os.environ.get("BFL_DAILY_REAL_REPLAY_EXECUTE_MYSQL") == "1",
    "set BFL_DAILY_REAL_REPLAY_EXECUTE_MYSQL=1 for isolated execute proof",
)
class DailyRealReplayExecuteMySQLTests(unittest.TestCase):
    def test_controlled_execute_proves_25_29_receipts_and_reentry(
        self,
    ) -> None:
        from harness.daily_real_replay_mysql import (
            IsolatedReplayMySQL,
        )
        from harness.daily_real_replay_operator import (
            _ReplayDispatchIdentity,
            _execute_real_replay_on_isolated_database,
            _preflight_session,
            _read_replay_execution_audit,
        )
        from scheduler.daily_policy import POLICY_V2_PATH

        fixture = _generation_fixture()
        policy_sha = hashlib.sha256(
            POLICY_V2_PATH.read_bytes()
        ).hexdigest()
        with (
            tempfile.TemporaryDirectory() as generation_root,
            IsolatedReplayMySQL() as server,
        ):
            native, databridge = fixture._delivery_generation(
                Path(generation_root)
            )
            _schema, engine = server.create_replay_database()
            preflight = SimpleNamespace(
                qualification="EXCLUDED",
                expected_item_count=25,
                expected_target_count=29,
                v2_item_count=8,
                policy_version="daily-scheduler-policy-v2",
                policy_sha256=policy_sha,
                business_date="2026-07-24",
                feature_date="2026-07-23",
                native_generation_id=native.generation_id,
                native_manifest_sha256=native.manifest_sha256,
                databridge_generation_id=databridge.generation_id,
                databridge_manifest_sha256=(
                    databridge.manifest_sha256
                ),
                preflight_digest="9" * 64,
            )
            identity = _ReplayDispatchIdentity(
                service_uid=os.getuid(),
                business_date="2026-07-24",
                native_manifest_path=str(
                    native.manifest_path.resolve()
                ),
                databridge_manifest_path=str(
                    databridge.manifest_path.resolve()
                ),
                candidate_digest="1" * 64,
                generation_digest="2" * 64,
                definition_digest="3" * 64,
                control_plane_digest="4" * 64,
                production_digest="5" * 64,
                source_database_digest="6" * 64,
                source_watermark_digest="7" * 64,
                policy_version="daily-scheduler-policy-v2",
            )
            release_clock = _MutableClock(
                datetime.now(timezone.utc)
            )
            runtime_results = []
            audits = []
            worker_results = []

            def controlled_runtime(runtime, *, session):
                sealed_at = datetime.fromisoformat(
                    databridge.sealed_at.replace("Z", "+00:00")
                )
                release_clock.set(
                    sealed_at + timedelta(minutes=15)
                )
                event_clock = _AdvancingClock(
                    release_clock.now_utc()
                )
                services = _MVPMySQLServices(
                    engine=engine,
                    policy=runtime._policy,
                    clock=event_clock,
                    occurrence_id=runtime._occurrence_id,
                )
                real_execute = runtime._execute_owned_item

                def execute_checked(*args, **kwargs):
                    result = real_execute(*args, **kwargs)
                    worker_results.append(result)
                    return result

                with (
                    _controlled_executor_patches(
                        services,
                        event_clock,
                    ),
                    patch.object(
                        runtime,
                        "_assert_replay_process_boundary",
                    ),
                    patch.object(
                        runtime,
                        "_probe_replay_process_boundary",
                    ),
                    patch.object(
                        runtime,
                        "_execute_owned_item",
                        side_effect=execute_checked,
                    ),
                    patch(
                        "harness.daily_real_replay._now_utc",
                        side_effect=release_clock.now_utc,
                    ),
                    patch(
                        "harness.daily_real_replay_operator."
                        "assert_real_replay_dispatch_identity_current",
                        return_value=identity,
                        create=True,
                    ),
                    patch(
                        "scheduler.repository."
                        "assert_daily_coordinator_epoch_payload_matches_current",
                        side_effect=_assert_replay_epoch,
                    ),
                    patch(
                        "scheduler.scheduled_executor."
                        "assert_daily_coordinator_epoch_matches_policy",
                        side_effect=lambda policy_json, *, engine: (
                            _assert_replay_epoch(
                                policy_json.get(
                                    "daily_coordinator_epoch"
                                ),
                                label="daily occurrence coordinator epoch",
                                engine=engine,
                            )
                        ),
                    ),
                ):
                    result = runtime._run_with_operator_session(
                        session
                    )
                runtime_results.append(result)
                return result

            with (
                _preflight_session() as session,
                patch(
                    "harness.daily_real_replay_operator."
                    "_run_real_replay_runtime",
                    side_effect=controlled_runtime,
                ),
                patch(
                    "harness.daily_real_replay_operator."
                    "_stable_candidate_identity"
                ),
                patch(
                    "harness.daily_real_replay_operator."
                    "_read_replay_execution_audit",
                    side_effect=lambda *args, **kwargs: audits.append(
                        _read_replay_execution_audit(*args, **kwargs)
                    )
                    or audits[-1],
                ),
            ):
                session.bind_dispatch_identity(identity)
                try:
                    report = _execute_real_replay_on_isolated_database(
                        session,
                        preflight_report=preflight,
                        native_manifest=native.manifest_path,
                        databridge_manifest=databridge.manifest_path,
                        policy_path=POLICY_V2_PATH,
                        engine=engine,
                        isolation=server.database_isolation,
                    )
                except Exception as exc:
                    self.fail(
                        f"{exc}; runtime_results={runtime_results}; "
                        f"audits={audits}; "
                        f"worker_results={worker_results}"
                    )

        self.assertEqual(report.status, "REHEARSAL_PASSED")
        self.assertEqual(report.qualification, "REHEARSAL")
        self.assertEqual(report.capacity_qualification, "EXCLUDED")
        self.assertEqual(
            (
                report.successful_item_count,
                report.accepted_target_count,
                report.scheduled_live_run_count,
                report.scheduled_live_prediction_count,
                report.valid_receipt_count,
            ),
            (25, 29, 25, 29, 29),
        )
        self.assertEqual(report.duplicate_prediction_count, 0)
        self.assertEqual(report.nonterminal_run_count, 0)
        self.assertEqual(report.reentry_run_delta, 0)
        self.assertEqual(report.reentry_prediction_delta, 0)
        self.assertEqual(len(worker_results), 25)
        self.assertTrue(
            all(result.status == "success" for result in worker_results)
        )
        self.assertTrue(report.within_capacity_limit)
        self.assertTrue(report.within_visibility_deadline)
        self.assertTrue(
            report.projected_last_visible_at.endswith("+08:00")
        )
        self.assertEqual(
            report.v2_release_offsets_minutes,
            (0, 2, 4, 6, 8, 10, 12, 14),
        )
        self.assertEqual(
            report.v2_release_schedule_qualification,
            "SEALED_AT_PLUS_EXACT_POLICY_OFFSET",
        )


if __name__ == "__main__":
    unittest.main()
