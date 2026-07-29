"""Runtime isolation checks for generated embedded 7Y runners."""

from __future__ import annotations

import importlib.util
from pathlib import Path
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
        self.tempdir = Path(self.temporary.name)

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

    def test_runtime_rejects_wrong_platform_abi(self) -> None:
        module = self.load_generated_module()
        with patch.object(module.platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(RuntimeError, "requires macOS arm64"):
                module._validate_runtime()

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

    def test_real_anchor_modules_import_from_extracted_tree(self) -> None:
        module = self.load_generated_module()
        run_root = self.tempdir / "private"
        payload_root = module._extract_payload(run_root)

        five, ten = module._anchor_modules(payload_root)

        self.assertTrue(
            Path(five.__file__).resolve().is_relative_to(payload_root.resolve())
        )
        self.assertTrue(
            Path(ten.__file__).resolve().is_relative_to(payload_root.resolve())
        )
        self.assertFalse(
            any(
                (name == "daily" or name.startswith("daily."))
                and isinstance(getattr(loaded, "__file__", None), str)
                and Path(loaded.__file__).resolve().is_relative_to(
                    payload_root.resolve()
                )
                for name, loaded in sys.modules.items()
            )
        )


if __name__ == "__main__":
    unittest.main()
