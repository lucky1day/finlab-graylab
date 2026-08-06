from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

import pandas as pd


def _write_schema(root: Path) -> Path:
    schema = root / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "schema_version": "data-bridge-v1",
                "files": {
                    "daily_output.csv": {
                        "columns": ["date", "factor"],
                    },
                    "weekly_output.csv": {
                        "columns": ["week_id", "factor"],
                    },
                    "monthly_output.csv": {
                        "columns": ["month_id", "factor"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return schema


def _publish_valid_current(root: Path):
    from shared.data_bridge.refresh import (
        DataBridgeRefreshConfig,
        DataBridgeStore,
    )
    from shared.data_bridge.validation import (
        validate_dataset,
        write_validated_dataset,
    )

    schema = _write_schema(root)
    config = DataBridgeRefreshConfig(
        data_root=root / "data",
        runtime_root=root / "runtime",
        schema_path=schema,
    )
    dataset = validate_dataset(
        {
            "daily_output.csv": pd.DataFrame(
                {
                    "date": ["2026/07/18 00:00"],
                    "factor": ["1"],
                }
            ),
            "weekly_output.csv": pd.DataFrame(
                {
                    "week_id": ["202629"],
                    "factor": ["2"],
                }
            ),
            "monthly_output.csv": pd.DataFrame(
                {
                    "month_id": ["202607"],
                    "factor": ["3"],
                }
            ),
        },
        schema_path=schema,
    )
    candidate = write_validated_dataset(
        dataset,
        root / "candidate",
    )
    state = {
        "schema_version": dataset.schema_version,
        "generation_id": "strict-read-generation",
        "refresh_date": "2026-07-24",
        "business_digest": dataset.business_digest,
        "files": {
            filename: {
                "sha256": profile.sha256,
                "business_hash": profile.business_hash,
            }
            for filename, profile in dataset.files.items()
        },
    }
    store = DataBridgeStore(
        data_root=config.data_root,
        runtime_root=config.runtime_root,
    )
    store.publish(candidate, state)
    return config, store


def _tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        kind = stat.S_IFMT(info.st_mode)
        payload = ""
        if stat.S_ISREG(info.st_mode):
            payload = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            payload = os.readlink(path)
        snapshot[relative] = (
            kind,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_size,
            info.st_mtime_ns,
            payload,
        )
    return snapshot


class StrictDataBridgeCurrentReadTests(unittest.TestCase):
    def test_valid_strict_read_does_not_modify_the_published_tree(
        self,
    ) -> None:
        from shared.data_bridge.refresh import check_current_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config, _ = _publish_valid_current(root)
            before = _tree_snapshot(root)

            checked = check_current_dataset(
                config,
                required_refresh_date="2026-07-24",
                strict_read_only=True,
            )

            self.assertEqual(
                checked.state["generation_id"],
                "strict-read-generation",
            )
            self.assertEqual(_tree_snapshot(root), before)

    def test_missing_roots_lock_or_state_are_typed_and_never_created(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeCurrentMissingError,
            DataBridgeRefreshConfig,
            check_current_dataset,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for missing in ("data", "runtime", "lock", "state"):
                with self.subTest(missing=missing):
                    case = root / missing
                    case.mkdir(mode=0o700)
                    data_root = case / "data"
                    runtime_root = case / "runtime"
                    if missing != "data":
                        data_root.mkdir(mode=0o700)
                    if missing != "runtime":
                        runtime_root.mkdir(mode=0o700)
                    if missing not in {"runtime", "lock"}:
                        (runtime_root / "refresh.lock").touch(mode=0o600)
                    if missing == "state":
                        pass
                    before = _tree_snapshot(case)
                    config = DataBridgeRefreshConfig(
                        data_root=data_root,
                        runtime_root=runtime_root,
                        schema_path=case / "schema.json",
                    )

                    with self.assertRaises(
                        DataBridgeCurrentMissingError
                    ):
                        check_current_dataset(
                            config,
                            strict_read_only=True,
                        )

                    self.assertEqual(_tree_snapshot(case), before)

    def test_unsafe_or_symlink_roots_are_typed_invalid(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            DataBridgeCurrentInvalidError,
            DataBridgeRefreshConfig,
            check_current_dataset,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o755)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            (runtime / "refresh.lock").touch(mode=0o600)
            config = DataBridgeRefreshConfig(
                data_root=unsafe,
                runtime_root=runtime,
                schema_path=root / "schema.json",
            )
            with self.assertRaises(DataBridgeCurrentInvalidError):
                check_current_dataset(
                    config,
                    strict_read_only=True,
                )

            real_data = root / "real-data"
            real_data.mkdir(mode=0o700)
            linked_data = root / "linked-data"
            linked_data.symlink_to(real_data, target_is_directory=True)
            linked_config = DataBridgeRefreshConfig(
                data_root=linked_data,
                runtime_root=runtime,
                schema_path=root / "schema.json",
            )
            with self.assertRaises(DataBridgeCurrentInvalidError):
                check_current_dataset(
                    linked_config,
                    strict_read_only=True,
                )

    def test_corrupt_state_marker_hash_or_current_are_typed_invalid(
        self,
    ) -> None:
        from shared.data_bridge.refresh import (
            CURRENT_PUBLICATION_MANIFEST,
            DataBridgeCurrentInvalidError,
            check_current_dataset,
        )

        mutators = {
            "state": lambda store: store.state_path.write_text(
                "{bad-json",
                encoding="utf-8",
            ),
            "marker": lambda store: _replace_read_only_file(
                store.current_dir / CURRENT_PUBLICATION_MANIFEST,
                b"{bad-json",
            ),
            "hash": lambda store: _replace_read_only_file(
                store.current_dir / "daily_output.csv",
                b"date,factor\n2026/07/18 00:00,99\n",
            ),
            "current": lambda store: shutil.rmtree(
                store.current_dir
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for label, mutate in mutators.items():
                with self.subTest(label=label):
                    case = root / label
                    case.mkdir(mode=0o700)
                    config, store = _publish_valid_current(case)
                    mutate(store)

                    with self.assertRaises(
                        DataBridgeCurrentInvalidError
                    ):
                        check_current_dataset(
                            config,
                            strict_read_only=True,
                        )

    def test_authority_uses_checker_classification_without_second_probe(
        self,
    ) -> None:
        from shared.data_bridge import authority as authority_module
        from shared.data_bridge.authority import (
            DataBridgeCurrentMissingError as AuthorityMissingError,
            resolve_stable_databridge_current_authority,
        )
        from shared.data_bridge.refresh import (
            DataBridgeCurrentMissingError,
            DataBridgeRefreshConfig,
        )

        self.assertIs(
            AuthorityMissingError,
            DataBridgeCurrentMissingError,
        )
        source = inspect.getsource(
            authority_module.resolve_stable_databridge_current_authority
        )
        self.assertNotIn("state_path", source)
        self.assertNotIn(".is_file(", source)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = DataBridgeRefreshConfig(
                data_root=root / "data",
                runtime_root=root / "runtime",
                schema_path=root / "schema.json",
            )

            with self.assertRaises(DataBridgeCurrentMissingError):
                resolve_stable_databridge_current_authority(
                    config,
                    feature_dates=("2026-07-24",),
                    connection=object(),
                )

            self.assertEqual(list(root.iterdir()), [])


def _replace_read_only_file(path: Path, payload: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(payload)
    path.chmod(0o444)


if __name__ == "__main__":
    unittest.main()
