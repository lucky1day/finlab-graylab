from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text


class SimulatedCrash(BaseException):
    """模拟 publish 已完成但调用方尚未收到返回。"""


class DailyCoordinatorEpochOperatorTests(unittest.TestCase):
    def test_capacity_probes_bind_policy_v2_in_process_and_child(
        self,
    ) -> None:
        from scheduler.daily_policy import POLICY_V2_PATH
        from scripts import daily_coordinator_epoch_operator as module

        engine = SimpleNamespace(dispose=Mock())
        with (
            patch(
                "scheduler.repository.create_engine_from_env",
                return_value=engine,
            ),
            patch(
                "scheduler.capacity_runtime_admission."
                "require_current_capacity_admission",
                return_value={"status": "ADMITTED"},
            ) as current,
        ):
            module._require_capacity_admission_in_current_process()

        current.assert_called_once_with(
            engine,
            policy_path=POLICY_V2_PATH,
        )
        engine.dispose.assert_called_once_with()

        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "ADMITTED",
                    "candidate_fingerprint": "a" * 64,
                }
            ),
            stderr="",
        )
        with (
            patch.object(module.os, "geteuid", return_value=0),
            patch.object(
                module.pwd,
                "getpwuid",
                return_value=SimpleNamespace(
                    pw_name="bond-factor-lab",
                    pw_gid=501,
                ),
            ),
            patch.object(
                module.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            module.require_trusted_current_capacity_admission(502)

        child_code = run.call_args.args[0][2]
        self.assertIn(
            "from scheduler.daily_policy import POLICY_V2_PATH;",
            child_code,
        )
        self.assertIn(
            "require_current_capacity_admission("
            "engine,policy_path=POLICY_V2_PATH)",
            child_code,
        )

    def setUp(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        mode_contract._reset_daily_coordinator_process_identity_for_tests()

    def tearDown(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        mode_contract._reset_daily_coordinator_process_identity_for_tests()

    @staticmethod
    def _canonical_bytes(payload: object) -> bytes:
        return (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )

    def _fixtures(
        self,
        root: Path,
    ) -> tuple[Path, Path, dict[str, object]]:
        from shared import daily_coordinator_mode as mode_contract

        genesis = {
            "epoch": 1,
            "mode": "ledger",
            "previous_record_sha256": "0" * 64,
            "schema_version": mode_contract.EPOCH_SCHEMA_VERSION,
            "transition_id": "bond-factor-lab-daily-ledger-genesis-v1",
        }
        genesis_path = root / "genesis.json"
        genesis_raw = self._canonical_bytes(genesis)
        genesis_path.write_bytes(genesis_raw)
        contract = {
            "epoch_filename": "epoch-%020d.json",
            "first_epoch": 1,
            "genesis_sha256": hashlib.sha256(genesis_raw).hexdigest(),
            "modes": ["legacy", "ledger"],
            "record_schema_version": mode_contract.EPOCH_SCHEMA_VERSION,
            "schema_version": mode_contract.EPOCH_CONTRACT_SCHEMA_VERSION,
            "zero_previous_record_sha256": "0" * 64,
        }
        contract_path = root / "contract.json"
        contract_path.write_bytes(self._canonical_bytes(contract))
        return contract_path, genesis_path, genesis

    @staticmethod
    def _service_states(stopped: bool = True) -> dict[str, bool]:
        from scripts.daily_coordinator_epoch_operator import (
            LAUNCHAGENT_LABELS,
        )

        return {
            label: not stopped
            for label in LAUNCHAGENT_LABELS
        }

    @staticmethod
    def _installed_modes(mode: str) -> dict[str, str]:
        from scripts.daily_coordinator_epoch_operator import (
            LAUNCHAGENT_LABELS,
        )

        return {
            label: mode
            for label in LAUNCHAGENT_LABELS
        }

    def _transition(
        self,
        *,
        epoch_root: Path,
        contract_path: Path,
        genesis_path: Path,
        expected_current_epoch: int,
        mode: str,
        transition_id: str,
        quiescence: dict[str, int] | None = None,
        admission: dict[str, object] | None = None,
        phase_hook=None,
    ):
        from scripts.daily_coordinator_epoch_operator import (
            publish_daily_coordinator_epoch,
        )

        return publish_daily_coordinator_epoch(
            expected_current_epoch=expected_current_epoch,
            mode=mode,
            transition_id=transition_id,
            service_uid=os.getuid(),
            business_date=date(2026, 7, 24),
            epoch_directory=epoch_root,
            epoch_contract_path=contract_path,
            epoch_genesis_path=genesis_path,
            expected_owner_uid=os.getuid(),
            parent_anchor=epoch_root.parent,
            effective_uid=0,
            service_state_probe=lambda _uid: self._service_states(),
            installed_mode_probe=lambda _uid: self._installed_modes(mode),
            quiescence_probe=(
                lambda _uid, _business_date: (
                    self._quiescent_report()
                    if quiescence is None
                    else quiescence
                )
            ),
            capacity_admission_probe=(
                lambda _uid: (
                    self._admitted_capacity()
                    if admission is None
                    else admission
                )
            ),
            phase_hook=phase_hook,
        )

    @staticmethod
    def _quiescent_report() -> dict[str, int]:
        from scripts.daily_coordinator_epoch_operator import (
            QUIESCENCE_FIELDS,
        )

        return {
            field: 0
            for field in QUIESCENCE_FIELDS
        }

    @staticmethod
    def _admitted_capacity() -> dict[str, object]:
        return {
            "status": "ADMITTED",
            "candidate_fingerprint": "a" * 64,
        }

    def _read_chain(
        self,
        *,
        epoch_root: Path,
        contract_path: Path,
        genesis_path: Path,
    ):
        from shared.daily_coordinator_mode import (
            read_daily_coordinator_epoch_chain,
        )

        return read_daily_coordinator_epoch_chain(
            epoch_directory=epoch_root,
            epoch_contract_path=contract_path,
            epoch_genesis_path=genesis_path,
            expected_owner_uid=os.getuid(),
            service_uid=os.getuid(),
            parent_anchor=epoch_root.parent,
        )

    def test_first_cutover_publishes_fixed_genesis_via_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"

            result = self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=0,
                mode="ledger",
                transition_id=(
                    "bond-factor-lab-daily-ledger-genesis-v1"
                ),
            )
            current = self._read_chain(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
            )

            self.assertEqual(result.status, "published")
            self.assertEqual(current.epoch, 1)
            self.assertEqual(current.mode, "ledger")
            self.assertTrue(
                (
                    epoch_root
                    / "records"
                    / "epoch-00000000000000000001.json"
                ).is_file()
            )
            self.assertTrue((epoch_root / "staging").is_dir())

    def test_partial_staging_is_ignored_and_retry_can_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            records = epoch_root / "records"
            staging = epoch_root / "staging"
            records.mkdir(parents=True, mode=0o755)
            staging.mkdir(mode=0o700)
            epoch_root.chmod(0o755)
            records.chmod(0o755)
            staging.chmod(0o700)
            (staging / ".partial.tmp").write_bytes(b'{"epoch":')

            with self.assertRaisesRegex(RuntimeError, "empty"):
                self._read_chain(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                )

            result = self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=0,
                mode="ledger",
                transition_id=(
                    "bond-factor-lab-daily-ledger-genesis-v1"
                ),
            )
            self.assertEqual(result.status, "published")
            self.assertEqual(
                self._read_chain(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                ).epoch,
                1,
            )

    def test_truncated_final_record_is_fail_closed_and_never_overwritten(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            records = epoch_root / "records"
            staging = epoch_root / "staging"
            records.mkdir(parents=True, mode=0o755)
            staging.mkdir(mode=0o700)
            epoch_root.chmod(0o755)
            records.chmod(0o755)
            final = records / "epoch-00000000000000000001.json"
            final.write_bytes(b'{"epoch":')
            final.chmod(0o644)

            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                self._read_chain(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                )
            with self.assertRaisesRegex(RuntimeError, "existing final"):
                self._transition(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    expected_current_epoch=0,
                    mode="ledger",
                    transition_id=(
                        "bond-factor-lab-daily-ledger-genesis-v1"
                    ),
                )
            self.assertEqual(final.read_bytes(), b'{"epoch":')

    def test_after_publish_before_return_is_visible_and_retry_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"

            def crash_after_publish(phase: str) -> None:
                if phase == "after_publish":
                    raise SimulatedCrash()

            with self.assertRaises(SimulatedCrash):
                self._transition(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    expected_current_epoch=0,
                    mode="ledger",
                    transition_id=(
                        "bond-factor-lab-daily-ledger-genesis-v1"
                    ),
                    phase_hook=crash_after_publish,
                )

            current = self._read_chain(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
            )
            retry = self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=0,
                mode="ledger",
                transition_id=(
                    "bond-factor-lab-daily-ledger-genesis-v1"
                ),
            )
            self.assertEqual(current.epoch, 1)
            self.assertEqual(retry.status, "already_published")
            self.assertEqual(retry.identity.record_sha256, current.record_sha256)

    def test_higher_epoch_rollback_rejects_recoverable_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=0,
                mode="ledger",
                transition_id=(
                    "bond-factor-lab-daily-ledger-genesis-v1"
                ),
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "not quiescent",
            ):
                self._transition(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    expected_current_epoch=1,
                    mode="legacy",
                    transition_id="operator-rollback-20260724",
                    quiescence={
                        **self._quiescent_report(),
                        "nonterminal_item_count": 1,
                    },
                )
            self.assertEqual(
                self._read_chain(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                ).epoch,
                1,
            )

    def test_higher_epoch_ledger_to_legacy_is_allowed_when_quiescent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=0,
                mode="ledger",
                transition_id=(
                    "bond-factor-lab-daily-ledger-genesis-v1"
                ),
            )
            result = self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=1,
                mode="legacy",
                transition_id="operator-rollback-20260724",
            )

            self.assertEqual(result.status, "published")
            self.assertEqual(result.identity.epoch, 2)
            self.assertEqual(result.identity.mode, "legacy")

    def test_transition_requires_root_stopped_services_and_all_plist_modes(
        self,
    ) -> None:
        from scripts.daily_coordinator_epoch_operator import (
            publish_daily_coordinator_epoch,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            base = {
                "expected_current_epoch": 0,
                "mode": "ledger",
                "transition_id":
                    "bond-factor-lab-daily-ledger-genesis-v1",
                "service_uid": os.getuid(),
                "business_date": date(2026, 7, 24),
                "epoch_directory": epoch_root,
                "epoch_contract_path": contract,
                "epoch_genesis_path": genesis_path,
                "expected_owner_uid": os.getuid(),
                "parent_anchor": epoch_root.parent,
                "quiescence_probe": (
                    lambda _uid, _date: self._quiescent_report()
                ),
                "capacity_admission_probe": (
                    lambda _uid: self._admitted_capacity()
                ),
            }
            with self.assertRaisesRegex(PermissionError, "root"):
                publish_daily_coordinator_epoch(
                    **base,
                    effective_uid=501,
                    service_state_probe=lambda _uid: self._service_states(),
                    installed_mode_probe=(
                        lambda _uid: self._installed_modes("ledger")
                    ),
                )
            with self.assertRaisesRegex(RuntimeError, "still loaded"):
                publish_daily_coordinator_epoch(
                    **base,
                    effective_uid=0,
                    service_state_probe=(
                        lambda _uid: self._service_states(stopped=False)
                    ),
                    installed_mode_probe=(
                        lambda _uid: self._installed_modes("ledger")
                    ),
                )
            with self.assertRaisesRegex(RuntimeError, "installed plist"):
                publish_daily_coordinator_epoch(
                    **base,
                    effective_uid=0,
                    service_state_probe=lambda _uid: self._service_states(),
                    installed_mode_probe=(
                        lambda _uid: self._installed_modes("legacy")
                    ),
                )

            self.assertFalse(epoch_root.exists())

    def test_any_transition_away_from_ledger_rejects_nonquiescent_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=0,
                mode="ledger",
                transition_id=(
                    "bond-factor-lab-daily-ledger-genesis-v1"
                ),
            )
            for field in (
                "running_ledger_run_count",
                "cleanup_pending_count",
                "registered_process_alive_count",
            ):
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "not quiescent",
                    ),
                ):
                    self._transition(
                        epoch_root=epoch_root,
                        contract_path=contract,
                        genesis_path=genesis_path,
                        expected_current_epoch=1,
                        mode="ledger",
                        transition_id=f"ledger-maintenance-{field}",
                        quiescence={
                            **self._quiescent_report(),
                            field: 1,
                        },
                    )
            self.assertEqual(
                self._read_chain(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                ).epoch,
                1,
            )

    def test_legacy_to_ledger_rejects_old_path_process_or_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=0,
                mode="ledger",
                transition_id=(
                    "bond-factor-lab-daily-ledger-genesis-v1"
                ),
            )
            self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=1,
                mode="legacy",
                transition_id="rollback-to-legacy",
            )
            for field in (
                "running_legacy_scheduled_live_run_count",
                "project_process_count",
            ):
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(RuntimeError, "not quiescent"),
                ):
                    self._transition(
                        epoch_root=epoch_root,
                        contract_path=contract,
                        genesis_path=genesis_path,
                        expected_current_epoch=2,
                        mode="ledger",
                        transition_id=f"return-to-ledger-{field}",
                        quiescence={
                            **self._quiescent_report(),
                            field: 1,
                        },
                    )

    def test_unregistered_native_scheme_runner_counts_as_platform_process(
        self,
    ) -> None:
        from scripts.daily_coordinator_epoch_operator import (
            _is_daily_platform_process,
        )

        command = (
            "/Users/macstudio0/miniconda3/bin/conda run "
            "--no-capture-output -n forecast_env python -m "
            "scheduler.scheme_runner --scheme-id "
            "daily_1y_xgb_1y13_0629 --predict-date 2026-07-27"
        )

        self.assertTrue(_is_daily_platform_process(command))

    def test_target_ledger_requires_current_trusted_capacity_admission(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            with self.assertRaisesRegex(RuntimeError, "capacity admission"):
                self._transition(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    expected_current_epoch=0,
                    mode="ledger",
                    transition_id=(
                        "bond-factor-lab-daily-ledger-genesis-v1"
                    ),
                    admission={
                        "status": "BLOCKED",
                        "candidate_fingerprint": None,
                    },
                )
            self.assertFalse(epoch_root.exists())

    def test_quiescence_probe_requires_exact_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            incomplete = self._quiescent_report()
            incomplete.pop("project_process_count")
            with self.assertRaisesRegex(RuntimeError, "exact fields"):
                self._transition(
                    epoch_root=epoch_root,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    expected_current_epoch=0,
                    mode="ledger",
                    transition_id=(
                        "bond-factor-lab-daily-ledger-genesis-v1"
                    ),
                    quiescence=incomplete,
                )

    def test_first_layout_and_publish_run_all_durability_phases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            contract, genesis_path, _genesis = self._fixtures(root)
            epoch_root = root / "machine-global-epochs"
            phases: list[str] = []
            self._transition(
                epoch_root=epoch_root,
                contract_path=contract,
                genesis_path=genesis_path,
                expected_current_epoch=0,
                mode="ledger",
                transition_id=(
                    "bond-factor-lab-daily-ledger-genesis-v1"
                ),
                phase_hook=phases.append,
            )

            self.assertEqual(
                phases,
                [
                    "after_layout_fsync",
                    "after_staging_fsync",
                    "after_publish",
                ],
            )

    def test_database_quiescence_is_global_across_past_and_future_dates(
        self,
    ) -> None:
        from scripts.daily_coordinator_epoch_operator import (
            _read_database_quiescence,
        )

        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_schedule_occurrences (
                        occurrence_id INTEGER PRIMARY KEY,
                        predict_date TEXT NOT NULL,
                        completion_state TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE t_schedule_items (
                        item_id INTEGER PRIMARY KEY,
                        occurrence_id INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        current_run_id INTEGER,
                        failure_code TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE t_scheme_runs (
                        run_id INTEGER PRIMARY KEY,
                        predict_date TEXT NOT NULL,
                        prediction_phase TEXT,
                        schedule_item_id INTEGER,
                        status TEXT NOT NULL,
                        failure_code TEXT,
                        process_id INTEGER,
                        process_group_id INTEGER
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_schedule_occurrences
                        (occurrence_id, predict_date, completion_state)
                    VALUES
                        (1, '2026-07-23', 'PENDING'),
                        (2, '2026-07-25', 'RUNNING')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_schedule_items
                        (item_id, occurrence_id, state, current_run_id,
                         failure_code)
                    VALUES
                        (11, 1, 'PENDING', 101, NULL),
                        (12, 2, 'ABANDONED', 102,
                         'ABANDONED_FENCE_PENDING_CLEANUP'),
                        (13, 2, 'FAILED', NULL, NULL)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_scheme_runs
                        (run_id, predict_date, prediction_phase,
                         schedule_item_id, status, failure_code,
                         process_id, process_group_id)
                    VALUES
                        (101, '2026-07-23', 'scheduled_live',
                         11, 'running', NULL, 50101, 50101),
                        (102, '2026-07-25', 'scheduled_live',
                         12, 'failed',
                         'ABANDONED_FENCE_PENDING_CLEANUP',
                         NULL, 50102),
                        (103, '2026-07-25', 'scheduled_live',
                         NULL, 'running', NULL, 50103, 50103),
                        (104, '2026-07-22', 'scheduled_live',
                         13, 'failed',
                         'ABANDONED_FENCE_PENDING_CLEANUP',
                         NULL, NULL),
                        (105, '2026-07-21', 'scheduled_live',
                         NULL, 'failed',
                         'ABANDONED_FENCE_PENDING_CLEANUP',
                         NULL, NULL)
                    """
                )
            )

        report, registered = _read_database_quiescence(engine)
        engine.dispose()

        self.assertEqual(
            report,
            {
                "active_occurrence_count": 2,
                "nonterminal_item_count": 2,
                "running_ledger_run_count": 1,
                "cleanup_pending_count": 3,
                "running_legacy_scheduled_live_run_count": 1,
            },
        )
        self.assertEqual(len(registered), 3)

    def test_abandoned_item_blocks_with_terminal_occurrence(self) -> None:
        from scripts.daily_coordinator_epoch_operator import (
            _read_database_quiescence,
        )

        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE t_schedule_occurrences (
                        occurrence_id INTEGER PRIMARY KEY,
                        predict_date TEXT NOT NULL,
                        completion_state TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE t_schedule_items (
                        item_id INTEGER PRIMARY KEY,
                        occurrence_id INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        current_run_id INTEGER,
                        failure_code TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE t_scheme_runs (
                        run_id INTEGER PRIMARY KEY,
                        predict_date TEXT NOT NULL,
                        prediction_phase TEXT,
                        schedule_item_id INTEGER,
                        status TEXT NOT NULL,
                        failure_code TEXT,
                        process_id INTEGER,
                        process_group_id INTEGER
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_schedule_occurrences
                        (occurrence_id, predict_date, completion_state)
                    VALUES (1, '2026-07-20', 'SUCCESS')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO t_schedule_items
                        (item_id, occurrence_id, state, current_run_id,
                         failure_code)
                    VALUES (11, 1, 'ABANDONED', NULL, NULL)
                    """
                )
            )

        report, registered = _read_database_quiescence(engine)
        engine.dispose()

        self.assertEqual(report["active_occurrence_count"], 0)
        self.assertEqual(report["nonterminal_item_count"], 1)
        self.assertEqual(
            {
                key: value
                for key, value in report.items()
                if key not in {
                    "active_occurrence_count",
                    "nonterminal_item_count",
                }
            },
            {
                "running_ledger_run_count": 0,
                "cleanup_pending_count": 0,
                "running_legacy_scheduled_live_run_count": 0,
            },
        )
        self.assertEqual(registered, ())


if __name__ == "__main__":
    unittest.main()
