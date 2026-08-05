from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT_ROOT / "migrations" / "runner.py"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
CLI_PUBLIC_EXPORTS = {
    "MigrationHistoryError",
    "MigrationPartialApplyError",
    "MigrationPreflightError",
    "MigrationSQLParseError",
    "apply_migration_files",
    "apply_pending_migration_files",
    "inspect_applying_migration_017",
    "inspect_applying_migration_018",
    "inspect_applying_migration_019",
    "main",
    "recover_applying_migration_017",
    "recover_applying_migration_018",
    "recover_applying_migration_019",
    "split_sql_statements",
    "validate_release_migration_manifest",
}


class CanonicalMigrationRunnerModuleTests(unittest.TestCase):
    def _load_runner(self):
        spec = importlib.util.find_spec("migrations.runner")
        self.assertIsNotNone(
            spec,
            "canonical migration runner must exist at migrations.runner",
        )
        return importlib.import_module("migrations.runner")

    def test_canonical_module_owns_apply_entry_point(self) -> None:
        runner = self._load_runner()

        self.assertEqual(
            "migrations.runner",
            runner.apply_migration_files.__module__,
        )

    def test_runner_has_no_upward_or_cli_imports_and_creates_no_engine(
        self,
    ) -> None:
        self._load_runner()
        tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_roots.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)

        self.assertTrue(
            {"scheduler", "harness", "scripts", "argparse"}.isdisjoint(
                imported_roots
            ),
            f"runner has forbidden imports: {sorted(imported_roots)}",
        )
        self.assertTrue(
            {"create_engine", "create_engine_from_env"}.isdisjoint(
                called_names
            ),
            f"runner creates a default engine at import/runtime: "
            f"{sorted(called_names)}",
        )

    def test_admin_script_reexports_canonical_public_objects(self) -> None:
        runner = self._load_runner()
        cli = importlib.import_module("scripts.apply_migrations")

        for name in (
            "apply_migration_files",
            "apply_pending_migration_files",
            "split_sql_statements",
            "MigrationSQLParseError",
            "MigrationPreflightError",
            "MigrationPartialApplyError",
            "MigrationHistoryError",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(runner, name), getattr(cli, name))
        self.assertEqual("scripts.apply_migrations", cli.main.__module__)

    def test_admin_script_exports_only_explicit_compatibility_surface(
        self,
    ) -> None:
        cli = importlib.import_module("scripts.apply_migrations")

        self.assertEqual(
            CLI_PUBLIC_EXPORTS,
            set(getattr(cli, "__all__", ())),
        )
        self.assertFalse(hasattr(cli, "Path"))
        self.assertFalse(hasattr(cli, "text"))
        self.assertFalse(hasattr(cli, "Mapping"))

    def test_absolute_cli_help_works_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "apply_migrations.py"),
                    "--help",
                ],
                cwd=temporary,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("explicit --apply authorization", completed.stdout)

    def test_cli_and_runner_cold_import_without_cycle_in_both_orders(
        self,
    ) -> None:
        for modules in (
            ("scripts.apply_migrations", "migrations.runner"),
            ("migrations.runner", "scripts.apply_migrations"),
        ):
            with self.subTest(modules=modules):
                code = (
                    "import importlib, sys;"
                    f"sys.path.insert(0, {str(PROJECT_ROOT)!r});"
                    f"first=importlib.import_module({modules[0]!r});"
                    f"second=importlib.import_module({modules[1]!r});"
                    "runner=importlib.import_module('migrations.runner');"
                    "cli=importlib.import_module('scripts.apply_migrations');"
                    "assert cli.apply_migration_files is "
                    "runner.apply_migration_files"
                )
                with tempfile.TemporaryDirectory() as temporary:
                    completed = subprocess.run(
                        [sys.executable, "-I", "-c", code],
                        cwd=temporary,
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                self.assertEqual(
                    0,
                    completed.returncode,
                    completed.stderr,
                )

    def test_release_manifest_still_binds_exact_001_through_019(
        self,
    ) -> None:
        runner = self._load_runner()

        prepared = runner.validate_release_migration_manifest(
            sorted(MIGRATIONS_DIR.glob("*.sql")),
            manifest_path=runner.RELEASE_MIGRATION_MANIFEST_PATH,
        )

        self.assertEqual(
            list(range(1, 20)),
            [migration.version for migration in prepared],
        )

    def test_core_patch_targets_canonical_runner_not_wrapper_proxy(
        self,
    ) -> None:
        runner = self._load_runner()
        engine = Mock()
        paths = [MIGRATIONS_DIR / "018_schedule_run_started_at_nullable.sql"]

        with patch(
            "migrations.runner.apply_pending_migration_files"
        ) as apply_pending:
            runner.apply_migration_files(engine, paths)

        apply_pending.assert_called_once_with(engine, paths)


if __name__ == "__main__":
    unittest.main()
