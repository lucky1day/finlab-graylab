from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path


_MISSING = object()


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
        self.assertEqual(
            payload["warnings"],
            [
                "Blackbox V2 Metadata 未提供 description；"
                "建议上游补充简短算法逻辑说明。"
            ],
        )

    def test_cli_intake_with_description_has_no_warning(self) -> None:
        from harness.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(
                root / "incoming",
                description="使用期限利差和滚动分类模型形成方向信号。",
            )
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
        self.assertEqual(payload["warnings"], [])

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
            self.assertNotIn("platform_inputs:", config)

    def test_cli_intake_writes_declared_platform_input(self) -> None:
        from harness.cli import main
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")

            exit_code = main(
                [
                    "intake-blackbox",
                    "--delivery-dir",
                    str(delivery),
                    "--project-root",
                    str(root),
                    "--platform-input",
                    "api-wind-date-v1",
                ]
            )
            config_path = root / "schemes" / "trial_10y" / "config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config = load_scheme_config(config_path)

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "platform_inputs:\n  - api-wind-date-v1\n",
            config_text,
        )
        self.assertEqual(config.platform_inputs, ("api-wind-date-v1",))

    def test_intake_rejects_duplicate_platform_input(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")

            with self.assertRaisesRegex(ValueError, "duplicate"):
                intake_delivery(
                    delivery,
                    schemes_root=root / "schemes",
                    platform_inputs=[
                        "api-wind-date-v1",
                        "api-wind-date-v1",
                    ],
                )

        self.assertFalse((root / "schemes" / "trial_10y").exists())

    def test_cli_intake_rejects_repeated_platform_input_flag(self) -> None:
        from harness.cli import main

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")

            with self.assertRaisesRegex(ValueError, "duplicate"):
                main(
                    [
                        "intake-blackbox",
                        "--delivery-dir",
                        str(delivery),
                        "--project-root",
                        str(root),
                        "--platform-input",
                        "api-wind-date-v1",
                        "--platform-input",
                        "api-wind-date-v1",
                    ]
                )

            self.assertFalse((root / "schemes" / "trial_10y").exists())

    def test_intake_rejects_explicit_empty_or_unknown_platform_inputs(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        for value in ([], ["unknown-input-v1"]):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                delivery = _write_delivery(root / "incoming")

                with self.assertRaises(ValueError):
                    intake_delivery(
                        delivery,
                        schemes_root=root / "schemes",
                        platform_inputs=value,
                    )

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


def _write_delivery(path: Path, *, description: object = _MISSING) -> Path:
    path.mkdir(parents=True)
    (path / "trial_10y.py").write_bytes(b"#!/usr/bin/env python\nprint('ok')\n")
    metadata = {
        "schema_version": "1.0",
        "scheme_id": "trial_10y",
        "name": "10Y Trial",
        "algorithm_version": "1.0.0",
        "target_tenor": "10Y",
        "task_type": "T+1",
        "horizon": 1,
        "target_rule": "target_date_yield_vs_feature_date_yield",
    }
    if description is not _MISSING:
        metadata["description"] = description
    (path / "trial_10y.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
