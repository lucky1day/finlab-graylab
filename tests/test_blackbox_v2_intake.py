from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path


class BlackboxV2IntakeTests(unittest.TestCase):
    def test_cli_intake_uses_project_schemes_directory(self) -> None:
        from harness.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "intake-blackbox",
                        "--delivery-dir",
                        str(delivery),
                        "--project-root",
                        str(root),
                    ]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["scheme_id"], "trial_10y")
        self.assertEqual(payload["runtime_type"], "blackbox_v2")

    def test_intake_preserves_delivery_bytes_and_generates_paused_config(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            schemes_root = root / "schemes"

            scheme_dir = intake_delivery(delivery, schemes_root=schemes_root)

            script = scheme_dir / "delivery" / "trial_10y.py"
            metadata = scheme_dir / "delivery" / "trial_10y.json"
            config = (scheme_dir / "config.yaml").read_text(encoding="utf-8")
            self.assertEqual(script.read_bytes(), (delivery / "trial_10y.py").read_bytes())
            self.assertEqual(metadata.read_bytes(), (delivery / "trial_10y.json").read_bytes())
            self.assertIn("runtime_type: blackbox_v2", config)
            self.assertIn("input_source: data_bridge_current", config)
            self.assertIn("status: paused", config)
            self.assertIn("version_status: draft", config)
            self.assertIn("cron: '3 7 * * 1-5'", config)

    def test_intake_rejects_additional_delivery_file(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            (delivery / "notes.txt").write_text("extra", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exactly two regular files"):
                intake_delivery(delivery, schemes_root=root / "schemes")

    def test_intake_refuses_existing_scheme_id(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            existing = root / "schemes" / "trial_10y"
            existing.mkdir(parents=True)

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                intake_delivery(delivery, schemes_root=root / "schemes")


def _write_delivery(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "trial_10y.py").write_bytes(b"#!/usr/bin/env python\nprint('ok')\n")
    (path / "trial_10y.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scheme_id": "trial_10y",
                "name": "10Y Trial",
                "algorithm_version": "1.0.0",
                "target_tenor": "10Y",
                "task_type": "T+1",
                "horizon": 1,
                "target_rule": "target_date_yield_vs_feature_date_yield",
            }
        ),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
