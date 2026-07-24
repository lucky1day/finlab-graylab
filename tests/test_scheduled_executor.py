from __future__ import annotations

import errno
import subprocess
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import (
    DBAPIError,
    DataError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
)

from shared.models import PredictionRecord


def _generation(
    *,
    generation_type: str = "native_source",
    generation_id: str = "native-20260724",
    manifest_sha256: str = "e" * 64,
):
    return SimpleNamespace(
        generation_id=generation_id,
        generation_type=generation_type,
        business_date="2026-07-24",
        feature_date="2026-07-23",
        manifest_uri=f"/frozen/{generation_id}/manifest.json",
        manifest_sha256=manifest_sha256,
        state="SEALED",
    )


def _envelope(*, runtime_type: str = "native_adapter"):
    generation = _generation()
    calendar_generation = generation
    input_compatibility = "generation_v1"
    if runtime_type == "blackbox_v2":
        input_compatibility = "databridge_v1"
        calendar_generation = _generation(
            generation_id="native-calendar-20260724",
            manifest_sha256="f" * 64,
        )
        generation = _generation(
            generation_type="databridge_v1",
            generation_id="databridge-20260724",
        )
    return SimpleNamespace(
        occurrence=SimpleNamespace(
            occurrence_id=7,
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            policy_json={
                "daily_coordinator_epoch": {
                    "epoch": 1,
                    "mode": "ledger",
                    "record_sha256": "1" * 64,
                },
                "schemes": [
                    {
                        "scheme_id": "alpha",
                        "cache_spec_fingerprint": None,
                        "input_compatibility": input_compatibility,
                    }
                ],
            },
        ),
        item=SimpleNamespace(
            item_id=11,
            base_scheme_id="alpha",
            runtime_type=runtime_type,
            scheme_version="alpha-v1",
            code_sha256="a" * 64,
            config_sha256="b" * 64,
            state="PENDING",
        ),
        generation=generation,
        calendar_generation=calendar_generation,
        targets=(
            SimpleNamespace(
                target_tenor="5Y",
                horizon=1,
                target_date="2026-07-27",
            ),
        ),
    )


def _config(*, runtime_type: str = "native_adapter"):
    return SimpleNamespace(
        scheme_id="alpha",
        runtime_type=runtime_type,
        scheme_version="alpha-v1",
        code_hash="a" * 64,
        config_hash="b" * 64,
        frequency="daily",
        status="active",
        schedule=SimpleNamespace(timeout_sec=None),
    )


def _record() -> PredictionRecord:
    return PredictionRecord(
        scheme_id="alpha",
        target_tenor="5Y",
        horizon=1,
        predict_date="2026-07-24",
        feature_date="2026-07-23",
        target_date="2026-07-27",
        predicted_direction=1,
    )


def _cache_qualification():
    from shared.liwei_0616_cache_contract import (
        cache_use_qualification_sha256,
    )
    from shared.liwei_0616_phase_a_cache import PhaseACacheSpec
    from tests.test_liwei_0616_phase_a_cache_generations import (
        Liwei0616ImmutableCacheGenerationTests,
    )

    spec = PhaseACacheSpec(
        cache_family="liwei_0616_5y_v31",
        tenor="5Y",
        baselines=("STD",),
        baseline_configs={"STD": {"window": 200}},
        source_ic_screen_start="2024-01-01",
        horizon=5,
        purge_gap=5,
    )
    trusted = (
        Liwei0616ImmutableCacheGenerationTests._trusted_qualification(
            spec,
            base_scheme_id="alpha",
        )
    )
    trusted["qualification"]["scheme_version"] = "alpha-v1"
    trusted["qualification_sha256"] = cache_use_qualification_sha256(
        trusted["qualification"]
    )
    return spec, trusted


class _MySqlDriverError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        self.errno = code
        super().__init__(code, message)


def _operational_error(code: int, message: str) -> OperationalError:
    return OperationalError(
        "SELECT 1",
        {},
        _MySqlDriverError(code, message),
    )


class ScheduledExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._epoch_patcher = patch(
            "scheduler.scheduled_executor."
            "assert_daily_coordinator_epoch_matches_policy",
            return_value=SimpleNamespace(),
        )
        self._epoch_patcher.start()

    def tearDown(self) -> None:
        self._epoch_patcher.stop()

    def test_epoch_drift_rejects_before_attempt_claim(self) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        envelope = _envelope()
        with (
            patch(
                "scheduler.scheduled_executor."
                "read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor."
                "assert_daily_coordinator_epoch_matches_policy",
                side_effect=RuntimeError("epoch drift"),
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
            ) as claim,
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
            ) as run_scheme,
        ):
            result = execute_scheduled_item(object(), item_id=11)

        self.assertEqual(result.status, "claim_rejected")
        self.assertIsNone(result.run_id)
        claim.assert_not_called()
        run_scheme.assert_not_called()

    def test_cache_qualified_record_missing_extra_is_rejected_precommit(
        self,
    ) -> None:
        from scheduler.scheduled_executor import (
            ScheduledResultError,
            _frozen_cache_use_qualification,
            _verify_cache_qualified_records,
        )

        spec, trusted = _cache_qualification()
        envelope = _envelope()
        envelope.item.cache_group = (
            f"{spec.cache_family}:{spec.tenor}"
        )
        envelope.generation.dataset_content_id = "d" * 64
        envelope.generation.schema_version = "native-generation-v1"
        envelope.generation.exporter_version = (
            "native-generation-exporter-v1"
        )
        envelope.occurrence.policy_json = {
            "capacity_candidate_fingerprint": "8" * 64,
            "schemes": [
                {
                    "scheme_id": "alpha",
                    "cache_spec_fingerprint":
                        trusted["qualification"]["spec_fingerprint"],
                }
            ],
            "cache_use_qualifications": {"alpha": trusted},
        }
        frozen = _frozen_cache_use_qualification(envelope)
        self.assertEqual(frozen, trusted)
        with self.assertRaisesRegex(
            ScheduledResultError,
            "cache-qualified prediction audit",
        ):
            _verify_cache_qualified_records(
                [_record()],
                envelope=envelope,
                expected_qualification=frozen,
            )

    def test_cache_qualification_wrong_consumer_is_rejected(self) -> None:
        from scheduler.scheduled_executor import (
            ScheduledContractError,
            _frozen_cache_use_qualification,
        )

        spec, trusted = _cache_qualification()
        envelope = _envelope()
        envelope.item.cache_group = (
            f"{spec.cache_family}:{spec.tenor}"
        )
        envelope.occurrence.policy_json = {
            "capacity_candidate_fingerprint": "8" * 64,
            "schemes": [
                {
                    "scheme_id": "alpha",
                    "cache_spec_fingerprint":
                        trusted["qualification"]["spec_fingerprint"],
                }
            ],
            "cache_use_qualifications": {"other": trusted},
        }
        with self.assertRaisesRegex(
            ScheduledContractError,
            "missing frozen qualification",
        ):
            _frozen_cache_use_qualification(envelope)

    def _execute_failure(
        self,
        error: Exception,
        *,
        runtime_type: str = "native_adapter",
        phase: str = "algorithm",
    ):
        from scheduler.scheduled_executor import execute_scheduled_item

        envelope = _envelope(runtime_type=runtime_type)
        attempt = SimpleNamespace(
            item_id=11,
            run_id=990,
            attempt_no=1,
            execution_token="classification-token",
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "scheduler.scheduled_executor."
                    "read_schedule_execution_envelope",
                    return_value=envelope,
                )
            )
            stack.enter_context(
                patch(
                    "scheduler.scheduled_executor.start_schedule_attempt",
                    return_value=attempt,
                )
            )
            stack.enter_context(
                patch(
                    "scheduler.scheduled_executor.load_scheme_config",
                    return_value=_config(runtime_type=runtime_type),
                )
            )
            stack.enter_context(
                patch(
                    "scheduler.scheduled_executor."
                    "open_native_generation",
                    return_value=SimpleNamespace(),
                )
            )
            if runtime_type == "blackbox_v2":
                stack.enter_context(
                    patch(
                        "scheduler.scheduled_executor."
                        "open_databridge_generation",
                        return_value=SimpleNamespace(),
                    )
                )
            if phase == "algorithm":
                stack.enter_context(
                    patch(
                        "scheduler.scheduled_executor."
                        "run_configured_scheme",
                        side_effect=error,
                    )
                )
            elif phase == "completion":
                stack.enter_context(
                    patch(
                        "scheduler.scheduled_executor."
                        "run_configured_scheme",
                        return_value=[_record()],
                    )
                )
                stack.enter_context(
                    patch(
                        "scheduler.scheduled_executor."
                        "complete_scheduled_attempt",
                        side_effect=error,
                    )
                )
            else:
                raise ValueError(f"unsupported phase: {phase}")
            retry = stack.enter_context(
                patch(
                    "scheduler.scheduled_executor."
                    "mark_schedule_attempt_retry_wait",
                    return_value="RETRY_WAIT",
                )
            )
            terminal = stack.enter_context(
                patch(
                    "scheduler.scheduled_executor."
                    "mark_schedule_attempt_terminal_failure",
                    return_value="FAILED_TERMINAL",
                )
            )
            result = execute_scheduled_item(object(), item_id=11)
        return result, retry, terminal

    def test_claim_race_returns_structured_rejection_without_a_run(
        self,
    ) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        envelope = _envelope()
        with (
            patch(
                "scheduler.scheduled_executor."
                "read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                side_effect=RuntimeError(
                    "schedule occurrence first-attempt barrier is not "
                    "satisfied"
                ),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
            ) as run_scheme,
        ):
            result = execute_scheduled_item(object(), item_id=11)

        self.assertEqual(result.status, "claim_rejected")
        self.assertEqual(result.failure_code, "STALE_ATTEMPT")
        self.assertIsNone(result.run_id)
        self.assertIsNone(result.attempt_no)
        run_scheme.assert_not_called()

    def test_process_start_after_cutoff_is_audited_as_expired(
        self,
    ) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        engine = object()
        envelope = _envelope()
        attempt = SimpleNamespace(
            item_id=11,
            run_id=911,
            attempt_no=1,
            execution_token="cutoff-token",
        )

        def run_scheme(*_args, **kwargs):
            kwargs["process_started"](43211, 43211)
            raise AssertionError("cutoff callback must stop execution")

        with (
            patch(
                "scheduler.scheduled_executor."
                "read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                side_effect=run_scheme,
            ),
            patch(
                "scheduler.scheduled_executor."
                "register_schedule_attempt_process",
                side_effect=RuntimeError(
                    "schedule process cannot start at/after recovery "
                    "cutoff"
                ),
            ),
            patch(
                "scheduler.scheduled_executor."
                "mark_schedule_attempt_terminal_failure",
                return_value="EXPIRED",
            ) as terminal,
        ):
            result = execute_scheduled_item(engine, item_id=11)

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.failure_code,
            "RECOVERY_CUTOFF_EXPIRED",
        )
        terminal.assert_called_once()
        self.assertEqual(
            terminal.call_args.kwargs["failure_code"],
            "RECOVERY_CUTOFF_EXPIRED",
        )

    def test_process_callback_registers_current_attempt_identity(
        self,
    ) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        engine = object()
        envelope = _envelope()
        attempt = SimpleNamespace(
            item_id=11,
            run_id=909,
            attempt_no=3,
            execution_token="current-attempt-token",
        )

        def run_scheme(*_args, **kwargs):
            kwargs["process_started"](43210, 43210)
            return [_record()]

        with (
            patch(
                "scheduler.scheduled_executor.read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                side_effect=run_scheme,
            ),
            patch(
                "scheduler.scheduled_executor.register_schedule_attempt_process",
                create=True,
            ) as register,
            patch(
                "scheduler.scheduled_executor.complete_scheduled_attempt",
                return_value=1,
            ),
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_terminal_failure",
                return_value="FAILED_TERMINAL",
            ),
        ):
            result = execute_scheduled_item(
                engine,
                item_id=11,
                trigger_origin="auto_retry",
            )

        self.assertEqual(result.status, "success")
        register.assert_called_once_with(
            engine,
            run_id=909,
            execution_token="current-attempt-token",
            process_id=43210,
            process_group_id=43210,
        )

    def test_confirmed_callback_cleanup_allows_transient_retry(self) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        engine = object()
        envelope = _envelope()
        attempt = SimpleNamespace(
            item_id=11,
            run_id=910,
            attempt_no=1,
            execution_token="db-error-token",
        )
        error = OperationalError(
            "register scheduled process",
            {},
            _MySqlDriverError(
                1213,
                "process registration deadlock after confirmed cleanup",
            ),
        )
        with (
            patch(
                "scheduler.scheduled_executor.read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                side_effect=error,
            ),
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_retry_wait",
                return_value="RETRY_WAIT",
            ) as retry,
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_terminal_failure",
            ) as terminal,
            patch(
                "scheduler.scheduled_executor."
                "fence_current_schedule_attempt",
                create=True,
            ) as fence,
        ):
            result = execute_scheduled_item(engine, item_id=11)

        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(result.failure_code, "TRANSIENT_INFRA")
        retry.assert_called_once()
        terminal.assert_not_called()
        fence.assert_not_called()

    def test_unconfirmed_callback_cleanup_fences_before_any_retry(
        self,
    ) -> None:
        from scheduler.process_control import (
            ProcessGroupTerminationResult,
            ProcessRegistrationCleanupError,
        )
        from scheduler.scheduled_executor import execute_scheduled_item

        engine = object()
        envelope = _envelope()
        attempt = SimpleNamespace(
            item_id=11,
            run_id=912,
            attempt_no=1,
            execution_token="cleanup-pending-token",
        )
        cleanup_error = ProcessRegistrationCleanupError(
            registration_error=OperationalError(
                "register scheduled process",
                {},
                RuntimeError("database unavailable"),
            ),
            termination=ProcessGroupTerminationResult(
                process_id=43212,
                process_group_id=43212,
                term_sent=True,
                kill_sent=True,
                confirmed_gone=False,
                failure_reason="process group still exists after SIGKILL",
            ),
        )
        fenced_attempt = SimpleNamespace(
            item_id=11,
            run_id=912,
            execution_token="cleanup-pending-token",
            process_id=None,
            process_group_id=None,
        )
        with (
            patch(
                "scheduler.scheduled_executor."
                "read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                side_effect=cleanup_error,
            ),
            patch(
                "scheduler.scheduled_executor."
                "fence_current_schedule_attempt",
                create=True,
                return_value=fenced_attempt,
            ) as fence,
            patch(
                "scheduler.scheduled_executor."
                "mark_schedule_attempt_retry_wait",
            ) as retry,
            patch(
                "scheduler.scheduled_executor."
                "mark_schedule_attempt_terminal_failure",
            ) as terminal,
        ):
            result = execute_scheduled_item(engine, item_id=11)

        self.assertEqual(result.status, "fenced_pending_cleanup")
        self.assertEqual(
            result.failure_code,
            "ABANDONED_FENCE_PENDING_CLEANUP",
        )
        fence.assert_called_once_with(engine, item_id=11)
        retry.assert_not_called()
        terminal.assert_not_called()

    def test_fence_write_failure_leaves_attempt_running_and_not_retryable(
        self,
    ) -> None:
        from scheduler.process_control import (
            ProcessGroupTerminationResult,
            ProcessRegistrationCleanupError,
        )
        from scheduler.scheduled_executor import execute_scheduled_item

        engine = object()
        envelope = _envelope(runtime_type="blackbox_v2")
        attempt = SimpleNamespace(
            item_id=11,
            run_id=913,
            attempt_no=1,
            execution_token="fence-write-failed-token",
        )
        cleanup_error = ProcessRegistrationCleanupError(
            registration_error=OperationalError(
                "register scheduled process",
                {},
                RuntimeError("database unavailable"),
            ),
            termination=ProcessGroupTerminationResult(
                process_id=43213,
                process_group_id=43213,
                term_sent=True,
                kill_sent=True,
                confirmed_gone=False,
                failure_reason="process group still exists after SIGKILL",
            ),
        )
        fence_error = OperationalError(
            "UPDATE t_schedule_items",
            {},
            RuntimeError("database still unavailable"),
        )
        with (
            patch(
                "scheduler.scheduled_executor."
                "read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(runtime_type="blackbox_v2"),
            ),
            patch(
                "scheduler.scheduled_executor."
                "open_databridge_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                side_effect=cleanup_error,
            ),
            patch(
                "scheduler.scheduled_executor."
                "fence_current_schedule_attempt",
                create=True,
                side_effect=fence_error,
            ) as fence,
            patch(
                "scheduler.scheduled_executor."
                "mark_schedule_attempt_retry_wait",
            ) as retry,
            patch(
                "scheduler.scheduled_executor."
                "mark_schedule_attempt_terminal_failure",
            ) as terminal,
        ):
            result = execute_scheduled_item(engine, item_id=11)

        self.assertEqual(result.status, "recovery_blocked")
        self.assertIsNone(result.failure_code)
        self.assertIn(
            "cleanup fence could not be persisted",
            result.error_message or "",
        )
        fence.assert_called_once_with(engine, item_id=11)
        retry.assert_not_called()
        terminal.assert_not_called()

    def test_native_reads_only_frozen_envelope_and_commits_atomically(
        self,
    ) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        envelope = _envelope()
        attempt = SimpleNamespace(
            item_id=11,
            run_id=101,
            attempt_no=1,
            execution_token="attempt-token",
        )
        native_context = SimpleNamespace(
            generation_id="native-20260724",
            manifest_sha256="e" * 64,
            business_date="2026-07-24",
            feature_date="2026-07-23",
        )
        with (
            patch(
                "scheduler.scheduled_executor.read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=native_context,
            ) as open_native,
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                return_value=[_record()],
            ) as run_scheme,
            patch(
                "scheduler.scheduled_executor.complete_scheduled_attempt",
                return_value=1,
            ) as complete,
        ):
            result = execute_scheduled_item(
                object(),
                item_id=11,
                trigger_origin="apscheduler",
            )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.run_id, 101)
        self.assertEqual(result.records_written, 1)
        open_native.assert_called_once()
        self.assertIs(
            run_scheme.call_args.kwargs["native_generation"],
            native_context,
        )
        self.assertFalse(
            run_scheme.call_args.kwargs.get(
                "live_source_compatibility",
                False,
            )
        )
        self.assertEqual(
            run_scheme.call_args.kwargs["execution_token"],
            "attempt-token",
        )
        committed = list(complete.call_args.kwargs["records"])
        self.assertEqual(committed[0].prediction_phase, "scheduled_live")
        self.assertEqual(committed[0].run_id, 101)
        self.assertEqual(committed[0].scheme_version, "alpha-v1")
        self.assertEqual(
            committed[0].extra["input_generation_id"],
            "native-20260724",
        )

    def test_approved_0629_uses_live_source_mode_with_frozen_fence(
        self,
    ) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        scheme_id = "daily_1y_xgb_1y13_0629"
        envelope = _envelope()
        envelope.item.base_scheme_id = scheme_id
        envelope.item.scheme_version = "0629-v1"
        envelope.occurrence.policy_json["schemes"] = [
            {
                "scheme_id": scheme_id,
                "cache_spec_fingerprint": None,
                "input_compatibility": "live_source_0629",
                "source_package_sha256": "c" * 64,
            }
        ]
        config = _config()
        config.scheme_id = scheme_id
        config.scheme_version = "0629-v1"
        attempt = SimpleNamespace(
            item_id=11,
            run_id=201,
            attempt_no=1,
            execution_token="compat-token",
        )
        native_context = SimpleNamespace(
            generation_id="native-20260724",
            manifest_sha256="e" * 64,
            business_date="2026-07-24",
            feature_date="2026-07-23",
        )
        record = PredictionRecord(
            scheme_id=scheme_id,
            target_tenor="5Y",
            horizon=1,
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_date="2026-07-27",
            predicted_direction=1,
            extra={
                "input_mode": "live_source_0629",
                "data_watermark": "2026-07-23",
                "data_watermark_basis":
                    "source_output_prediction_date",
                "live_source_fence_generation_id":
                    "native-20260724",
                "source_package_hash": "c" * 64,
            },
        )

        with (
            patch(
                "scheduler.scheduled_executor."
                "read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=config,
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=native_context,
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                return_value=[record],
            ) as run_scheme,
            patch(
                "scheduler.scheduled_executor.complete_scheduled_attempt",
                return_value=1,
            ),
        ):
            result = execute_scheduled_item(object(), item_id=11)

        self.assertEqual(result.status, "success")
        self.assertTrue(
            run_scheme.call_args.kwargs["live_source_compatibility"]
        )
        self.assertIs(
            run_scheme.call_args.kwargs["native_generation"],
            native_context,
        )
        self.assertEqual(
            run_scheme.call_args.kwargs[
                "live_source_package_sha256"
            ],
            "c" * 64,
        )

    def test_live_source_audit_rejects_source_package_drift(
        self,
    ) -> None:
        from scheduler.scheduled_executor import (
            ScheduledResultError,
            _verify_input_compatibility_records,
        )

        scheme_id = "daily_1y_xgb_1y13_0629"
        envelope = _envelope()
        envelope.item.base_scheme_id = scheme_id
        envelope.occurrence.policy_json["schemes"] = [
            {
                "scheme_id": scheme_id,
                "cache_spec_fingerprint": None,
                "input_compatibility": "live_source_0629",
                "source_package_sha256": "c" * 64,
            }
        ]
        record = PredictionRecord(
            scheme_id=scheme_id,
            target_tenor="1Y",
            horizon=1,
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_date="2026-07-27",
            predicted_direction=1,
            extra={
                "input_mode": "live_source_0629",
                "data_watermark": "2026-07-23",
                "data_watermark_basis":
                    "source_output_prediction_date",
                "live_source_fence_generation_id":
                    "native-20260724",
                "source_package_hash": "d" * 64,
            },
        )

        with self.assertRaisesRegex(
            ScheduledResultError,
            "source_package_hash",
        ):
            _verify_input_compatibility_records(
                [record],
                envelope=envelope,
                input_compatibility="live_source_0629",
            )

    def test_live_source_audit_rejects_watermark_basis_drift(
        self,
    ) -> None:
        from scheduler.scheduled_executor import (
            ScheduledResultError,
            _verify_input_compatibility_records,
        )

        scheme_id = "daily_1y_xgb_1y13_0629"
        envelope = _envelope()
        envelope.item.base_scheme_id = scheme_id
        envelope.occurrence.policy_json["schemes"] = [
            {
                "scheme_id": scheme_id,
                "cache_spec_fingerprint": None,
                "input_compatibility": "live_source_0629",
                "source_package_sha256": "c" * 64,
            }
        ]
        record = PredictionRecord(
            scheme_id=scheme_id,
            target_tenor="1Y",
            horizon=1,
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_date="2026-07-27",
            predicted_direction=1,
            extra={
                "input_mode": "live_source_0629",
                "data_watermark": "2026-07-23",
                "data_watermark_basis": "forged_basis",
                "live_source_fence_generation_id":
                    "native-20260724",
                "source_package_hash": "c" * 64,
            },
        )

        with self.assertRaisesRegex(
            ScheduledResultError,
            "data_watermark_basis",
        ):
            _verify_input_compatibility_records(
                [record],
                envelope=envelope,
                input_compatibility="live_source_0629",
            )

    def test_unapproved_live_source_policy_fails_closed(self) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        envelope = _envelope()
        envelope.occurrence.policy_json["schemes"][0][
            "input_compatibility"
        ] = "live_source_0629"
        attempt = SimpleNamespace(
            item_id=11,
            run_id=202,
            attempt_no=1,
            execution_token="forged-compat-token",
        )
        with (
            patch(
                "scheduler.scheduled_executor."
                "read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
            ) as run_scheme,
            patch(
                "scheduler.scheduled_executor."
                "mark_schedule_attempt_terminal_failure",
                return_value="FAILED_TERMINAL",
            ),
        ):
            result = execute_scheduled_item(object(), item_id=11)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_code, "CONTRACT")
        run_scheme.assert_not_called()

    def test_v2_reopens_bound_databridge_and_its_frozen_native_calendar(
        self,
    ) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        envelope = _envelope(runtime_type="blackbox_v2")
        attempt = SimpleNamespace(
            item_id=11,
            run_id=102,
            attempt_no=1,
            execution_token="v2-token",
        )
        databridge_context = SimpleNamespace(
            generation_id="databridge-20260724",
            manifest_sha256="e" * 64,
            business_date="2026-07-24",
            feature_date="2026-07-23",
        )
        calendar_context = SimpleNamespace(
            generation_id="native-calendar-20260724",
            manifest_sha256="f" * 64,
            business_date="2026-07-24",
            feature_date="2026-07-23",
        )
        with (
            patch(
                "scheduler.scheduled_executor.read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(runtime_type="blackbox_v2"),
            ),
            patch(
                "scheduler.scheduled_executor.open_databridge_generation",
                return_value=databridge_context,
            ) as open_databridge,
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=calendar_context,
            ) as open_native,
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                return_value=[_record()],
            ) as run_scheme,
            patch(
                "scheduler.scheduled_executor.complete_scheduled_attempt",
                return_value=1,
            ),
        ):
            result = execute_scheduled_item(
                object(),
                item_id=11,
                default_timeout_sec=999,
            )

        self.assertEqual(result.status, "success")
        open_databridge.assert_called_once()
        open_native.assert_called_once()
        self.assertIs(
            run_scheme.call_args.kwargs["databridge_generation"],
            databridge_context,
        )
        self.assertIs(
            run_scheme.call_args.kwargs["calendar_generation"],
            calendar_context,
        )
        self.assertEqual(run_scheme.call_args.kwargs["timeout_sec"], 120)

    def test_frozen_scheme_drift_is_terminal_contract_failure(self) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        envelope = _envelope()
        attempt = SimpleNamespace(
            item_id=11,
            run_id=103,
            attempt_no=1,
            execution_token="drift-token",
        )
        drifted = _config()
        drifted.code_hash = "c" * 64
        with (
            patch(
                "scheduler.scheduled_executor.read_schedule_execution_envelope",
                return_value=envelope,
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=drifted,
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
            ) as run_scheme,
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_terminal_failure",
                return_value="FAILED_TERMINAL",
            ) as terminal,
        ):
            result = execute_scheduled_item(object(), item_id=11)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failure_code, "CONTRACT")
        run_scheme.assert_not_called()
        self.assertEqual(
            terminal.call_args.kwargs["failure_code"],
            "CONTRACT",
        )

    def test_timeout_is_terminal_and_is_never_immediate_retry(self) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        attempt = SimpleNamespace(
            item_id=11,
            run_id=104,
            attempt_no=1,
            execution_token="timeout-token",
        )
        with (
            patch(
                "scheduler.scheduled_executor.read_schedule_execution_envelope",
                return_value=_envelope(),
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                side_effect=subprocess.TimeoutExpired(["algo"], 600),
            ),
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_retry_wait",
            ) as retry,
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_terminal_failure",
                return_value="FAILED_TERMINAL",
            ) as terminal,
        ):
            result = execute_scheduled_item(object(), item_id=11)

        self.assertEqual(result.failure_code, "TIMEOUT")
        retry.assert_not_called()
        self.assertEqual(
            terminal.call_args.kwargs["failure_code"],
            "TIMEOUT",
        )

    def test_transient_infrastructure_failure_waits_for_coordinator_retry(
        self,
    ) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        attempt = SimpleNamespace(
            item_id=11,
            run_id=105,
            attempt_no=1,
            execution_token="infra-token",
        )
        with (
            patch(
                "scheduler.scheduled_executor.read_schedule_execution_envelope",
                return_value=_envelope(),
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                side_effect=BlockingIOError(
                    errno.EAGAIN,
                    "temporary process launch failure",
                ),
            ),
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_retry_wait",
                return_value="RETRY_WAIT",
            ) as retry,
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_terminal_failure",
            ) as terminal,
        ):
            result = execute_scheduled_item(object(), item_id=11)

        self.assertEqual(result.status, "retry_wait")
        self.assertEqual(result.failure_code, "TRANSIENT_INFRA")
        terminal.assert_not_called()
        self.assertEqual(
            retry.call_args.kwargs["failure_code"],
            "TRANSIENT_INFRA",
        )

    def test_dbapi_failures_use_narrow_transient_allowlist(self) -> None:
        cases = (
            (
                "integrity",
                IntegrityError(
                    "INSERT prediction",
                    {},
                    _MySqlDriverError(1062, "duplicate key"),
                ),
                "RESULT",
                False,
            ),
            (
                "data",
                DataError(
                    "INSERT prediction",
                    {},
                    _MySqlDriverError(1264, "out of range"),
                ),
                "RESULT",
                False,
            ),
            (
                "programming",
                ProgrammingError(
                    "SELECT missing_column",
                    {},
                    _MySqlDriverError(1054, "unknown column"),
                ),
                "CONTRACT",
                False,
            ),
            (
                "deadlock",
                _operational_error(1213, "deadlock"),
                "TRANSIENT_INFRA",
                True,
            ),
            (
                "lock_timeout",
                _operational_error(1205, "lock wait timeout"),
                "TRANSIENT_INFRA",
                True,
            ),
            (
                "disconnect",
                _operational_error(2013, "lost connection"),
                "TRANSIENT_INFRA",
                True,
            ),
            (
                "unknown_operational",
                _operational_error(9999, "unknown driver failure"),
                "CONTRACT",
                False,
            ),
            (
                "unknown_dbapi",
                DBAPIError(
                    "SELECT 1",
                    {},
                    _MySqlDriverError(9998, "unknown DBAPI failure"),
                    False,
                ),
                "CONTRACT",
                False,
            ),
        )
        for label, error, expected_code, retryable in cases:
            with self.subTest(label=label):
                result, retry, terminal = self._execute_failure(error)

                self.assertEqual(result.failure_code, expected_code)
                if retryable:
                    retry.assert_called_once()
                    terminal.assert_not_called()
                else:
                    retry.assert_not_called()
                    terminal.assert_called_once()

    def test_native_and_v2_completion_share_fail_closed_db_classifier(
        self,
    ) -> None:
        for runtime_type in ("native_adapter", "blackbox_v2"):
            for error, expected_code, retryable in (
                (
                    IntegrityError(
                        "INSERT prediction",
                        {},
                        _MySqlDriverError(1062, "duplicate key"),
                    ),
                    "RESULT",
                    False,
                ),
                (
                    ProgrammingError(
                        "SELECT missing_column",
                        {},
                        _MySqlDriverError(1054, "unknown column"),
                    ),
                    "CONTRACT",
                    False,
                ),
                (
                    _operational_error(1213, "deadlock"),
                    "TRANSIENT_INFRA",
                    True,
                ),
                (
                    _operational_error(
                        9999,
                        "unknown operational error",
                    ),
                    "CONTRACT",
                    False,
                ),
            ):
                with self.subTest(
                    runtime_type=runtime_type,
                    error_type=type(error).__name__,
                ):
                    result, retry, terminal = self._execute_failure(
                        error,
                        runtime_type=runtime_type,
                        phase="completion",
                    )

                    self.assertEqual(result.failure_code, expected_code)
                    if retryable:
                        retry.assert_called_once()
                        terminal.assert_not_called()
                    else:
                        retry.assert_not_called()
                        terminal.assert_called_once()

    def test_os_errors_retry_only_explicit_transient_errno(self) -> None:
        cases = (
            (
                "permission",
                PermissionError(errno.EACCES, "permission denied"),
                "CONTRACT",
                False,
            ),
            (
                "disk_full",
                OSError(errno.ENOSPC, "disk full"),
                "CONTRACT",
                False,
            ),
            (
                "invalid",
                OSError(errno.EINVAL, "invalid argument"),
                "CONTRACT",
                False,
            ),
            (
                "unknown",
                OSError(9999, "unknown OS failure"),
                "CONTRACT",
                False,
            ),
            (
                "again",
                BlockingIOError(errno.EAGAIN, "try again"),
                "TRANSIENT_INFRA",
                True,
            ),
            (
                "interrupted",
                InterruptedError(errno.EINTR, "interrupted"),
                "TRANSIENT_INFRA",
                True,
            ),
            (
                "os_timeout",
                TimeoutError(errno.ETIMEDOUT, "connection timed out"),
                "TRANSIENT_INFRA",
                True,
            ),
            (
                "connection_reset",
                ConnectionResetError(
                    errno.ECONNRESET,
                    "connection reset",
                ),
                "TRANSIENT_INFRA",
                True,
            ),
        )
        for label, error, expected_code, retryable in cases:
            with self.subTest(label=label):
                result, retry, terminal = self._execute_failure(error)

                self.assertEqual(result.failure_code, expected_code)
                if retryable:
                    retry.assert_called_once()
                    terminal.assert_not_called()
                else:
                    retry.assert_not_called()
                    terminal.assert_called_once()

    def test_atomic_completion_rejection_is_result_failure(self) -> None:
        from scheduler.scheduled_executor import execute_scheduled_item

        attempt = SimpleNamespace(
            item_id=11,
            run_id=106,
            attempt_no=1,
            execution_token="result-token",
        )
        with (
            patch(
                "scheduler.scheduled_executor.read_schedule_execution_envelope",
                return_value=_envelope(),
            ),
            patch(
                "scheduler.scheduled_executor.start_schedule_attempt",
                return_value=attempt,
            ),
            patch(
                "scheduler.scheduled_executor.load_scheme_config",
                return_value=_config(),
            ),
            patch(
                "scheduler.scheduled_executor.open_native_generation",
                return_value=SimpleNamespace(),
            ),
            patch(
                "scheduler.scheduled_executor.run_configured_scheme",
                return_value=[_record()],
            ),
            patch(
                "scheduler.scheduled_executor.complete_scheduled_attempt",
                side_effect=RuntimeError(
                    "scheduled target multiset mismatch"
                ),
            ),
            patch(
                "scheduler.scheduled_executor.mark_schedule_attempt_terminal_failure",
                return_value="FAILED_TERMINAL",
            ) as terminal,
        ):
            result = execute_scheduled_item(object(), item_id=11)

        self.assertEqual(result.failure_code, "RESULT")
        self.assertEqual(
            terminal.call_args.kwargs["failure_code"],
            "RESULT",
        )


if __name__ == "__main__":
    unittest.main()
