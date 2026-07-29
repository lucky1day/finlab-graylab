"""Runtime isolation checks for generated embedded 7Y runners."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import uuid

from tools.embedded_7y_blackbox.frozen_schemes import get_scheme
from tools.embedded_7y_blackbox.payload import collect_payload
from tools.embedded_7y_blackbox.renderer import render_runner


BLACKBOX_PYTHON = Path(
    "/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python"
)
SOURCE_ROOT = Path(
    "source_evidence/benchmark_batches/daily_0629/source_package/forecast_project"
)


class EmbeddedRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rendered_runner = render_runner(
            get_scheme("seven_y_t1_cfc_0084_embedded_v1"),
            collect_payload(SOURCE_ROOT),
        )

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
