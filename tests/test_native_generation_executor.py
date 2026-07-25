from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_native_generation_input_artifacts import _generation_context


class NativeGenerationSubprocessTests(unittest.TestCase):
    def test_frozen_native_mode_refuses_live_database_engine_creation(
        self,
    ) -> None:
        from shared import data_service
        from shared.input_artifacts import (
            NATIVE_INPUT_MODE,
            NATIVE_INPUT_MODE_ENV,
        )

        with (
            patch.dict(
                os.environ,
                {NATIVE_INPUT_MODE_ENV: NATIVE_INPUT_MODE},
                clear=False,
            ),
            patch.object(
                data_service,
                "_load_root_db_config",
            ) as load_config,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "forbids live database access",
            ):
                data_service.create_sqlalchemy_engine()

        load_config.assert_not_called()

    def test_v2_policy_timeout_is_a_hard_upper_bound(self) -> None:
        from types import SimpleNamespace

        from scheduler.executor import _effective_timeout_sec

        cfg = SimpleNamespace(
            scheme_id="daily_v2",
            runtime_type="blackbox_v2",
            schedule=SimpleNamespace(timeout_sec=3600),
        )

        self.assertEqual(_effective_timeout_sec(cfg, 120), 120)

    def test_native_subprocess_environment_is_allowlisted_and_excludes_secrets(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess

        captured: dict[str, str] = {}

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            captured.update(env)
            return CompletedProcess(cmd, 0, "[]", "")

        parent_environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/Users/tester",
            "TMPDIR": "/tmp/tester/",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "OMP_NUM_THREADS": "2",
            "LIWEI_0616_PHASE_A_CACHE_ROOT": "/tmp/cache",
            "BFL_SOURCE_DB_CONFIG_PATH": "/private/source-db.json",
            "BOND_DB_PASSWORD": "db-secret-sentinel",
            "DATABRIDGE_API_PASSWORD": "bridge-secret-sentinel",
            "ANTHROPIC_API_KEY": "api-secret-sentinel",
            "ANTHROPIC_AUTH_TOKEN": "auth-secret-sentinel",
            "AWS_SECRET_ACCESS_KEY": "cloud-secret-sentinel",
        }
        with (
            patch.dict(os.environ, parent_environment, clear=True),
            patch(
                "scheduler.executor._run_process_group",
                side_effect=fake_run,
            ),
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-24",
                native_generation=_generation_context(),
            )

        self.assertEqual(captured["PATH"], parent_environment["PATH"])
        self.assertEqual(captured["HOME"], parent_environment["HOME"])
        self.assertEqual(captured["LC_ALL"], parent_environment["LC_ALL"])
        self.assertEqual(captured["OMP_NUM_THREADS"], "2")
        self.assertEqual(
            captured["LIWEI_0616_PHASE_A_CACHE_ROOT"],
            "/tmp/cache",
        )
        for secret_name in (
            "BFL_SOURCE_DB_CONFIG_PATH",
            "BOND_DB_PASSWORD",
            "DATABRIDGE_API_PASSWORD",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
        ):
            self.assertNotIn(secret_name, captured)

    def test_native_subprocess_receives_exact_frozen_generation_environment(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess
        from shared import input_artifacts

        context = _generation_context()
        captured: dict[str, object] = {}

        def fake_run(
            cmd,
            *,
            cwd,
            env,
            timeout,
        ):
            from subprocess import CompletedProcess

            captured.update(
                {"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout}
            )
            return CompletedProcess(cmd, 0, "[]", "")

        with patch("scheduler.executor._run_process_group", side_effect=fake_run):
            records = run_scheme_subprocess(
                "daily_demo",
                "2026-07-24",
                algo_env="test-env",
                timeout_sec=33,
                native_generation=context,
                execution_token="attempt-7",
            )

        self.assertEqual(records, [])
        environment = captured["env"]
        self.assertEqual(
            environment[input_artifacts.NATIVE_INPUT_MODE_ENV],
            input_artifacts.NATIVE_INPUT_MODE,
        )
        self.assertEqual(
            environment[input_artifacts.NATIVE_MANIFEST_PATH_ENV],
            str(context.manifest_path),
        )
        self.assertEqual(
            environment[input_artifacts.NATIVE_GENERATION_ID_ENV],
            context.generation_id,
        )
        self.assertEqual(
            environment[input_artifacts.NATIVE_MANIFEST_SHA256_ENV],
            context.manifest_sha256,
        )
        self.assertEqual(
            environment[input_artifacts.NATIVE_BUSINESS_DATE_ENV],
            context.business_date,
        )
        self.assertEqual(
            environment[input_artifacts.NATIVE_FEATURE_DATE_ENV],
            context.feature_date,
        )
        self.assertEqual(
            environment[input_artifacts.SCHEDULE_EXECUTION_TOKEN_ENV],
            "attempt-7",
        )

    def test_approved_0629_live_source_uses_fence_without_frozen_db_mode(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess
        from shared import input_artifacts

        context = _generation_context()
        captured: dict[str, str] = {}
        runtime_config_path: Path | None = None

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            nonlocal runtime_config_path
            del cwd, timeout
            captured.update(env)
            runtime_config_path = Path(
                env["BFL_SOURCE_DB_CONFIG_PATH"]
            )
            self.assertTrue(runtime_config_path.is_file())
            self.assertNotEqual(
                runtime_config_path,
                original_config_path,
            )
            return CompletedProcess(cmd, 0, "[]", "")

        with tempfile.TemporaryDirectory() as tmpdir:
            from shared.source_runtime_database import (
                SourceRuntimeDatabaseConfig,
            )

            original_config_path = (
                Path(tmpdir) / "source-db.json"
            )
            original_config_path.write_text(
                "{}",
                encoding="utf-8",
            )
            original_config_path.chmod(0o600)
            database_config = SourceRuntimeDatabaseConfig(
                user="source_reader",
                password="source-test-secret",
                host="127.0.0.1",
                port=43306,
                database="bfl_source_test",
                charset="utf8mb4",
                config_path=original_config_path,
            )
            with (
                patch(
                    "scheduler.executor._run_process_group",
                    side_effect=fake_run,
                ),
                patch(
                    "scheduler.executor."
                    "load_source_runtime_database_config",
                    return_value=database_config,
                ),
            ):
                run_scheme_subprocess(
                    "daily_1y_xgb_1y13_0629",
                    "2026-07-24",
                    native_generation=context,
                    execution_token="compat-attempt",
                    live_source_compatibility=True,
                    live_source_package_sha256=(
                        "de63375f51810962ad10162444f93f7b"
                        "2fbde6236b7f4b9921734ae6b8fad1e3"
                    ),
                )

        self.assertNotIn(
            input_artifacts.NATIVE_INPUT_MODE_ENV,
            captured,
        )
        self.assertEqual(
            captured[input_artifacts.LIVE_SOURCE_INPUT_MODE_ENV],
            "live_source_0629",
        )
        self.assertEqual(
            captured[
                input_artifacts.LIVE_SOURCE_FENCE_GENERATION_ID_ENV
            ],
            context.generation_id,
        )
        self.assertEqual(
            captured[input_artifacts.LIVE_SOURCE_FEATURE_DATE_ENV],
            context.feature_date,
        )
        self.assertEqual(
            captured[
                input_artifacts.LIVE_SOURCE_PACKAGE_SHA256_ENV
            ],
            (
                "de63375f51810962ad10162444f93f7b"
                "2fbde6236b7f4b9921734ae6b8fad1e3"
            ),
        )
        self.assertEqual(
            captured["DAILY_0629_SOURCE_CACHE_DISABLE"],
            "1",
        )
        self.assertEqual(
            captured["BFL_SOURCE_DB_CONFIG_PATH"],
            str(runtime_config_path),
        )
        self.assertIsNotNone(runtime_config_path)
        self.assertFalse(runtime_config_path.exists())

    def test_cache_qualification_is_explicitly_validated_and_injected(
        self,
    ) -> None:
        import json

        from scheduler.executor import run_scheme_subprocess
        from shared.liwei_0616_cache_contract import (
            CACHE_USE_QUALIFICATION_ENV,
        )
        from shared.liwei_0616_phase_a_cache import PhaseACacheSpec
        from tests.test_liwei_0616_phase_a_cache_generations import (
            Liwei0616ImmutableCacheGenerationTests,
        )

        spec = PhaseACacheSpec(
            cache_family="liwei_0616_5y_v31",
            tenor="5Y",
            baselines=("STD",),
            baseline_configs={"STD": {"window": 200}},
            source_ic_screen_start="2024-01-01",
            horizon=5,
            purge_gap=5,
        )
        qualification = (
            Liwei0616ImmutableCacheGenerationTests
            ._trusted_qualification(
                spec,
                base_scheme_id="daily_demo",
            )
        )
        captured: dict[str, str] = {}

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            del cwd, timeout
            captured.update(env)
            return CompletedProcess(cmd, 0, "[]", "")

        with patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-24",
                native_generation=_generation_context(),
                cache_use_qualification=qualification,
            )
        self.assertEqual(
            json.loads(captured[CACHE_USE_QUALIFICATION_ENV]),
            qualification,
        )

        with self.assertRaisesRegex(
            ValueError,
            "cache_use_qualification is invalid",
        ):
            run_scheme_subprocess(
                "other_consumer",
                "2026-07-24",
                native_generation=_generation_context(),
                cache_use_qualification=qualification,
            )

    def test_native_subprocess_rejects_generation_from_another_occurrence(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess

        with self.assertRaisesRegex(
            ValueError,
            "business_date does not match predict_date",
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-25",
                native_generation=_generation_context(),
            )

    def test_unbound_subprocess_drops_inherited_generation_environment(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess
        from shared import input_artifacts

        captured: dict[str, str] = {}

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            for name in (
                input_artifacts.NATIVE_INPUT_MODE_ENV,
                input_artifacts.NATIVE_MANIFEST_PATH_ENV,
                input_artifacts.NATIVE_GENERATION_ID_ENV,
                input_artifacts.NATIVE_MANIFEST_SHA256_ENV,
                input_artifacts.NATIVE_BUSINESS_DATE_ENV,
                input_artifacts.NATIVE_FEATURE_DATE_ENV,
                input_artifacts.SCHEDULE_EXECUTION_TOKEN_ENV,
                "BOND_LIWEI_0616_CACHE_USE_QUALIFICATION",
            ):
                captured[name] = env.get(name, "<missing>")
            return CompletedProcess(cmd, 0, "[]", "")

        inherited = {
            input_artifacts.NATIVE_INPUT_MODE_ENV:
                input_artifacts.NATIVE_INPUT_MODE,
            input_artifacts.NATIVE_MANIFEST_PATH_ENV:
                "/tmp/stale/manifest.json",
            input_artifacts.NATIVE_GENERATION_ID_ENV: "stale-generation",
            input_artifacts.NATIVE_MANIFEST_SHA256_ENV: "f" * 64,
            input_artifacts.NATIVE_BUSINESS_DATE_ENV: "2026-07-23",
            input_artifacts.NATIVE_FEATURE_DATE_ENV: "2026-07-22",
            input_artifacts.SCHEDULE_EXECUTION_TOKEN_ENV: "stale-attempt",
            "BOND_LIWEI_0616_CACHE_USE_QUALIFICATION": (
                '{"forged":true}'
            ),
        }
        with (
            patch.dict(os.environ, inherited, clear=False),
            patch("scheduler.executor._run_process_group", side_effect=fake_run),
        ):
            run_scheme_subprocess("daily_demo", "2026-07-24")

        self.assertEqual(set(captured.values()), {"<missing>"})


if __name__ == "__main__":
    unittest.main()
