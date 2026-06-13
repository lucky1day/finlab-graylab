from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.context import GateContext
from harness.gates.static_gate import StaticGate


def _evidence_keys(result) -> set[str]:
    return {item.key for item in result.evidence}


def _write_minimal_scheme(project_root: Path, *, scheme_id: str = "demo_daily") -> Path:
    scheme_dir = project_root / "schemes" / scheme_id
    (scheme_dir / "core").mkdir(parents=True)
    (scheme_dir / "__init__.py").write_text("", encoding="utf-8")
    (scheme_dir / "core" / "__init__.py").write_text("", encoding="utf-8")
    (scheme_dir / "core" / "model.py").write_text("def fit(df):\n    return df\n", encoding="utf-8")
    (scheme_dir / "predict.py").write_text(
        "\n".join(
            [
                "from shared.input_artifacts import build_daily_input_artifact",
                f'SCHEME_ID = "{scheme_id}"',
                "def run(predict_date: str):",
                "    return []",
            ]
        ),
        encoding="utf-8",
    )
    (scheme_dir / "config.yaml").write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                'name: "Demo"',
                'description: "Demo scheme"',
                "horizon: 1",
                'tenors: ["10Y"]',
                "frequency: daily",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                "status: paused",
                "input_spec:",
                "  data_version: shared_data_service_daily.v1",
                '  required_columns: ["date", "TB0YWI0C"]',
            ]
        ),
        encoding="utf-8",
    )
    return scheme_dir


def _run_gate(project_root: Path, scheme_id: str = "demo_daily"):
    return StaticGate().run(
        GateContext(
            scheme_id=scheme_id,
            predict_date="2026-06-08",
            project_root=project_root,
            report_dir=project_root / "reports",
        )
    )


class StaticGateHardeningTests(unittest.TestCase):
    def test_nested_core_violation_is_caught_by_recursion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root)
            nested = scheme_dir / "core" / "sub"
            nested.mkdir()
            (nested / "__init__.py").write_text("", encoding="utf-8")
            (nested / "x.py").write_text("import sqlalchemy\n", encoding="utf-8")

            result = _run_gate(project_root)

            self.assertFalse(result.passed)
            self.assertTrue(any("core/sub/x.py" in item for item in result.errors), result.errors)
            self.assertTrue(any("sqlalchemy" in item for item in result.errors), result.errors)

    def test_active_predict_import_of_legacy_module_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root)
            (scheme_dir / "core" / "legacy_old.py").write_text("def stale():\n    return 1\n", encoding="utf-8")
            (scheme_dir / "predict.py").write_text(
                "\n".join(
                    [
                        "from shared.input_artifacts import build_daily_input_artifact",
                        "from schemes.demo_daily.core import legacy_old",
                        'SCHEME_ID = "demo_daily"',
                        "def run(predict_date: str):",
                        "    return []",
                    ]
                ),
                encoding="utf-8",
            )

            result = _run_gate(project_root)

            self.assertFalse(result.passed)
            self.assertTrue(
                any("legacy" in item and "predict.py" in item for item in result.errors), result.errors
            )

    def test_newly_forbidden_core_import_requests_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root)
            (scheme_dir / "core" / "net.py").write_text("import requests\n", encoding="utf-8")

            result = _run_gate(project_root)

            self.assertFalse(result.passed)
            self.assertTrue(any("requests" in item for item in result.errors), result.errors)
            self.assertIn("dangerous_core_imports", _evidence_keys(result))

    def test_core_file_write_and_qualified_call_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root)
            (scheme_dir / "core" / "io_bad.py").write_text(
                "\n".join(
                    [
                        "import pickle",
                        "from pathlib import Path",
                        "def dump(obj):",
                        "    with open('/tmp/x.bin', 'wb') as fh:",
                        "        fh.write(b'')",
                        "    Path('/tmp/y.txt').write_text('hi')",
                        "    return pickle.load(open('/tmp/x.bin', 'rb'))",
                    ]
                ),
                encoding="utf-8",
            )

            result = _run_gate(project_root)

            self.assertFalse(result.passed)
            joined = "\n".join(result.errors)
            self.assertIn("file write call", joined)
            self.assertIn("pickle.load", joined)

    def test_core_dataframe_file_export_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root)
            (scheme_dir / "core" / "export_bad.py").write_text(
                "\n".join(
                    [
                        "def dump(df):",
                        "    df.to_csv('/tmp/export.csv', index=False)",
                    ]
                ),
                encoding="utf-8",
            )

            result = _run_gate(project_root)

            self.assertFalse(result.passed)
            self.assertTrue(any("to_csv" in item for item in result.errors), result.errors)

    def test_predict_non_whitelisted_shared_import_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = _write_minimal_scheme(project_root)
            (scheme_dir / "predict.py").write_text(
                "\n".join(
                    [
                        "from shared.input_artifacts import build_daily_input_artifact",
                        "from shared.data_service import create_sqlalchemy_engine",
                        'SCHEME_ID = "demo_daily"',
                        "def run(predict_date: str):",
                        "    return []",
                    ]
                ),
                encoding="utf-8",
            )

            result = _run_gate(project_root)

            self.assertFalse(result.passed)
            self.assertTrue(
                any("whitelist" in item and "shared.data_service" in item for item in result.errors),
                result.errors,
            )

    def test_real_schemes_pass_static_gate(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        for scheme_id in ("t1_daily", "t5_daily"):
            with self.subTest(scheme_id=scheme_id):
                result = StaticGate().run(
                    GateContext(
                        scheme_id=scheme_id,
                        predict_date="2026-06-08",
                        project_root=project_root,
                        report_dir=project_root / "reports" / "harness" / "test",
                    )
                )
                self.assertTrue(result.passed, result.errors)


if __name__ == "__main__":
    unittest.main()
