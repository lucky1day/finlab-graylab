from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.contracts import import_rules
from scheduler import daily_coordinator, main as scheduler_main, repository


class RepositoryArchitectureBoundaryTests(unittest.TestCase):
    def test_repository_boundary_scanner_is_available(self) -> None:
        self.assertTrue(
            hasattr(import_rules, "repository_layer_import_violations"),
            "repo-wide architecture gate must expose a repository scanner",
        )

    def test_shared_and_scheduler_upward_imports_report_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root / "shared" / "allowed.py", "from shared.models import PredictionRecord\n")
            self._write(
                root / "shared" / "bad.py",
                "\nfrom scheduler.executor import execute_scheme\n",
            )
            self._write(
                root / "scheduler" / "scheme_edge.py",
                "from schemes.alpha.predict import run\n",
            )
            self._write(
                root / "scheduler" / "harness_edge.py",
                "import harness.contracts.config_schema\n",
            )

            actual = [
                violation.format(root)
                for violation in import_rules.repository_layer_import_violations(root)
            ]

            self.assertEqual(
                [
                    "scheduler/harness_edge.py:1: forbidden layer import: "
                    "scheduler -> harness.contracts.config_schema",
                    "scheduler/scheme_edge.py:1: forbidden layer import: "
                    "scheduler -> schemes.alpha.predict",
                    "shared/bad.py:2: forbidden layer import: shared -> scheduler.executor",
                ],
                actual,
            )

    def test_backend_and_backtests_follow_existing_dependency_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root / "backend" / "allowed.py",
                "from shared.models import PredictionRecord\n"
                "from scheduler.repository import create_engine_from_env\n",
            )
            self._write(
                root / "backend" / "bad.py",
                "from backtests.repository import clean_json\n",
            )
            self._write(
                root / "backtests" / "allowed.py",
                "from schemes.alpha.core.model import predict\n"
                "from schemes.alpha.inference import run_window\n",
            )
            self._write(
                root / "backtests" / "bad.py",
                "from scheduler.executor import execute_scheme\n"
                "from schemes.alpha.predict import run\n"
                "import harness\n",
            )

            actual = [
                violation.format(root)
                for violation in import_rules.repository_layer_import_violations(root)
            ]

            self.assertEqual(
                [
                    "backend/bad.py:1: forbidden layer import: backend -> backtests.repository",
                    "backtests/bad.py:1: forbidden layer import: backtests -> scheduler.executor",
                    "backtests/bad.py:2: forbidden layer import: backtests -> schemes.alpha.predict",
                    "backtests/bad.py:3: forbidden layer import: backtests -> harness",
                ],
                actual,
            )

    def test_scanner_excludes_tests_outputs_and_unclassified_tooling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root / "tests" / "bad.py", "from scheduler import executor\n")
            self._write(root / "outputs" / "bad.py", "from scheduler import executor\n")
            self._write(root / "scripts" / "admin.py", "from scheduler import repository\n")
            self._write(root / "shared" / "ok.py", "from shared import models\n")

            self.assertEqual(
                [],
                import_rules.repository_layer_import_violations(root),
            )

    def test_harness_cannot_import_one_shot_admin_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root / "harness" / "bad.py",
                "from scripts.refresh_data_bridge_current "
                "import check_current\n",
            )

            actual = [
                violation.format(root)
                for violation
                in import_rules.repository_layer_import_violations(root)
            ]

            self.assertEqual(
                [
                    "harness/bad.py:1: forbidden layer import: "
                    "harness -> "
                    "scripts.refresh_data_bridge_current"
                ],
                actual,
            )

    def test_scheduler_cannot_import_one_shot_admin_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root / "scheduler" / "bad.py",
                "from scripts.apply_migrations import "
                "apply_migration_files\n",
            )

            actual = [
                violation.format(root)
                for violation
                in import_rules.repository_layer_import_violations(root)
            ]

            self.assertEqual(
                [
                    "scheduler/bad.py:1: forbidden layer import: "
                    "scheduler -> scripts.apply_migrations"
                ],
                actual,
            )

    def test_native_core_and_scheme_boundaries_remain_repo_wide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root / "schemes" / "alpha" / "core" / "model.py",
                "from shared.models import PredictionRecord\n",
            )
            self._write(
                root / "schemes" / "alpha" / "predict.py",
                "from scheduler.executor import execute_scheme\n"
                "from schemes.beta.core.model import predict\n",
            )

            actual = [
                violation.format(root)
                for violation in import_rules.repository_layer_import_violations(root)
            ]

            self.assertEqual(
                [
                    "schemes/alpha/core/model.py:1: forbidden layer import: "
                    "schemes.alpha -> shared.models",
                    "schemes/alpha/predict.py:1: forbidden layer import: "
                    "schemes.alpha -> scheduler.executor",
                    "schemes/alpha/predict.py:2: forbidden layer import: "
                    "schemes.alpha -> schemes.beta.core.model",
                ],
                actual,
            )

    def test_parent_relative_cross_scheme_import_is_resolved_from_source_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(
                root / "schemes" / "alpha" / "predict.py",
                "from .core.model import predict\n"
                "from ..beta.core import model\n",
            )
            self._write(
                root / "schemes" / "alpha" / "core" / "model.py",
                "from .helpers import build_features\n",
            )

            actual = [
                violation.format(root)
                for violation in import_rules.repository_layer_import_violations(root)
            ]

            self.assertEqual(
                [
                    "schemes/alpha/predict.py:2: forbidden layer import: "
                    "schemes.alpha -> schemes.beta.core",
                ],
                actual,
            )

    def test_retired_daily_capacity_admission_modules_are_absent(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        retired = (
            project_root / "scheduler" / "capacity_admission.py",
            project_root / "scheduler" / "capacity_runtime_admission.py",
        )

        self.assertEqual(
            [],
            [
                path.relative_to(project_root).as_posix()
                for path in retired
                if path.exists()
            ],
            "retired daily capacity admission modules must not return",
        )

    def test_retired_daily_capacity_gate_and_cli_are_absent(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        retired = (
            project_root / "scheduler" / "capacity_gate.py",
            project_root / "scripts" / "evaluate_daily_capacity_gate.py",
        )

        self.assertEqual(
            [],
            [
                path.relative_to(project_root).as_posix()
                for path in retired
                if path.exists()
            ],
            "retired offline daily capacity gate and CLI must not return",
        )

    def test_retired_daily_gray_and_v2_preflight_artifacts_are_absent(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        retired = (
            project_root / "scheduler" / "daily_gray_runner.py",
            project_root / "scheduler" / "daily_gray_launchd_policy.py",
            project_root / "scheduler" / "v2_daily_preflight.py",
            project_root / "deploy" / "daily_gray_launchd_policy_v1.json",
            project_root / "deploy" / "launchd" / "com.bond-factor-lab.daily-gray.plist",
            project_root / "deploy" / "launchd" / "com.bond-factor-lab.v2-preflight.plist",
        )

        self.assertEqual(
            [],
            [
                path.relative_to(project_root).as_posix()
                for path in retired
                if path.exists()
            ],
            "retired daily-gray and v2-preflight artifacts must not return",
        )

    def test_retired_daily_coordinator_symbols_are_not_exposed(self) -> None:
        retired = (
            "V2Release",
            "compute_v2_releases",
            "_required_release_offset",
            "decide_recovery",
        )

        self.assertEqual(
            [],
            [name for name in retired if hasattr(daily_coordinator, name)],
            "retired daily coordinator symbols must not return",
        )

    def test_retired_scheduler_startup_catchup_symbols_are_not_exposed(
        self,
    ) -> None:
        retired = (
            "run_ledger_startup_catchup",
            "run_startup_prediction_catchup",
            "_startup_prediction_catchup_due_jobs",
            "_prediction_run_exists",
            "_scheduled_datetime_for_date",
            "_cron_field_matches",
            "_cron_day_of_week_matches",
        )

        self.assertEqual(
            [],
            [name for name in retired if hasattr(scheduler_main, name)],
            "retired scheduler startup catchup symbols must not return",
        )

    def test_retired_repository_symbols_are_not_exposed(self) -> None:
        retired = (
            "insert_approved_blackbox_predictions",
            "insert_run_predictions",
            "finish_scheme_run",
            "seal_input_generation",
            "_validate_generation_seal_dependencies_conn",
            "bind_schedule_item_input_generation",
            "abandon_current_schedule_attempt",
            "require_daily_coordinator_mode",
            "BLACKBOX_BOOTSTRAP_BASELINE_TABLES",
            "BLACKBOX_BOOTSTRAP_GUARDED_TABLES",
        )

        self.assertEqual(
            [],
            [name for name in retired if hasattr(repository, name)],
            "retired repository symbols must not return",
        )

    def test_current_repository_has_no_layer_inversions(self) -> None:
        project_root = Path(__file__).resolve().parents[1]

        violations = import_rules.repository_layer_import_violations(project_root)

        self.assertEqual(
            [],
            [violation.format(project_root) for violation in violations],
            "repo-wide production import graph contains upward dependencies",
        )

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
