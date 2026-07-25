from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class DailyStoragePreflightTests(unittest.TestCase):
    def test_missing_roots_are_created_private_and_reentry_is_stable(
        self,
    ) -> None:
        from shared.daily_storage_preflight import (
            preflight_daily_storage_roots,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            daily_runtime = base / "daily-runtime"
            roots = {
                "daily_runtime": daily_runtime,
                "occurrence_locks": daily_runtime / "occurrence-locks",
                "native_generations": base / "native",
                "databridge_generations": base / "databridge",
                "databridge_data": base / "data-bridge",
                "databridge_refresh": base / "refresh",
                "liwei_cache": base / "cache",
            }

            first = preflight_daily_storage_roots(roots)
            identities = {
                label: (path.stat().st_dev, path.stat().st_ino)
                for label, path in roots.items()
            }
            second = preflight_daily_storage_roots(roots)

            self.assertEqual(first.labels, tuple(roots))
            self.assertEqual(second.labels, tuple(roots))
            for label, path in roots.items():
                with self.subTest(label=label):
                    details = path.lstat()
                    self.assertTrue(stat.S_ISDIR(details.st_mode))
                    self.assertEqual(details.st_uid, os.getuid())
                    self.assertEqual(
                        stat.S_IMODE(details.st_mode),
                        0o700,
                    )
                    self.assertEqual(
                        (details.st_dev, details.st_ino),
                        identities[label],
                    )

    def test_any_unsafe_root_fails_before_creating_missing_roots(
        self,
    ) -> None:
        from shared.daily_storage_preflight import (
            DailyStoragePreflightError,
            preflight_daily_storage_roots,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            missing = base / "must-not-be-created"
            unsafe = base / "unsafe"
            unsafe.mkdir(mode=0o755)

            with self.assertRaises(
                DailyStoragePreflightError
            ) as caught:
                preflight_daily_storage_roots(
                    {
                        "missing": missing,
                        "unsafe": unsafe,
                    }
                )

            self.assertEqual(
                caught.exception.code,
                "DAILY_STORAGE_ROOT_NOT_PRIVATE",
            )
            self.assertEqual(caught.exception.label, "unsafe")
            self.assertFalse(missing.exists())
            self.assertEqual(
                stat.S_IMODE(unsafe.stat().st_mode),
                0o755,
            )

    def test_final_or_parent_symlink_is_rejected(
        self,
    ) -> None:
        from shared.daily_storage_preflight import (
            DailyStoragePreflightError,
            preflight_daily_storage_roots,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            target = base / "target"
            target.mkdir(mode=0o700)
            final_link = base / "final-link"
            final_link.symlink_to(target, target_is_directory=True)
            parent_link = base / "parent-link"
            parent_link.symlink_to(base, target_is_directory=True)

            for label, path in (
                ("final", final_link),
                ("parent", parent_link / "child"),
            ):
                with (
                    self.subTest(label=label),
                    self.assertRaises(
                        DailyStoragePreflightError
                    ) as caught,
                ):
                    preflight_daily_storage_roots({label: path})
                self.assertEqual(
                    caught.exception.code,
                    "DAILY_STORAGE_PATH_UNSAFE",
                )

    def test_writable_parent_is_rejected_without_creating_child(
        self,
    ) -> None:
        from shared.daily_storage_preflight import (
            DailyStoragePreflightError,
            preflight_daily_storage_roots,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            writable_parent = base / "writable-parent"
            writable_parent.mkdir(mode=0o700)
            writable_parent.chmod(0o777)
            child = writable_parent / "child"

            with self.assertRaises(
                DailyStoragePreflightError
            ) as caught:
                preflight_daily_storage_roots({"child": child})

            self.assertEqual(
                caught.exception.code,
                "DAILY_STORAGE_PATH_UNSAFE",
            )
            self.assertFalse(child.exists())

    def test_wrong_service_owner_is_rejected(self) -> None:
        from shared.daily_storage_preflight import (
            DailyStoragePreflightError,
            preflight_daily_storage_roots,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve() / "owned"
            root.mkdir(mode=0o700)

            with self.assertRaises(
                DailyStoragePreflightError
            ) as caught:
                preflight_daily_storage_roots(
                    {"owned": root},
                    service_uid=os.getuid() + 1,
                )

            self.assertEqual(
                caught.exception.code,
                "DAILY_STORAGE_OWNER_MISMATCH",
            )

    def test_parent_replacement_during_create_fails_closed(
        self,
    ) -> None:
        from shared.daily_storage_preflight import (
            DailyStoragePreflightError,
            preflight_daily_storage_roots,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            parent = base / "parent"
            displaced = base / "displaced"
            outside = base / "outside"
            parent.mkdir(mode=0o700)
            outside.mkdir(mode=0o700)
            child = parent / "child"
            real_mkdir = os.mkdir
            replaced = False

            def replace_parent_then_mkdir(
                path: str | bytes | os.PathLike[str],
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> None:
                nonlocal replaced
                if not replaced and (
                    Path(path) == child
                    or (Path(path) == Path("child") and dir_fd is not None)
                ):
                    parent.rename(displaced)
                    parent.symlink_to(
                        outside,
                        target_is_directory=True,
                    )
                    replaced = True
                real_mkdir(path, mode, dir_fd=dir_fd)

            with (
                mock.patch(
                    "shared.daily_storage_preflight.os.mkdir",
                    side_effect=replace_parent_then_mkdir,
                ),
                self.assertRaises(
                    DailyStoragePreflightError
                ) as caught,
            ):
                preflight_daily_storage_roots({"child": child})

            self.assertTrue(replaced)
            self.assertEqual(
                caught.exception.code,
                "DAILY_STORAGE_PATH_UNSAFE",
            )
            self.assertFalse((outside / "child").exists())

    def test_default_root_matrix_matches_current_runtime_layout(
        self,
    ) -> None:
        from shared.daily_storage_preflight import (
            resolve_daily_storage_roots,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            project = base / "project"
            daily_runtime = base / "daily-runtime"
            cache = base / "configured-cache"
            roots = resolve_daily_storage_roots(
                project_root=project,
                daily_runtime_root=daily_runtime,
                environ={
                    "LIWEI_0616_PHASE_A_CACHE_ROOT": str(cache),
                },
            )

        self.assertEqual(
            roots,
            {
                "daily_runtime": daily_runtime,
                "occurrence_locks":
                    daily_runtime / "occurrence-locks",
                "native_generations":
                    project
                    / "backtest_artifacts"
                    / "input_generations"
                    / "native",
                "databridge_generations":
                    project
                    / "backtest_artifacts"
                    / "input_generations"
                    / "databridge",
                "databridge_data":
                    project / "data" / "data_bridge",
                "databridge_refresh":
                    project
                    / "backtest_artifacts"
                    / "data_bridge_refresh",
                "liwei_cache": cache,
            },
        )

    def test_default_roots_stay_aligned_with_runtime_consumers(
        self,
    ) -> None:
        from scheduler import daily_runtime
        from shared.data_bridge.refresh import DataBridgeRefreshConfig
        from shared.daily_storage_preflight import (
            resolve_daily_storage_roots,
        )
        from shared.liwei_0616_phase_a_cache import DEFAULT_CACHE_ROOT

        roots = resolve_daily_storage_roots(environ={})
        refresh = DataBridgeRefreshConfig.from_env()

        self.assertEqual(
            roots["daily_runtime"],
            daily_runtime.DAILY_RUNTIME_ROOT,
        )
        self.assertEqual(
            roots["occurrence_locks"],
            daily_runtime.LOCK_ROOT,
        )
        self.assertEqual(
            roots["native_generations"],
            daily_runtime.NATIVE_GENERATION_ROOT,
        )
        self.assertEqual(
            roots["databridge_generations"],
            daily_runtime.DATABRIDGE_GENERATION_ROOT,
        )
        self.assertEqual(
            roots["databridge_data"],
            refresh.data_root,
        )
        self.assertEqual(
            roots["databridge_refresh"],
            refresh.runtime_root,
        )
        self.assertEqual(roots["liwei_cache"], DEFAULT_CACHE_ROOT)


if __name__ == "__main__":
    unittest.main()
