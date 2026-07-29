"""Runtime isolation checks for generated embedded 7Y runners."""

from __future__ import annotations

import builtins
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch
import uuid

import pandas as pd

from tools.embedded_7y_blackbox.frozen_schemes import get_scheme
from tools.embedded_7y_blackbox.payload import collect_payload
from tools.embedded_7y_blackbox.renderer import render_runner


BLACKBOX_PYTHON = Path(
    "/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python"
)
SOURCE_ROOT = Path(
    "source_evidence/benchmark_batches/daily_0629/source_package/forecast_project"
)
REPLAY_DATA_ROOT = Path(
    "/Users/macstudio0/Documents/liwei/outputs/"
    "gray_lab_transfer_3Y_20260725_round6_rank_fusion/"
    "source_replay_input_real/data"
)
PLATFORM_FILENAMES = (
    "daily_output.csv",
    "weekly_output.csv",
    "monthly_output.csv",
    "api_wind_date.csv",
)
FORBIDDEN_ROOTS = (
    "/Users/macstudio0/bond-factor-lab/schemes/daily_5y_lgbm_5y10_0629",
    "/Users/macstudio0/bond-factor-lab/schemes/daily_10y_lgbm_10y04_0629",
    "/Users/macstudio0/bond-factor-lab/source_evidence/benchmark_batches/daily_0629",
    "/Users/macstudio0/Desktop/方案/0629/forecast_project",
)
CUTOFF = pd.Timestamp("2025-07-15")


class EmbeddedRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload = collect_payload(SOURCE_ROOT)
        cls.rendered_runner = render_runner(
            get_scheme("seven_y_t1_cfc_0084_embedded_v1"),
            payload,
        )
        cls.first_payload_path = payload[0].relative_path

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tempdir = Path(self.temporary.name).resolve()

    def load_generated_module(self, script: str | None = None) -> types.ModuleType:
        runner = self.tempdir / "runner.py"
        runner.write_text(script or self.rendered_runner, encoding="utf-8")
        module_name = f"_embedded_7y_test_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, runner)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self.addCleanup(sys.modules.pop, module_name, None)
        spec.loader.exec_module(module)
        return module

    def write_replay_fixture(
        self,
        destination: Path,
        *,
        append_one_future_daily_row: bool,
    ) -> None:
        destination.mkdir(parents=True)
        daily = pd.read_csv(
            REPLAY_DATA_ROOT / "daily_output.csv",
            low_memory=False,
        )
        daily_dates = pd.to_datetime(daily["date"], errors="raise")
        direct_yields = daily.loc[
            :,
            ("TB5YWI0C", "TB7YWI0C", "TB0YWI0C"),
        ].apply(pd.to_numeric, errors="coerce")
        finite_rows = pd.Series(
            direct_yields.notna().all(axis=1)
            & direct_yields.apply(lambda values: values.map(math.isfinite)).all(axis=1),
            index=daily.index,
        )
        last_nonfinite = finite_rows.loc[~finite_rows].index[-1]
        history_start = daily_dates.iloc[last_nonfinite + 1]
        cutoff_mask = daily_dates.between(history_start, CUTOFF)
        if append_one_future_daily_row:
            first_future_index = daily_dates.loc[daily_dates > CUTOFF].index[0]
            cutoff_mask.loc[first_future_index] = True
        daily.loc[cutoff_mask].to_csv(
            destination / "daily_output.csv",
            index=False,
        )

        calendar = pd.read_csv(REPLAY_DATA_ROOT / "api_wind_date.csv")
        calendar_dates = pd.to_datetime(calendar["rdate"], errors="raise")
        calendar = calendar.loc[
            calendar_dates.between(history_start, CUTOFF)
        ].copy()
        calendar.to_csv(destination / "api_wind_date.csv", index=False)

        weekly = pd.read_csv(
            REPLAY_DATA_ROOT / "weekly_output.csv",
            low_memory=False,
        )
        weekly.loc[weekly["week_id"].isin(calendar["week_id"])].to_csv(
            destination / "weekly_output.csv",
            index=False,
        )

        monthly = pd.read_csv(
            REPLAY_DATA_ROOT / "monthly_output.csv",
            low_memory=False,
        )
        monthly.loc[
            monthly["month_id"].between(
                int(history_start.strftime("%Y%m")),
                202507,
            )
        ].to_csv(
            destination / "monthly_output.csv",
            index=False,
        )

    def write_minimal_platform_fixture(self, destination: Path) -> None:
        destination.mkdir(parents=True)
        pd.DataFrame(
            {
                "date": ["2025-07-14", "2025-07-15"],
                "TB5YWI0C": [1.5, 1.6],
                "TB7YWI0C": [1.7, 1.8],
                "TB0YWI0C": [1.9, 2.0],
            }
        ).to_csv(destination / "daily_output.csv", index=False)
        pd.DataFrame(
            {
                "rdate": ["2025-07-14", "2025-07-15"],
                "week_id": [202529, 202529],
            }
        ).to_csv(destination / "api_wind_date.csv", index=False)
        pd.DataFrame(
            {"week_id": [202528, 202529], "factor": [1.0, 2.0]}
        ).to_csv(destination / "weekly_output.csv", index=False)
        pd.DataFrame(
            {"month_id": [202506, 202507], "factor": [1.0, 2.0]}
        ).to_csv(destination / "monthly_output.csv", index=False)

    def replay_anchor_scores(
        self,
        data_dir: Path,
    ) -> tuple[pd.Series, pd.Series, list[str]]:
        module = self.load_generated_module()
        run_root = self.tempdir / f"run-{uuid.uuid4().hex}"
        payload_root = module._extract_payload(run_root / "payload")
        five, ten = module._anchor_modules(payload_root)
        read_paths: list[str] = []
        original_open = builtins.open

        def audited_open(file: object, *args: object, **kwargs: object):
            mode = args[0] if args else kwargs.get("mode", "r")
            if (
                isinstance(file, (str, os.PathLike))
                and isinstance(mode, str)
                and "r" in mode
            ):
                read_path = str(Path(file).resolve())
                read_paths.append(read_path)
                self.assertFalse(
                    any(
                        read_path.startswith(forbidden)
                        for forbidden in FORBIDDEN_ROOTS
                    ),
                    read_path,
                )
            return original_open(file, *args, **kwargs)

        diagnostics = io.StringIO()
        with (
            patch.object(module, "open", audited_open, create=True),
            redirect_stdout(diagnostics),
            redirect_stderr(diagnostics),
        ):
            protected_root = module._materialize_cutoff_data(
                data_dir,
                CUTOFF,
                run_root / "request",
            )
            five_frame = module._run_5y10(
                five,
                protected_root,
                run_root / "five",
            )
            ten_frame = module._run_10y04(
                ten,
                protected_root,
                run_root / "ten",
                CUTOFF,
            )
            five_scores = module._anchor_score(five_frame, "5Y10", CUTOFF)
            ten_scores = module._anchor_score(ten_frame, "10Y04", CUTOFF)
        return five_scores, ten_scores, read_paths

    def test_materializes_exact_causal_platform_cutoff(self) -> None:
        module = self.load_generated_module()
        source = self.tempdir / "platform"
        self.write_minimal_platform_fixture(source)
        future_daily = pd.DataFrame(
            {
                "date": ["2025-07-16"],
                "TB5YWI0C": [99.0],
                "TB7YWI0C": [math.inf],
                "TB0YWI0C": [99.0],
            }
        )
        future_daily.to_csv(
            source / "daily_output.csv",
            mode="a",
            header=False,
            index=False,
        )

        protected_root = module._materialize_cutoff_data(
            source,
            CUTOFF,
            self.tempdir / "request",
        )

        data_root = protected_root / "data"
        self.assertEqual(
            sorted(path.name for path in data_root.iterdir()),
            sorted(PLATFORM_FILENAMES),
        )
        daily = pd.read_csv(data_root / "daily_output.csv")
        calendar = pd.read_csv(data_root / "api_wind_date.csv")
        weekly = pd.read_csv(data_root / "weekly_output.csv")
        monthly = pd.read_csv(data_root / "monthly_output.csv")
        self.assertEqual(pd.to_datetime(daily["date"]).max(), CUTOFF)
        self.assertEqual(pd.to_datetime(calendar["rdate"]).max(), CUTOFF)
        self.assertEqual(weekly["week_id"].tolist(), [202529])
        self.assertEqual(monthly["month_id"].tolist(), [202506, 202507])

    def test_post_cutoff_disorder_in_each_stream_is_ignored(self) -> None:
        module = self.load_generated_module()
        baseline_source = self.tempdir / "baseline-platform"
        self.write_minimal_platform_fixture(baseline_source)
        baseline_root = module._materialize_cutoff_data(
            baseline_source,
            CUTOFF,
            self.tempdir / "baseline-request",
        )
        baseline = {
            filename: pd.read_csv(baseline_root / "data" / filename)
            for filename in PLATFORM_FILENAMES
        }
        future_disorder = {
            "daily_output.csv": pd.DataFrame(
                {
                    "date": ["2025-07-17", "2025-07-16", "2025-07-16"],
                    "TB5YWI0C": [9.0, 9.0, 9.0],
                    "TB7YWI0C": [math.inf, 9.0, 9.0],
                    "TB0YWI0C": [9.0, 9.0, 9.0],
                }
            ),
            "api_wind_date.csv": pd.DataFrame(
                {
                    "rdate": ["2025-07-17", "2025-07-16", "2025-07-16"],
                    "week_id": [202530, 202530, 202530],
                }
            ),
            "weekly_output.csv": pd.DataFrame(
                {
                    "week_id": [202531, 202530, 202530],
                    "factor": [9.0, 9.0, 9.0],
                }
            ),
            "monthly_output.csv": pd.DataFrame(
                {
                    "month_id": [202509, 202508, 202508],
                    "factor": [9.0, 9.0, 9.0],
                }
            ),
        }

        for index, (filename, future_rows) in enumerate(
            future_disorder.items()
        ):
            with self.subTest(filename=filename):
                source = self.tempdir / f"future-disorder-{index}"
                self.write_minimal_platform_fixture(source)
                future_rows.to_csv(
                    source / filename,
                    mode="a",
                    header=False,
                    index=False,
                )
                protected = module._materialize_cutoff_data(
                    source,
                    CUTOFF,
                    self.tempdir / f"future-disorder-request-{index}",
                )
                for output_filename in PLATFORM_FILENAMES:
                    pd.testing.assert_frame_equal(
                        pd.read_csv(protected / "data" / output_filename),
                        baseline[output_filename],
                    )

    def test_materialization_fails_closed_on_invalid_inputs(self) -> None:
        module = self.load_generated_module()

        source = self.tempdir / "missing"
        self.write_minimal_platform_fixture(source)
        (source / "weekly_output.csv").unlink()
        with self.assertRaises((FileNotFoundError, ValueError)):
            module._materialize_cutoff_data(
                source,
                CUTOFF,
                self.tempdir / "missing-request",
            )

        source = self.tempdir / "nonmonotonic"
        self.write_minimal_platform_fixture(source)
        daily = pd.read_csv(source / "daily_output.csv").iloc[::-1]
        daily.to_csv(source / "daily_output.csv", index=False)
        with self.assertRaisesRegex(ValueError, "monotonic"):
            module._materialize_cutoff_data(
                source,
                CUTOFF,
                self.tempdir / "nonmonotonic-request",
            )

        source = self.tempdir / "nonfinite"
        self.write_minimal_platform_fixture(source)
        daily = pd.read_csv(source / "daily_output.csv")
        daily.loc[daily.index[-1], "TB7YWI0C"] = math.inf
        daily.to_csv(source / "daily_output.csv", index=False)
        with self.assertRaisesRegex(ValueError, "finite"):
            module._materialize_cutoff_data(
                source,
                CUTOFF,
                self.tempdir / "nonfinite-request",
            )

    def test_materialization_rejects_nonfinite_retained_yield_history(self) -> None:
        module = self.load_generated_module()
        invalid_values = (float("nan"), "not-a-number")
        for index, invalid in enumerate(invalid_values):
            with self.subTest(invalid=invalid):
                source = self.tempdir / f"retained-nonfinite-{index}"
                self.write_minimal_platform_fixture(source)
                daily = pd.read_csv(source / "daily_output.csv")
                if isinstance(invalid, str):
                    daily["TB5YWI0C"] = daily["TB5YWI0C"].astype(object)
                daily.loc[daily.index[0], "TB5YWI0C"] = invalid
                daily.to_csv(source / "daily_output.csv", index=False)
                with self.assertRaisesRegex(ValueError, "finite"):
                    module._materialize_cutoff_data(
                        source,
                        CUTOFF,
                        self.tempdir / f"retained-nonfinite-request-{index}",
                    )

    def test_future_rows_do_not_change_result(self) -> None:
        without_future = self.tempdir / "without-future"
        with_future = self.tempdir / "with-future"
        self.write_replay_fixture(
            without_future,
            append_one_future_daily_row=False,
        )
        self.write_replay_fixture(
            with_future,
            append_one_future_daily_row=True,
        )

        baseline_five, baseline_ten, baseline_reads = self.replay_anchor_scores(
            without_future
        )
        future_five, future_ten, future_reads = self.replay_anchor_scores(with_future)

        pd.testing.assert_series_equal(baseline_five, future_five)
        pd.testing.assert_series_equal(baseline_ten, future_ten)
        self.assertTrue(math.isfinite(float(baseline_five.loc[CUTOFF])))
        self.assertTrue(math.isfinite(float(baseline_ten.loc[CUTOFF])))
        self.assertTrue(baseline_reads)
        self.assertTrue(future_reads)

    def test_corrupted_payload_fails_before_extraction(self) -> None:
        script = self.rendered_runner.replace(
            '"compressed_sha256":"', '"compressed_sha256":"0', 1
        )
        runner = self.tempdir / "corrupt_runner.py"
        runner.write_text(script, encoding="utf-8")
        extraction_root = self.tempdir / "private"
        command = (
            "import runpy; from pathlib import Path; "
            f"m=runpy.run_path({str(runner)!r}); "
            f"m['_extract_payload'](Path({str(extraction_root)!r}))"
        )

        completed = subprocess.run(
            [str(BLACKBOX_PYTHON), "-c", command],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("compressed payload hash mismatch", completed.stderr)
        self.assertFalse(extraction_root.exists())

    def test_payload_path_escape_is_rejected(self) -> None:
        module = self.load_generated_module()
        for unsafe in (
            "../escape.so",
            "/tmp/escape.so",
            r"..\escape.so",
            "daily_project//escape.so",
            "daily_project/./escape.so",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "unsafe payload path"):
                    module._safe_payload_path(self.tempdir, unsafe)

    def test_runtime_rejects_wrong_python_abi(self) -> None:
        module = self.load_generated_module()
        with patch.object(module.sys, "version_info", (3, 12)):
            with self.assertRaisesRegex(RuntimeError, "requires CPython 3.13"):
                module._validate_runtime()

    def test_runtime_rejects_non_cpython_implementation(self) -> None:
        module = self.load_generated_module()
        implementation = types.SimpleNamespace(name="pypy")
        with patch.object(module.sys, "implementation", implementation):
            with self.assertRaisesRegex(RuntimeError, "requires CPython 3.13"):
                module._validate_runtime()

    def test_runtime_rejects_wrong_platform_abi(self) -> None:
        module = self.load_generated_module()
        for system_name, machine_name in (("Linux", "arm64"), ("Darwin", "x86_64")):
            with self.subTest(system=system_name, machine=machine_name):
                with (
                    patch.object(
                        module.platform, "system", return_value=system_name
                    ),
                    patch.object(
                        module.platform, "machine", return_value=machine_name
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "requires macOS arm64"):
                        module._validate_runtime()

    def test_extraction_rejects_symlink_root(self) -> None:
        module = self.load_generated_module()
        outside = self.tempdir / "outside"
        outside.mkdir()
        run_root = self.tempdir / "private"
        run_root.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "unsafe payload extraction root"):
            module._extract_payload(run_root)

        self.assertEqual(list(outside.iterdir()), [])

    def test_extraction_rejects_symlink_root_ancestor(self) -> None:
        module = self.load_generated_module()
        outside = self.tempdir / "outside"
        outside.mkdir()
        link = self.tempdir / "linked-parent"
        link.symlink_to(outside, target_is_directory=True)
        run_root = link / "nested" / "private"

        with self.assertRaisesRegex(ValueError, "unsafe payload extraction root"):
            module._extract_payload(run_root)

        self.assertEqual(list(outside.iterdir()), [])

    def test_extraction_rejects_existing_parent_symlink(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        run_root.mkdir(mode=0o700)
        outside = self.tempdir / "outside"
        outside.mkdir()
        (run_root / "daily_project").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "unsafe payload path"):
            module._extract_payload(run_root)

        self.assertEqual(list(outside.iterdir()), [])

    def test_dirfd_parent_open_rejects_swapped_symlink(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        run_root.mkdir(mode=0o700)
        original = run_root / "daily_project"
        original.mkdir()
        displaced = run_root / "displaced"
        original.rename(displaced)
        outside = self.tempdir / "outside"
        outside.mkdir()
        original.symlink_to(outside, target_is_directory=True)
        root_fd = os.open(run_root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)

        with self.assertRaisesRegex(ValueError, "unsafe payload path"):
            module._open_payload_parent(
                root_fd,
                ("daily_project", "src"),
                "daily_project/src/escape.so",
            )

    def test_post_install_tamper_is_removed_and_fails_closed(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        original_rename = module.os.rename
        tampered = False

        def rename_then_tamper(
            source: str,
            target: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
        ) -> None:
            nonlocal tampered
            original_rename(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            if not tampered:
                tampered = True
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_TRUNC,
                    dir_fd=dst_dir_fd,
                )
                try:
                    os.write(descriptor, b"tampered")
                finally:
                    os.close(descriptor)

        with patch.object(module.os, "rename", side_effect=rename_then_tamper):
            with self.assertRaisesRegex(ValueError, "post-install mismatch"):
                module._extract_payload(run_root)

        first_path = module._PAYLOAD_MANIFEST[0]["relative_path"]
        self.assertFalse((run_root / first_path).exists())

    def run_post_install_replacement(
        self, replacement: str
    ) -> subprocess.CompletedProcess[str]:
        runner = self.tempdir / f"{replacement}_runner.py"
        runner.write_text(self.rendered_runner, encoding="utf-8")
        run_root = self.tempdir / f"{replacement}_private"
        first_path = self.first_payload_path
        replacement_statement = (
            f"os.mkfifo({str(run_root / first_path)!r}, 0o600)"
            if replacement == "fifo"
            else "os.mkdir(target, 0o700, dir_fd=dst_dir_fd)"
        )
        command = textwrap.dedent(
            f"""
            import os
            from pathlib import Path
            import runpy
            namespace = runpy.run_path({str(runner)!r})
            original_rename = os.rename
            replaced = False
            def replace_after_rename(source, target, *, src_dir_fd, dst_dir_fd):
                global replaced
                original_rename(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                if not replaced:
                    replaced = True
                    os.unlink(target, dir_fd=dst_dir_fd)
                    {replacement_statement}
            os.rename = replace_after_rename
            namespace["_extract_payload"](Path({str(run_root)!r}))
            """
        )
        try:
            completed = subprocess.run(
                [str(BLACKBOX_PYTHON), "-c", command],
                text=True,
                capture_output=True,
                check=False,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"post-install {replacement} verification blocked")
        self.assertFalse((run_root / first_path).exists())
        return completed

    def test_post_install_fifo_replacement_is_nonblocking_and_removed(self) -> None:
        completed = self.run_post_install_replacement("fifo")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("payload post-install mismatch", completed.stderr)

    def test_post_install_directory_replacement_is_removed(self) -> None:
        completed = self.run_post_install_replacement("directory")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("payload post-install mismatch", completed.stderr)

    def test_extracted_tree_has_private_directory_and_file_modes(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "missing" / "nested" / "private"

        payload_root = module._extract_payload(run_root)

        created_roots = [
            self.tempdir / "missing",
            self.tempdir / "missing" / "nested",
            payload_root,
        ]
        for directory in created_roots:
            with self.subTest(directory=directory):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in payload_root.rglob("*"):
            with self.subTest(path=path):
                expected_mode = 0o700 if path.is_dir() else 0o600
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected_mode)

    def test_real_anchor_modules_import_from_extracted_tree(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        prior_daily = {
            name: loaded
            for name, loaded in sys.modules.items()
            if name == "daily" or name.startswith("daily.")
        }
        payload_root = module._extract_payload(run_root)

        five, ten = module._anchor_modules(payload_root)

        self.assertTrue(
            Path(five.__file__).resolve().is_relative_to(payload_root.resolve())
        )
        self.assertTrue(
            Path(ten.__file__).resolve().is_relative_to(payload_root.resolve())
        )
        final_daily = {
            name: loaded
            for name, loaded in sys.modules.items()
            if name == "daily" or name.startswith("daily.")
        }
        self.assertEqual(
            set(final_daily),
            set(prior_daily),
        )
        for name, loaded in prior_daily.items():
            self.assertIs(final_daily[name], loaded)


if __name__ == "__main__":
    unittest.main()
