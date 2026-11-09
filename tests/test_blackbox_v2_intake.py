from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml
import pytest


class BlackboxV2IntakeTests(unittest.TestCase):
    def test_cli_intake_preserves_bytes_and_creates_paused_config(self) -> None:
        from harness.cli import main
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            with redirect_stdout(io.StringIO()) as output:
                code = main(["intake-blackbox", "--delivery-dir", str(delivery),
                             "--project-root", str(root)])
            scheme_dir = root / "schemes" / "trial_10y"
            config = load_scheme_config(scheme_dir / "config.yaml")
            raw = yaml.safe_load((scheme_dir / "config.yaml").read_text(encoding="utf-8"))
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


                config = yaml.safe_load((scheme_dir / "config.yaml").read_text(encoding="utf-8"))
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


@pytest.mark.parametrize(("source", "error"), [
    ('import os as platform\nplatform.system("exit 0")\n', "forbidden call os.system"),
    ('from os import popen as call\ncall("exit 0")\n', "forbidden call os.popen"),
    ('from builtins import eval as calculate\ncalculate("1+1")\n', "forbidden call builtins.eval"),
    ('open("/tmp/not-a-platform-input.csv")\n', "hard-coded absolute path"),
    ('from pathlib import Path as P\nP("/var/private").read_text()\n', "hard-coded absolute path"),
    ('import pandas as pd\npath = "/etc/demo.csv"\npd.read_csv(filepath_or_buffer=path)\n', "hard-coded absolute path"),
    ('import os.path as paths\npaths.join("data", f"/opt/{name}")\n', "hard-coded absolute path"),
    ('def load(data_dir):\n    return data_dir / "/mnt/test.csv"\n', "hard-coded absolute path"),
])
def test_intake_rejects_aliased_calls_and_absolute_file_paths(tmp_path, source, error):
    from shared.blackbox_v2.intake import intake_delivery

    delivery = _write_delivery(tmp_path / "incoming")
    (delivery / "trial_10y.py").write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        intake_delivery(delivery, schemes_root=tmp_path / "schemes")
    assert not (tmp_path / "schemes").exists()


def test_intake_accepts_text_and_relative_path_construction(tmp_path):
    from shared.blackbox_v2.intake import intake_delivery

    source = (
        'from pathlib import Path as P\n'
        'label = "/chart/ratio"\n'
        'def load(data_dir):\n'
        '    path = P(f"{data_dir}/daily_output.csv")\n'
        '    return path.read_text(), label\n'
    )
    delivery = _write_delivery(tmp_path / "incoming")
    (delivery / "trial_10y.py").write_text(source, encoding="utf-8")
    canonical = intake_delivery(delivery, schemes_root=tmp_path / "schemes")
    assert (canonical / "delivery" / "trial_10y.py").read_text() == source


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
