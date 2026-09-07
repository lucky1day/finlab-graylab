from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


class BlackboxV2IntakeTests(unittest.TestCase):
    def test_period_average_intake_uses_one_daily_post_close_poll(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        for task_type, target_rule in (
            (
                "monthly_average",
                "target_month_average_yield_vs_feature_month_average_yield",
            ),
            (
                "quarterly_average",
                "target_quarter_average_yield_vs_feature_quarter_average_yield",
            ),
            (
                "annual_average",
                "target_year_average_yield_vs_feature_year_average_yield",
            ),
        ):
            with self.subTest(task_type=task_type), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                delivery = _write_delivery(root / "incoming")
                metadata_path = delivery / "trial_10y.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata.update(
                    task_type=task_type,
                    horizon=1,
                    target_rule=target_rule,
                )
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                scheme_dir = intake_delivery(
                    delivery,
                    schemes_root=root / "schemes",
                )

                config = (scheme_dir / "config.yaml").read_text(encoding="utf-8")
                self.assertIn("cron: '0 18 * * 1-5'", config)


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
            self.assertIn("factor_input_mode: algorithm_managed", config)
            self.assertIn("status: paused", config)
            self.assertIn("version_status: draft", config)
            self.assertIn("cron: '3 7 * * 1-5'", config)
            self.assertIn("timeout_sec: 3600", config)
            self.assertNotIn("platform_inputs:", config)
            self.assertNotIn("display_name:", config)
            self.assertNotIn("incremental_state:", config)

    def test_cli_incremental_state_preserves_two_file_metadata_contract(self) -> None:
        from harness.cli import main
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "intake-blackbox",
                        "--delivery-dir", str(delivery),
                        "--project-root", str(root),
                        "--incremental-state",
                    ]
                )
            scheme_dir = root / "schemes" / "trial_10y"
            config = load_scheme_config(scheme_dir / "config.yaml")
            self.assertEqual(exit_code, 0)
            self.assertIs(config.incremental_state, True)
            self.assertEqual(config.contract_version, "1.0")
            self.assertEqual(
                {path.name for path in (scheme_dir / "delivery").iterdir()},
                {"trial_10y.py", "trial_10y.json"},
            )
            for name in ("trial_10y.py", "trial_10y.json"):
                self.assertEqual(
                    (scheme_dir / "delivery" / name).read_bytes(),
                    (delivery / name).read_bytes(),
                )

    def test_intake_requires_business_metadata_without_partial_write(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        for field in ("description", "owner"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                delivery = _write_delivery(root / "incoming")
                metadata_path = delivery / "trial_10y.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata.pop(field)
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, field):
                    intake_delivery(delivery, schemes_root=root / "schemes")

                self.assertFalse((root / "schemes" / "trial_10y").exists())


    def test_cli_intake_uses_fixed_databridge_inputs(self) -> None:
        from harness.cli import main
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    [
                        "intake-blackbox",
                        "--delivery-dir",
                        str(delivery),
                        "--project-root",
                        str(root),
                    ]
                )
            config_path = root / "schemes" / "trial_10y" / "config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config = load_scheme_config(config_path)
            payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertNotIn("platform_inputs:", config_text)
        self.assertFalse(hasattr(config, "platform_inputs"))
        self.assertEqual(
            set(payload),
            {"scheme_id", "runtime_type", "scheme_dir"},
        )

    def test_intake_rejects_additional_delivery_file(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            (delivery / "notes.txt").write_text("extra", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exactly two regular files"):
                intake_delivery(delivery, schemes_root=root / "schemes")

    def test_intake_rejects_unsafe_script_before_writing_scheme(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            (delivery / "trial_10y.py").write_text(
                "import requests\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "forbidden import requests"):
                intake_delivery(delivery, schemes_root=root / "schemes")

            self.assertFalse((root / "schemes" / "trial_10y").exists())

    def test_intake_refuses_existing_scheme_id(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            existing = root / "schemes" / "trial_10y"
            existing.mkdir(parents=True)

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                intake_delivery(delivery, schemes_root=root / "schemes")


def _write_delivery(
    path: Path,
) -> Path:
    path.mkdir(parents=True)
    (path / "trial_10y.py").write_bytes(
        b"#!/usr/bin/env python\nprint('ok')\n"
    )
    metadata = {
        "schema_version": "1.0",
        "scheme_id": "trial_10y",
        "name": "10Y Trial",
        "owner": "ALGO-A",
        "description": "使用期限利差和滚动分类模型形成方向信号。",
        "algorithm_version": "1.0.0",
        "target_tenor": "10Y",
        "task_type": "T+1",
        "horizon": 1,
        "target_rule": "target_date_yield_vs_feature_date_yield",
    }
    (path / "trial_10y.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    return path
