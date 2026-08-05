from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

from harness.contracts import import_rules
from scheduler import (
    daily_coordinator,
    daily_runtime,
    direct_prediction,
    executor,
    repository,
)


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

    def test_direct_prediction_does_not_expose_resident_scheduler_symbols(
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
            "build_scheduler",
            "run_scheduled_prediction_job",
            "run_all_prediction_jobs",
            "run_daily_coordinator_job",
            "run_daily_recovery_tick_job",
            "run_daily_watchdog_job",
            "run_daily_heartbeat_job",
            "_sync_registry",
            "_preflight_source_runtime_database",
            "_staggered_prediction_jobs",
            "_automatic_prediction_schemes",
            "main",
        )

        self.assertEqual(
            [],
            [name for name in retired if hasattr(direct_prediction, name)],
            "direct helper must not retain resident scheduler symbols",
        )

    def test_retired_executor_aggregate_cli_is_absent(self) -> None:
        """自然 writer 只能按方案调用 execute_scheme，不得恢复全量 CLI。"""
        retired = (
            "execute_all",
            "_execute_all_scheduled",
            "_scheduled_aggregate_configuration_failure",
            "main",
            "_executor_exit_code",
        )
        project_root = Path(__file__).resolve().parents[1]
        executor_path = project_root / "scheduler" / "executor.py"

        self.assertEqual(
            [],
            [name for name in retired if hasattr(executor, name)],
            "retired broad executor CLI symbols must not return",
        )
        self.assertFalse(
            (project_root / "tests" / "test_executor_cli.py").exists(),
            "retired executor aggregate CLI tests must not return",
        )
        executor_tree = ast.parse(executor_path.read_text(encoding="utf-8"))
        main_guards = [
            node
            for node in ast.walk(executor_tree)
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
                and len(node.test.ops) == 1
                and isinstance(node.test.ops[0], ast.Eq)
                and len(node.test.comparators) == 1
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value == "__main__"
            )
        ]
        self.assertEqual(
            [],
            main_guards,
            "retired executor CLI module entry block must not return",
        )

    def test_standalone_scheduler_heartbeat_refresh_api_is_absent(
        self,
    ) -> None:
        """自然 one-shot 写入不得恢复独立 heartbeat 刷新控制面。"""
        project_root = Path(__file__).resolve().parents[1]
        paths = (
            project_root / "scheduler" / "daily_runtime.py",
            project_root / "scheduler" / "repository.py",
        )
        retired_symbols = {
            "heartbeat_tick",
            "run_scheduler_heartbeat",
            "read_scheduler_heartbeat",
        }

        self.assertFalse(
            hasattr(daily_runtime.DefaultDailyRuntimeServices, "heartbeat_tick"),
            "daily runtime must not retain a standalone heartbeat tick",
        )
        self.assertFalse(
            hasattr(daily_runtime, "run_scheduler_heartbeat"),
            "daily runtime must not retain a standalone heartbeat entrypoint",
        )
        self.assertFalse(
            hasattr(repository, "read_scheduler_heartbeat"),
            "repository must not retain the public heartbeat read wrapper",
        )

        for path in paths:
            with self.subTest(path=path.relative_to(project_root)):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                imported_names = {
                    alias.asname or alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    for alias in node.names
                }
                defined_names = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(
                        node,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    )
                }
                referenced_names = {
                    node.id
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Name)
                }
                string_literals = {
                    node.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                }
                self.assertEqual(
                    set(),
                    retired_symbols
                    & (
                        imported_names
                        | defined_names
                        | referenced_names
                        | string_literals
                    ),
                    source,
                )

    def test_backend_direct_paths_do_not_retain_daily_ledger_management(
        self,
    ) -> None:
        """backend 手动触发与健康 API 不得再导入旧 ledger 管理面。"""
        project_root = Path(__file__).resolve().parents[1]
        paths = (
            project_root / "backend" / "main.py",
            project_root / "scheduler" / "direct_prediction.py",
        )
        forbidden_names = {
            "DAILY_LEDGER",
            "_daily_coordinator_mode",
            "run_daily_operator_recovery_job",
            "read_dashboard_source_generation",
            "read_schedule_occurrence_snapshot",
            "read_scheduler_heartbeat",
            "require_current_daily_coordinator_identity",
        }

        for path in paths:
            with self.subTest(path=path.relative_to(project_root)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imported_names = {
                    alias.asname or alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    for alias in node.names
                }
                defined_names = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(
                        node,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    )
                }
                self.assertFalse(
                    forbidden_names & (imported_names | defined_names),
                    path.read_text(encoding="utf-8"),
                )

    def test_production_daily_health_retires_ledger_epoch_control_plane(self) -> None:
        """只读 health CLI 不得恢复 occurrence/epoch 第二控制面。"""
        project_root = Path(__file__).resolve().parents[1]
        retired = (
            project_root / "scheduler" / "daily_health.py",
            project_root / "tests" / "test_daily_health.py",
        )
        health_path = project_root / "scripts" / "check_production_daily_health.py"
        source = health_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        defined_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        retired_symbols = {
            "CoordinatorMode",
            "DAILY_HEARTBEAT_SERVICE",
            "DAILY_NOT_BEFORE",
            "_ledger_unavailable_projection",
            "_parse_health_datetime",
            "_resolve_coordinator_mode",
            "assert_daily_coordinator_epoch_matches_policy",
            "assert_daily_coordinator_epoch_payload_matches_current",
            "evaluate_ledger_daily_health",
            "load_ledger_daily_health",
            "project_daily_health",
            "read_deployment_daily_coordinator_mode",
            "read_schedule_health_envelope",
            "read_schedule_occurrence_snapshot",
            "read_scheduler_heartbeat",
            "require_current_daily_coordinator_identity",
        }

        self.assertEqual(
            [],
            [
                path.relative_to(project_root).as_posix()
                for path in retired
                if path.exists()
            ],
            "retired ledger health modules and tests must not return",
        )
        self.assertEqual(
            set(),
            retired_symbols & (imported_names | defined_names),
            source,
        )
        for symbol in retired_symbols:
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, source)
        self.assertNotIn("--coordinator-mode", source)
        self.assertNotIn("scheduler.daily_health", source)
        self.assertNotIn("shared.daily_coordinator_mode", source)

    def test_legacy_resident_scheduler_module_is_absent_from_current_paths(
        self,
    ) -> None:
        """当前生产路径只能保留无 APScheduler 的 backend 直调辅助模块。"""
        project_root = Path(__file__).resolve().parents[1]
        legacy_main = project_root / "scheduler" / "main.py"
        direct_prediction = project_root / "scheduler" / "direct_prediction.py"
        backend_source = (project_root / "backend" / "main.py").read_text(
            encoding="utf-8"
        )
        desired_plists = sorted(
            (project_root / "deploy" / "launchd").glob("*.plist")
        )

        self.assertFalse(
            legacy_main.exists(),
            "retired resident APScheduler module must not return",
        )
        self.assertTrue(
            direct_prediction.exists(),
            "backend direct/manual helpers require an isolated module",
        )
        self.assertNotIn("from scheduler.main import", backend_source)
        self.assertIn("from scheduler.direct_prediction import", backend_source)
        self.assertEqual(
            [],
            [
                path.relative_to(project_root).as_posix()
                for path in desired_plists
                if "scheduler.main" in path.read_text(encoding="utf-8")
            ],
            "repo desired launchd plists must not call the retired module",
        )
        tree = ast.parse(direct_prediction.read_text(encoding="utf-8"))
        apscheduler_imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("apscheduler")
        ]
        apscheduler_imports.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name.startswith("apscheduler")
        )
        self.assertEqual(
            [],
            apscheduler_imports,
            "direct/manual helper must not load resident scheduler dependencies",
        )

    def test_current_service_manifests_retire_apscheduler_and_tzlocal(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        current_manifests = (
            project_root / "pyproject.toml",
            project_root / "requirements-service.txt",
            project_root / "requirements-service.freeze.installable.txt",
        )

        for path in current_manifests:
            with self.subTest(path=path.relative_to(project_root)):
                content = path.read_text(encoding="utf-8").lower()
                self.assertNotIn("apscheduler", content)
                self.assertNotIn("tzlocal", content)

        raw_historical_freeze = (
            project_root / "requirements-service.freeze.txt"
        ).read_text(encoding="utf-8").lower()
        self.assertIn("apscheduler", raw_historical_freeze)
        self.assertIn("tzlocal", raw_historical_freeze)
        self.assertEqual(
            (project_root / "AGENTS.md").read_bytes(),
            (project_root / "CLAUDE.md").read_bytes(),
        )

        cloud_environment = (
            project_root / "docs" / "operations" / "CLOUD_ENVIRONMENT.md"
        ).read_text(encoding="utf-8").lower()
        bash_blocks = re.findall(r"```bash\n(.*?)\n```", cloud_environment, re.DOTALL)
        self.assertEqual(
            [],
            [block for block in bash_blocks if "apscheduler" in block],
            "current Cloud smoke commands must not import retired APScheduler",
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
