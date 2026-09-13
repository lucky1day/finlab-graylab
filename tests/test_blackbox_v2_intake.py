from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


class BlackboxV2IntakeTests(unittest.TestCase):
    def test_cli_intake_preserves_bytes_and_creates_paused_config(self) -> None:
        from harness.cli import main
        from scheduler.discovery import load_scheme_config
        from shared.scheme_config_loader import load_yaml_mapping

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            with redirect_stdout(io.StringIO()) as output:
                code = main(["intake-blackbox", "--delivery-dir", str(delivery),
                             "--project-root", str(root)])
            scheme_dir = root / "schemes" / "trial_10y"
            config = load_scheme_config(scheme_dir / "config.yaml")
            raw = load_yaml_mapping(scheme_dir / "config.yaml")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue()), {
                "scheme_id": "trial_10y", "runtime_type": "blackbox_v2",
                "scheme_dir": str(scheme_dir.resolve()),
            })
            for path in delivery.iterdir():
                self.assertEqual((scheme_dir / "delivery" / path.name).read_bytes(), path.read_bytes())
            self.assertEqual((config.runtime_type, config.input_source, config.factor_input_mode),
                             ("blackbox_v2", "data_bridge_current", "algorithm_managed"))
            self.assertEqual((config.status, config.version_status), ("paused", "draft"))
            self.assertFalse(config.incremental_state)
            self.assertFalse({"platform_inputs", "display_name", "incremental_state"} & raw.keys())
            self.assertEqual(raw["schedule"], {
                "cron": "3 7 * * 1-5", "timezone": "Asia/Shanghai", "timeout_sec": 3600,
            })

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

                from shared.scheme_config_loader import load_yaml_mapping

                config = load_yaml_mapping(scheme_dir / "config.yaml")
                self.assertEqual(config["schedule"]["cron"], "0 18 * * 1-5")


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
