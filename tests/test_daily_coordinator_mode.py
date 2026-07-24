from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DailyCoordinatorModeTests(unittest.TestCase):
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

    def _write_canonical_json(self, path: Path, payload: object) -> bytes:
        raw = self._canonical_bytes(payload)
        path.write_bytes(raw)
        return raw

    def _contract_files(
        self,
        root: Path,
    ) -> tuple[Path, Path, Path, dict[str, object]]:
        from shared import daily_coordinator_mode as mode_contract

        genesis = {
            "epoch": 1,
            "mode": "ledger",
            "previous_record_sha256": "0" * 64,
            "schema_version": mode_contract.EPOCH_SCHEMA_VERSION,
            "transition_id": "bond-factor-lab-daily-ledger-genesis-v1",
        }
        genesis_path = root / "genesis.json"
        genesis_bytes = self._write_canonical_json(genesis_path, genesis)
        contract = {
            "epoch_filename": "epoch-%020d.json",
            "first_epoch": 1,
            "genesis_sha256": hashlib.sha256(genesis_bytes).hexdigest(),
            "modes": ["legacy", "ledger"],
            "record_schema_version": mode_contract.EPOCH_SCHEMA_VERSION,
            "schema_version": mode_contract.EPOCH_CONTRACT_SCHEMA_VERSION,
            "zero_previous_record_sha256": "0" * 64,
        }
        contract_path = root / "contract.json"
        self._write_canonical_json(contract_path, contract)
        rollout_path = root / "rollout.json"
        self._write_canonical_json(
            rollout_path,
            {
                "mode": "legacy",
                "schema_version": mode_contract.ROLLOUT_SCHEMA_VERSION,
            },
        )
        return rollout_path, contract_path, genesis_path, genesis

    def _write_epoch(
        self,
        epoch_dir: Path,
        payload: dict[str, object],
        *,
        filename_epoch: int | None = None,
    ) -> bytes:
        records_dir = epoch_dir / "records"
        records_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        epoch_dir.chmod(0o755)
        records_dir.chmod(0o755)
        path = records_dir / (
            f"epoch-{filename_epoch if filename_epoch is not None else payload['epoch']:020d}.json"
        )
        raw = self._write_canonical_json(path, payload)
        path.chmod(0o644)
        return raw

    def _append_epoch(
        self,
        epoch_dir: Path,
        *,
        epoch: int,
        mode: str,
        previous_raw: bytes,
    ) -> bytes:
        return self._write_epoch(
            epoch_dir,
            {
                "epoch": epoch,
                "mode": mode,
                "previous_record_sha256": hashlib.sha256(
                    previous_raw
                ).hexdigest(),
                "schema_version": "daily-coordinator-epoch-v1",
                "transition_id": f"operator-transition-{epoch}",
            },
        )

    def _patch_contract(
        self,
        mode_contract,
        *,
        rollout_path: Path,
        contract_path: Path,
        genesis_path: Path,
        epoch_dir: Path,
    ):
        return patch.multiple(
            mode_contract,
            DEFAULT_ROLLOUT_PATH=rollout_path,
            DEFAULT_EPOCH_CONTRACT_PATH=contract_path,
            DEFAULT_EPOCH_GENESIS_PATH=genesis_path,
            DEFAULT_EPOCH_DIRECTORY=epoch_dir,
            DEFAULT_EPOCH_OWNER_UID=os.getuid(),
            DEFAULT_EPOCH_SERVICE_UID=os.getuid(),
            DEFAULT_EPOCH_PARENT_ANCHOR=epoch_dir.parent,
        )

    def test_mode_must_be_explicit(self) -> None:
        from shared.daily_coordinator_mode import (
            DAILY_COORDINATOR_MODE_ENV,
            require_daily_coordinator_mode,
        )

        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(
                ValueError,
                f"{DAILY_COORDINATOR_MODE_ENV} must be explicitly set",
            ),
        ):
            require_daily_coordinator_mode()

    def test_repository_rollout_only_bootstraps_absent_epoch_directory(
        self,
    ) -> None:
        from shared import daily_coordinator_mode as mode_contract

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            rollout, contract, genesis, _payload = self._contract_files(root)
            epoch_dir = root / "machine-global" / "epochs"
            with (
                patch.dict(os.environ, {}, clear=True),
                self._patch_contract(
                    mode_contract,
                    rollout_path=rollout,
                    contract_path=contract,
                    genesis_path=genesis,
                    epoch_dir=epoch_dir,
                ),
            ):
                identity = (
                    mode_contract.require_current_daily_coordinator_identity()
                )

            self.assertEqual(identity.epoch, 0)
            self.assertEqual(identity.mode, "legacy")
            self.assertEqual(identity.source, "repository_bootstrap")

            (epoch_dir / "records").mkdir(mode=0o755, parents=True)
            epoch_dir.chmod(0o755)
            with (
                patch.dict(
                    os.environ,
                    {mode_contract.DAILY_COORDINATOR_MODE_ENV: "legacy"},
                    clear=True,
                ),
                self._patch_contract(
                    mode_contract,
                    rollout_path=rollout,
                    contract_path=contract,
                    genesis_path=genesis,
                    epoch_dir=epoch_dir,
                ),
                self.assertRaisesRegex(RuntimeError, "epoch directory is empty"),
            ):
                mode_contract.read_daily_coordinator_epoch_chain()

    def test_epoch_chain_is_authority_and_explicit_mode_is_only_assertion(
        self,
    ) -> None:
        from shared import daily_coordinator_mode as mode_contract

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            rollout, contract, genesis_path, genesis = self._contract_files(
                root
            )
            epoch_dir = root / "machine-global" / "epochs"
            self._write_epoch(epoch_dir, genesis)
            common = self._patch_contract(
                mode_contract,
                rollout_path=rollout,
                contract_path=contract,
                genesis_path=genesis_path,
                epoch_dir=epoch_dir,
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                common,
                patch.object(
                    mode_contract,
                    "read_deployment_daily_coordinator_mode",
                    side_effect=AssertionError(
                        "rollout must not be read once the chain exists"
                    ),
                ),
            ):
                self.assertEqual(
                    mode_contract.bootstrap_deployment_daily_coordinator_mode(),
                    "ledger",
                )

            mode_contract._reset_daily_coordinator_process_identity_for_tests()
            with (
                patch.dict(
                    os.environ,
                    {mode_contract.DAILY_COORDINATOR_MODE_ENV: "legacy"},
                    clear=True,
                ),
                self._patch_contract(
                    mode_contract,
                    rollout_path=rollout,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    epoch_dir=epoch_dir,
                ),
                self.assertRaisesRegex(RuntimeError, "does not match.*ledger"),
            ):
                mode_contract.bootstrap_deployment_daily_coordinator_mode()

            mode_contract._reset_daily_coordinator_process_identity_for_tests()
            with (
                patch.dict(
                    os.environ,
                    {mode_contract.DAILY_COORDINATOR_MODE_ENV: "ledger"},
                    clear=True,
                ),
                self._patch_contract(
                    mode_contract,
                    rollout_path=rollout,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    epoch_dir=epoch_dir,
                ),
            ):
                self.assertEqual(
                    mode_contract.bootstrap_deployment_daily_coordinator_mode(),
                    "ledger",
                )

    def test_higher_epoch_ledger_to_legacy_rollback_is_legal(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            rollout, contract, genesis_path, genesis = self._contract_files(
                root
            )
            epoch_dir = root / "machine-global" / "epochs"
            genesis_raw = self._write_epoch(epoch_dir, genesis)
            rollback_raw = self._append_epoch(
                epoch_dir,
                epoch=2,
                mode="legacy",
                previous_raw=genesis_raw,
            )
            with (
                patch.dict(
                    os.environ,
                    {mode_contract.DAILY_COORDINATOR_MODE_ENV: "legacy"},
                    clear=True,
                ),
                self._patch_contract(
                    mode_contract,
                    rollout_path=rollout,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    epoch_dir=epoch_dir,
                ),
            ):
                identity = (
                    mode_contract.require_current_daily_coordinator_identity()
                )

            self.assertEqual(identity.epoch, 2)
            self.assertEqual(identity.mode, "legacy")
            self.assertEqual(
                identity.record_sha256,
                hashlib.sha256(rollback_raw).hexdigest(),
            )
            self.assertEqual(identity.source, "epoch_chain")

    def test_gap_broken_link_and_replayed_epoch_are_rejected(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        cases = ("gap", "broken_link", "replay")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                rollout, contract, genesis_path, genesis = (
                    self._contract_files(root)
                )
                epoch_dir = root / "machine-global" / "epochs"
                genesis_raw = self._write_epoch(epoch_dir, genesis)
                if case == "gap":
                    self._write_epoch(
                        epoch_dir,
                        {
                            "epoch": 3,
                            "mode": "legacy",
                            "previous_record_sha256": hashlib.sha256(
                                genesis_raw
                            ).hexdigest(),
                            "schema_version":
                                mode_contract.EPOCH_SCHEMA_VERSION,
                            "transition_id": "gap-epoch-3",
                        },
                    )
                    expected = "contiguous"
                elif case == "broken_link":
                    self._append_epoch(
                        epoch_dir,
                        epoch=2,
                        mode="legacy",
                        previous_raw=b"not-the-genesis",
                    )
                    expected = "previous_record_sha256"
                else:
                    self._write_epoch(
                        epoch_dir,
                        genesis,
                        filename_epoch=2,
                    )
                    expected = "filename.*payload epoch"
                with (
                    self._patch_contract(
                        mode_contract,
                        rollout_path=rollout,
                        contract_path=contract,
                        genesis_path=genesis_path,
                        epoch_dir=epoch_dir,
                    ),
                    self.assertRaisesRegex(RuntimeError, expected),
                ):
                    mode_contract.read_daily_coordinator_epoch_chain()

    def test_same_or_lower_transition_cannot_be_prepared(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        current = mode_contract.DailyCoordinatorEpochIdentity(
            epoch=2,
            mode="legacy",
            previous_record_sha256="a" * 64,
            record_sha256="b" * 64,
            source="epoch_chain",
            transition_id="operator-transition-2",
        )
        for epoch in (1, 2, 4):
            with (
                self.subTest(epoch=epoch),
                self.assertRaisesRegex(ValueError, "exactly current epoch \\+ 1"),
            ):
                mode_contract.build_daily_coordinator_epoch_record(
                    current=current,
                    epoch=epoch,
                    mode="ledger",
                    transition_id=f"invalid-{epoch}",
                )

        raw = mode_contract.build_daily_coordinator_epoch_record(
            current=current,
            epoch=3,
            mode="ledger",
            transition_id="operator-transition-3",
        )
        self.assertEqual(
            json.loads(raw),
            {
                "epoch": 3,
                "mode": "ledger",
                "previous_record_sha256": "b" * 64,
                "schema_version": mode_contract.EPOCH_SCHEMA_VERSION,
                "transition_id": "operator-transition-3",
            },
        )

    def test_process_binding_rejects_in_flight_epoch_transition(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            rollout, contract, genesis_path, genesis = self._contract_files(
                root
            )
            epoch_dir = root / "machine-global" / "epochs"
            genesis_raw = self._write_epoch(epoch_dir, genesis)
            with (
                patch.dict(
                    os.environ,
                    {mode_contract.DAILY_COORDINATOR_MODE_ENV: "ledger"},
                    clear=True,
                ),
                self._patch_contract(
                    mode_contract,
                    rollout_path=rollout,
                    contract_path=contract,
                    genesis_path=genesis_path,
                    epoch_dir=epoch_dir,
                ),
            ):
                bound = (
                    mode_contract.require_current_daily_coordinator_identity()
                )
                self._append_epoch(
                    epoch_dir,
                    epoch=2,
                    mode="legacy",
                    previous_raw=genesis_raw,
                )
                os.environ[
                    mode_contract.DAILY_COORDINATOR_MODE_ENV
                ] = "legacy"
                with self.assertRaisesRegex(
                    RuntimeError,
                    "process-bound daily coordinator epoch drift",
                ):
                    mode_contract.require_current_daily_coordinator_identity()

            self.assertEqual(bound.epoch, 1)

    def test_chain_files_must_be_canonical_and_private(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        for case in ("non_root_writable", "noncanonical"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                rollout, contract, genesis_path, genesis = (
                    self._contract_files(root)
                )
                epoch_dir = root / "machine-global" / "epochs"
                self._write_epoch(epoch_dir, genesis)
                epoch_path = (
                    epoch_dir
                    / "records"
                    / "epoch-00000000000000000001.json"
                )
                if case == "non_root_writable":
                    epoch_path.chmod(0o666)
                    expected = "writable by non-root"
                else:
                    epoch_path.write_text(
                        json.dumps(genesis, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    epoch_path.chmod(0o644)
                    expected = "canonical"
                with (
                    self._patch_contract(
                        mode_contract,
                        rollout_path=rollout,
                        contract_path=contract,
                        genesis_path=genesis_path,
                        epoch_dir=epoch_dir,
                    ),
                    self.assertRaisesRegex(RuntimeError, expected),
                ):
                    mode_contract.read_daily_coordinator_epoch_chain()

    def test_symlink_wrong_owner_and_writable_parent_fail_closed(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        cases = ("symlink", "wrong_owner", "writable_parent")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir).resolve()
                rollout, contract, genesis_path, genesis = (
                    self._contract_files(root)
                )
                epoch_dir = root / "machine-global" / "epochs"
                self._write_epoch(epoch_dir, genesis)
                record = (
                    epoch_dir
                    / "records"
                    / "epoch-00000000000000000001.json"
                )
                expected_owner_uid = os.getuid()
                expected = ""
                if case == "symlink":
                    record.unlink()
                    record.symlink_to(genesis_path)
                    expected = "unreadable or unsafe"
                elif case == "wrong_owner":
                    expected_owner_uid = os.getuid() + 10000
                    expected = "unexpected owner"
                else:
                    epoch_dir.parent.chmod(0o777)
                    expected = "parent is writable by non-root"
                with self.assertRaisesRegex(RuntimeError, expected):
                    mode_contract.read_daily_coordinator_epoch_chain(
                        epoch_directory=epoch_dir,
                        epoch_contract_path=contract,
                        epoch_genesis_path=genesis_path,
                        expected_owner_uid=expected_owner_uid,
                        service_uid=os.getuid(),
                        parent_anchor=epoch_dir.parent,
                    )

    def test_control_plane_candidate_identity_excludes_active_epoch(self) -> None:
        from shared import daily_coordinator_mode as mode_contract

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            _rollout, contract, genesis_path, genesis = self._contract_files(
                root
            )
            epoch_dir = root / "machine-global" / "epochs"
            before = mode_contract.daily_control_plane_artifacts(
                service_uid=os.getuid(),
                runtime_root=root / "runtime",
                epoch_directory=epoch_dir,
                epoch_contract_path=contract,
                epoch_genesis_path=genesis_path,
            )
            genesis_raw = self._write_epoch(epoch_dir, genesis)
            self._append_epoch(
                epoch_dir,
                epoch=2,
                mode="legacy",
                previous_raw=genesis_raw,
            )
            after = mode_contract.daily_control_plane_artifacts(
                service_uid=os.getuid(),
                runtime_root=root / "runtime",
                epoch_directory=epoch_dir,
                epoch_contract_path=contract,
                epoch_genesis_path=genesis_path,
            )

        self.assertEqual(before, after)
        self.assertEqual(
            set(before),
            {
                "cutover/epoch-contract.json",
                "cutover/epoch-directory.txt",
                "cutover/epoch-genesis.json",
                "runtime/root.txt",
                "service/uid.txt",
            },
        )
        epoch_path_digest = before["cutover/epoch-directory.txt"]
        runtime_path_digest = before["runtime/root.txt"]
        self.assertNotEqual(epoch_path_digest, runtime_path_digest)

    def test_launchagent_uid_can_read_but_no_non_root_uid_can_write(
        self,
    ) -> None:
        from shared import daily_coordinator_mode as mode_contract

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            rollout, contract, genesis_path, genesis = self._contract_files(
                root
            )
            epoch_dir = root / "machine-global" / "epochs"
            self._write_epoch(epoch_dir, genesis)
            synthetic_service_uid = os.getuid() + 10000
            with (
                patch.dict(
                    os.environ,
                    {mode_contract.DAILY_COORDINATOR_MODE_ENV: "ledger"},
                    clear=True,
                ),
                patch.multiple(
                    mode_contract,
                    DEFAULT_ROLLOUT_PATH=rollout,
                    DEFAULT_EPOCH_CONTRACT_PATH=contract,
                    DEFAULT_EPOCH_GENESIS_PATH=genesis_path,
                    DEFAULT_EPOCH_DIRECTORY=epoch_dir,
                    DEFAULT_EPOCH_OWNER_UID=os.getuid(),
                    DEFAULT_EPOCH_SERVICE_UID=synthetic_service_uid,
                    DEFAULT_EPOCH_PARENT_ANCHOR=epoch_dir.parent,
                ),
            ):
                identity = (
                    mode_contract.require_current_daily_coordinator_identity()
                )

            directory_mode = stat.S_IMODE(epoch_dir.stat().st_mode)
            record_mode = stat.S_IMODE(
                (
                    epoch_dir
                    / "records"
                    / "epoch-00000000000000000001.json"
                ).stat().st_mode
            )
            self.assertEqual(identity.mode, "ledger")
            self.assertTrue(directory_mode & stat.S_IXOTH)
            self.assertTrue(record_mode & stat.S_IROTH)
            self.assertFalse(directory_mode & (stat.S_IWGRP | stat.S_IWOTH))
            self.assertFalse(record_mode & (stat.S_IWGRP | stat.S_IWOTH))


if __name__ == "__main__":
    unittest.main()
