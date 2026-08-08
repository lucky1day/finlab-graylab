from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
            "BFL_SOURCE_DB_CONFIG_ROOT": "/private",
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
            "BFL_SOURCE_DB_CONFIG_ROOT",
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

    def test_signal_gap_current_snapshot_accepts_later_capture_and_exact_feature(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess
        from shared import input_artifacts

        context = replace(
            _generation_context(),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        captured: dict[str, object] = {}

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            captured.update(
                {"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout}
            )
            return CompletedProcess(cmd, 0, "[]", "")

        with patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ):
            try:
                records = run_scheme_subprocess(
                    "daily_demo",
                    "2026-07-28",
                    native_generation=context,
                    native_execution_mode=
                        "signal_gap_current_snapshot",
                    expected_native_feature_date="2026-07-27",
                )
            except (TypeError, ValueError) as exc:
                self.fail(
                    "explicit signal-gap current snapshot contract "
                    f"must be accepted: {exc}"
                )

        self.assertEqual(records, [])
        environment = captured["env"]
        self.assertEqual(
            environment[input_artifacts.NATIVE_BUSINESS_DATE_ENV],
            "2026-07-30",
        )
        self.assertEqual(
            environment[input_artifacts.NATIVE_FEATURE_DATE_ENV],
            "2026-07-27",
        )
        self.assertEqual(
            environment[
                "BOND_LIWEI_0616_CACHE_MUTATION_POLICY"
            ],
            "hit_only",
        )

    def test_signal_gap_cache_prewarm_uses_sealed_input_and_private_root(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess
        from shared import input_artifacts

        context = replace(
            _generation_context(),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        cache_root = Path("/private/signal-gap-cache/native-123")
        permit_path = cache_root / ".prewarm-permit-AbCdEf1234567890.json"
        captured: dict[str, object] = {}

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            captured.update(
                {"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout}
            )
            return CompletedProcess(cmd, 0, "[]", "")

        with patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ):
            records = run_scheme_subprocess(
                "liwei_0616_10y01_full_oos_k3_div_k10",
                "2026-07-28",
                native_generation=context,
                native_execution_mode="signal_gap_cache_prewarm",
                expected_native_feature_date="2026-07-27",
                phase_a_cache_root=cache_root,
                phase_a_cache_prewarm_permit=permit_path,
                phase_a_cache_prewarm_capability="permit_capability",
            )

        self.assertEqual(records, [])
        environment = captured["env"]
        self.assertEqual(
            environment[input_artifacts.NATIVE_MANIFEST_PATH_ENV],
            str(context.manifest_path),
        )
        self.assertEqual(
            environment["LIWEI_0616_PHASE_A_CACHE_ROOT"],
            str(cache_root),
        )
        self.assertEqual(
            environment["BOND_LIWEI_0616_CACHE_MUTATION_POLICY"],
            "prewarm",
        )
        self.assertEqual(
            environment["BOND_LIWEI_0616_SIGNAL_GAP_PREWARM_PERMIT"],
            str(permit_path),
        )
        self.assertEqual(
            environment[
                "BOND_LIWEI_0616_SIGNAL_GAP_PREWARM_CAPABILITY"
            ],
            "permit_capability",
        )

    def test_signal_gap_cache_prewarm_rejects_nonpublisher(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess

        context = replace(
            _generation_context(),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        with self.assertRaisesRegex(
            ValueError,
            "requires an approved publisher",
        ):
            run_scheme_subprocess(
                "liwei_0616_10y01_cons_say_k3_div_k10",
                "2026-07-28",
                native_generation=context,
                native_execution_mode="signal_gap_cache_prewarm",
                expected_native_feature_date="2026-07-27",
                phase_a_cache_root=Path("/private/signal-gap-cache/native-123"),
            )

    def test_signal_gap_cache_prewarm_requires_one_time_permit(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        context = replace(
            _generation_context(),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        with self.assertRaisesRegex(ValueError, "cache root and permit"):
            run_scheme_subprocess(
                "liwei_0616_10y01_full_oos_k3_div_k10",
                "2026-07-28",
                native_generation=context,
                native_execution_mode="signal_gap_cache_prewarm",
                expected_native_feature_date="2026-07-27",
                phase_a_cache_root=Path("/private/signal-gap-cache/native-123"),
            )

    def test_signal_gap_archived_accepts_exact_normal_generation_hit_only(
        self,
    ) -> None:
        from scheduler.executor import (
            NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED,
            run_scheme_subprocess,
        )
        from shared import input_artifacts

        context = _generation_context()
        captured: dict[str, object] = {}

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            captured.update(
                {"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout}
            )
            return CompletedProcess(cmd, 0, "[]", "")

        with patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ):
            records = run_scheme_subprocess(
                "daily_demo",
                context.business_date,
                native_generation=context,
                native_execution_mode=(
                    NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED
                ),
                expected_native_feature_date=context.feature_date,
            )

        self.assertEqual(records, [])
        environment = captured["env"]
        self.assertEqual(
            environment[input_artifacts.NATIVE_BUSINESS_DATE_ENV],
            context.business_date,
        )
        self.assertEqual(
            environment[input_artifacts.NATIVE_FEATURE_DATE_ENV],
            context.feature_date,
        )
        self.assertEqual(
            environment[
                "BOND_LIWEI_0616_CACHE_MUTATION_POLICY"
            ],
            "hit_only",
        )

    def test_signal_gap_archived_rejects_non_exact_business_date(
        self,
    ) -> None:
        from scheduler.executor import (
            NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED,
            run_scheme_subprocess,
        )

        context = _generation_context()
        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(
                ValueError,
                "business_date does not match predict_date",
            ),
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-25",
                native_generation=context,
                native_execution_mode=(
                    NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED
                ),
                expected_native_feature_date=context.feature_date,
            )
        run_process.assert_not_called()

    def test_signal_gap_archived_rejects_current_snapshot_exporter(
        self,
    ) -> None:
        from scheduler.executor import (
            NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED,
            run_scheme_subprocess,
        )

        context = replace(
            _generation_context(),
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(ValueError, "exporter_version"),
        ):
            run_scheme_subprocess(
                "daily_demo",
                context.business_date,
                native_generation=context,
                native_execution_mode=(
                    NATIVE_EXECUTION_MODE_SIGNAL_GAP_ARCHIVED
                ),
                expected_native_feature_date=context.feature_date,
            )
        run_process.assert_not_called()

    def test_signal_gap_current_snapshot_rejects_wrong_exporter(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        context = replace(
            _generation_context(),
            business_date="2026-07-30",
            feature_date="2026-07-27",
        )
        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(ValueError, "exporter_version"),
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-28",
                native_generation=context,
                native_execution_mode="signal_gap_current_snapshot",
                expected_native_feature_date="2026-07-27",
            )
        run_process.assert_not_called()

    def test_signal_gap_current_snapshot_rejects_capture_not_after_predict(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess

        context = replace(
            _generation_context(),
            business_date="2026-07-28",
            feature_date="2026-07-27",
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(
                ValueError,
                "capture date must be after predict_date",
            ),
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-28",
                native_generation=context,
                native_execution_mode="signal_gap_current_snapshot",
                expected_native_feature_date="2026-07-27",
            )
        run_process.assert_not_called()

    def test_signal_gap_current_snapshot_rejects_feature_drift(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        context = replace(
            _generation_context(),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(
                ValueError,
                "feature_date does not match expected",
            ),
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-28",
                native_generation=context,
                native_execution_mode="signal_gap_current_snapshot",
                expected_native_feature_date="2026-07-28",
            )
        run_process.assert_not_called()

    def test_signal_gap_current_snapshot_requires_expected_feature(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        context = replace(
            _generation_context(),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(
                ValueError,
                "expected_native_feature_date is required",
            ),
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-28",
                native_generation=context,
                native_execution_mode="signal_gap_current_snapshot",
            )
        run_process.assert_not_called()

    def test_run_configured_scheme_forwards_signal_gap_native_contract(
        self,
    ) -> None:
        from scheduler.executor import run_configured_scheme

        context = replace(
            _generation_context(),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        cfg = SimpleNamespace(
            runtime_type="native_adapter",
            scheme_id="daily_demo",
        )
        with patch(
            "scheduler.executor.run_scheme_subprocess",
            return_value=[],
        ) as subprocess_runner:
            try:
                result = run_configured_scheme(
                    cfg,
                    "2026-07-28",
                    engine=object(),
                    algo_env="forecast_env",
                    timeout_sec=600,
                    native_generation=context,
                    native_execution_mode=
                        "signal_gap_current_snapshot",
                    expected_native_feature_date="2026-07-27",
                )
            except TypeError as exc:
                self.fail(
                    "run_configured_scheme must forward the explicit "
                    f"Native execution contract: {exc}"
                )

        self.assertEqual(result, [])
        self.assertIs(
            subprocess_runner.call_args.kwargs["native_generation"],
            context,
        )
        self.assertEqual(
            subprocess_runner.call_args.kwargs[
                "native_execution_mode"
            ],
            "signal_gap_current_snapshot",
        )
        self.assertEqual(
            subprocess_runner.call_args.kwargs[
                "expected_native_feature_date"
            ],
            "2026-07-27",
        )

    def test_signal_gap_mode_requires_native_generation(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(
                ValueError,
                "requires native_generation",
            ),
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-28",
                native_execution_mode="signal_gap_current_snapshot",
                expected_native_feature_date="2026-07-27",
            )
        run_process.assert_not_called()

    def test_scheduled_mode_rejects_gap_expected_feature(self) -> None:
        from scheduler.executor import run_scheme_subprocess

        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(
                ValueError,
                "only valid for signal-gap",
            ),
        ):
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-24",
                native_generation=_generation_context(),
                expected_native_feature_date="2026-07-23",
            )
        run_process.assert_not_called()

    def test_scheduled_mode_rejects_signal_gap_exporter_even_when_date_matches(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess

        context = replace(
            _generation_context(),
            exporter_version="native-signal-gap-current-snapshot-v1",
        )
        with (
            patch(
                "scheduler.executor._run_process_group",
                return_value=SimpleNamespace(stdout="[]"),
            ) as run_process,
            self.assertRaisesRegex(
                ValueError,
                "requires explicit signal-gap",
            ),
        ):
            run_scheme_subprocess(
                "daily_demo",
                context.business_date,
                native_generation=context,
            )
        run_process.assert_not_called()

    def test_blackbox_rejects_signal_gap_native_contract(self) -> None:
        from scheduler.executor import run_configured_scheme

        cfg = SimpleNamespace(
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            scheme_id="blackbox_demo",
        )
        with (
            patch(
                "scheduler.executor.run_blackbox_scheme_subprocess",
                return_value=[],
            ) as blackbox_runner,
            self.assertRaisesRegex(
                ValueError,
                "only valid for native_adapter",
            ),
        ):
            run_configured_scheme(
                cfg,
                "2026-07-28",
                engine=object(),
                algo_env="forecast_env",
                timeout_sec=600,
                native_execution_mode="signal_gap_current_snapshot",
                expected_native_feature_date="2026-07-27",
            )
        blackbox_runner.assert_not_called()

    def test_approved_0629_live_source_uses_fence_without_frozen_db_mode(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess
        from shared import input_artifacts

        context = _generation_context()
        captured: dict[str, str] = {}
        runtime_config_path: Path | None = None
        runtime_config_root: Path | None = None

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess
            from shared.source_runtime_database import (
                load_source_runtime_database_config,
            )

            nonlocal runtime_config_path, runtime_config_root
            del cwd, timeout
            captured.update(env)
            runtime_config_root = Path(
                env["BFL_SOURCE_DB_CONFIG_ROOT"]
            )
            runtime_config_path = Path(
                env["BFL_SOURCE_DB_CONFIG_PATH"]
            )
            self.assertEqual(
                runtime_config_root,
                runtime_config_path.parent,
            )
            self.assertTrue(runtime_config_root.is_dir())
            self.assertTrue(runtime_config_path.is_file())
            self.assertNotEqual(
                runtime_config_path,
                original_config_path,
            )
            reloaded_config = load_source_runtime_database_config(env)
            self.assertEqual(
                reloaded_config.cache_identity,
                database_config.cache_identity,
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
                    side_effect=AssertionError(
                        "explicit preflighted config must not be reloaded"
                    ),
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
                    source_database_config=database_config,
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
        self.assertEqual(
            captured["BFL_SOURCE_DB_CONFIG_ROOT"],
            str(runtime_config_root),
        )
        self.assertIsNotNone(runtime_config_path)
        self.assertIsNotNone(runtime_config_root)
        self.assertFalse(runtime_config_path.exists())
        self.assertFalse(runtime_config_root.exists())

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
        }
        with (
            patch.dict(os.environ, inherited, clear=False),
            patch("scheduler.executor._run_process_group", side_effect=fake_run),
        ):
            run_scheme_subprocess("daily_demo", "2026-07-24")

        self.assertEqual(set(captured.values()), {"<missing>"})


if __name__ == "__main__":
    unittest.main()
