from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


_MISSING = object()


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
        self.assertEqual(payload["registry_scheme_id"], "trial_10y__h1__10Y")
        self.assertEqual(payload["warnings"], [])

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
            registry_path = root / "deploy" / "scheme_owner_v1.json"
            registry_path.chmod(0o600)

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
            self.assertIn("timeout_sec: 3600", config)
            self.assertNotIn("platform_inputs:", config)
            self.assertNotIn("display_name:", config)
            registry = json.loads(
                (root / "deploy" / "scheme_owner_v1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                registry["owners"], {"trial_10y__h1__10Y": "ALGO-A"}
            )
            self.assertEqual(registry_path.stat().st_mode & 0o777, 0o600)

    def test_intake_requires_owner_and_description_without_partial_write(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        for missing_field in ("owner", "description"):
            with self.subTest(missing_field=missing_field), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                delivery = _write_delivery(root / "incoming")
                metadata_path = delivery / "trial_10y.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata.pop(missing_field)
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                registry_path = root / "deploy" / "scheme_owner_v1.json"
                original_registry = registry_path.read_bytes()

                with self.assertRaisesRegex(ValueError, missing_field):
                    intake_delivery(delivery, schemes_root=root / "schemes")

                self.assertFalse((root / "schemes" / "trial_10y").exists())
                self.assertEqual(registry_path.read_bytes(), original_registry)

    def test_intake_rejects_conflicting_owner_without_writing_scheme(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_owner_registry(
                root,
                {"trial_10y__h1__10Y": "ALGO-B"},
            )
            original = (root / "deploy" / "scheme_owner_v1.json").read_bytes()

            with self.assertRaisesRegex(ValueError, "owner.*conflict"):
                intake_delivery(
                    _write_delivery(root / "incoming"),
                    schemes_root=root / "schemes",
                )

            self.assertFalse((root / "schemes" / "trial_10y").exists())
            self.assertEqual(
                (root / "deploy" / "scheme_owner_v1.json").read_bytes(),
                original,
            )

    def test_intake_requires_existing_owner_registry(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            delivery = _write_delivery(root / "incoming")
            registry_path = root / "deploy" / "scheme_owner_v1.json"
            registry_path.unlink()

            with self.assertRaisesRegex(ValueError, "existing regular file"):
                intake_delivery(delivery, schemes_root=root / "schemes")

            self.assertFalse((root / "schemes" / "trial_10y").exists())

    def test_intake_accepts_same_existing_owner_without_rewriting_registry(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_owner_registry(
                root,
                {"trial_10y__h1__10Y": "ALGO-A"},
            )
            registry_path = root / "deploy" / "scheme_owner_v1.json"
            original = registry_path.read_bytes()

            scheme_dir = intake_delivery(
                _write_delivery(root / "incoming"),
                schemes_root=root / "schemes",
            )

            self.assertTrue(scheme_dir.is_dir())
            self.assertEqual(registry_path.read_bytes(), original)

    def test_concurrent_intakes_serialize_owner_registry_updates(self) -> None:
        from shared.blackbox_v2 import intake as intake_module

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_owner_registry(root, {})
            first_delivery = _write_delivery(
                root / "incoming-a",
                scheme_id="trial_a",
            )
            second_delivery = _write_delivery(
                root / "incoming-b",
                scheme_id="trial_b",
                owner="ALGO-B",
            )
            real_prepare = intake_module._prepare_owner_update
            active = 0
            maximum_active = 0
            counter_lock = threading.Lock()

            def slow_prepare(*args, **kwargs):
                nonlocal active, maximum_active
                with counter_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                try:
                    time.sleep(0.03)
                    return real_prepare(*args, **kwargs)
                finally:
                    with counter_lock:
                        active -= 1

            with patch.object(
                intake_module,
                "_prepare_owner_update",
                side_effect=slow_prepare,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        intake_module.intake_delivery,
                        delivery,
                        schemes_root=root / "schemes",
                    )
                    for delivery in (first_delivery, second_delivery)
                ]
                destinations = [future.result() for future in futures]

            registry = json.loads(
                (root / "deploy" / "scheme_owner_v1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(maximum_active, 1)
            self.assertEqual(
                registry["owners"],
                {
                    "trial_a__h1__10Y": "ALGO-A",
                    "trial_b__h1__10Y": "ALGO-B",
                },
            )
            self.assertTrue(all(path.is_dir() for path in destinations))

    def test_intake_rolls_back_registry_when_scheme_commit_fails(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_owner_registry(root, {"existing__h1__5Y": "ALGO-X"})
            registry_path = root / "deploy" / "scheme_owner_v1.json"
            original = registry_path.read_bytes()
            real_replace = __import__("os").replace

            def fail_scheme_commit(source, destination):
                if Path(destination).resolve() == (
                    root / "schemes" / "trial_10y"
                ).resolve():
                    raise OSError("synthetic scheme commit failure")
                return real_replace(source, destination)

            with patch(
                "shared.blackbox_v2.intake.os.replace",
                side_effect=fail_scheme_commit,
            ):
                with self.assertRaisesRegex(OSError, "synthetic"):
                    intake_delivery(
                        _write_delivery(root / "incoming"),
                        schemes_root=root / "schemes",
                    )

            self.assertFalse((root / "schemes" / "trial_10y").exists())
            self.assertEqual(registry_path.read_bytes(), original)

    def test_intake_does_not_commit_scheme_when_registry_replace_fails(self) -> None:
        from shared.blackbox_v2.intake import intake_delivery

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_owner_registry(root, {})
            registry_path = root / "deploy" / "scheme_owner_v1.json"
            original = registry_path.read_bytes()
            real_replace = __import__("os").replace

            def fail_registry_commit(source, destination):
                if Path(destination).resolve() == registry_path.resolve():
                    raise OSError("synthetic registry commit failure")
                return real_replace(source, destination)

            with patch(
                "shared.blackbox_v2.intake.os.replace",
                side_effect=fail_registry_commit,
            ):
                with self.assertRaisesRegex(OSError, "synthetic"):
                    intake_delivery(
                        _write_delivery(root / "incoming"),
                        schemes_root=root / "schemes",
                    )

            self.assertFalse((root / "schemes" / "trial_10y").exists())
            self.assertEqual(registry_path.read_bytes(), original)

    def test_intake_rejects_registry_drift_after_preflight(self) -> None:
        from shared.blackbox_v2 import intake as intake_module

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_owner_registry(root, {})
            registry_path = root / "deploy" / "scheme_owner_v1.json"
            real_stage = intake_module._stage_registry

            def stage_then_drift(*args, **kwargs):
                staged = real_stage(*args, **kwargs)
                registry_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "scheme-owner-v1",
                            "owners": {"external__h1__5Y": "ALGO-X"},
                        }
                    ),
                    encoding="utf-8",
                )
                return staged

            with patch.object(
                intake_module,
                "_stage_registry",
                side_effect=stage_then_drift,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after"):
                    intake_module.intake_delivery(
                        _write_delivery(root / "incoming"),
                        schemes_root=root / "schemes",
                    )

            self.assertFalse((root / "schemes" / "trial_10y").exists())
            self.assertEqual(
                json.loads(registry_path.read_text(encoding="utf-8"))["owners"],
                {"external__h1__5Y": "ALGO-X"},
            )

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


def _write_delivery(
    path: Path,
    *,
    scheme_id: str = "trial_10y",
    description: object = "使用期限利差和滚动分类模型形成方向信号。",
    owner: object = "ALGO-A",
) -> Path:
    registry_path = path.parent / "deploy" / "scheme_owner_v1.json"
    if not registry_path.exists():
        _write_owner_registry(path.parent, {})
    path.mkdir(parents=True)
    (path / f"{scheme_id}.py").write_bytes(
        b"#!/usr/bin/env python\nprint('ok')\n"
    )
    metadata = {
        "schema_version": "1.0",
        "scheme_id": scheme_id,
        "name": "10Y Trial",
        "algorithm_version": "1.0.0",
        "target_tenor": "10Y",
        "task_type": "T+1",
        "horizon": 1,
        "target_rule": "target_date_yield_vs_feature_date_yield",
    }
    if owner is not _MISSING:
        metadata["owner"] = owner
    if description is not _MISSING:
        metadata["description"] = description
    (path / f"{scheme_id}.json").write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_owner_registry(root: Path, owners: dict[str, str]) -> None:
    deploy = root / "deploy"
    deploy.mkdir(parents=True, exist_ok=True)
    (deploy / "scheme_owner_v1.json").write_text(
        json.dumps(
            {"schema_version": "scheme-owner-v1", "owners": owners},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
