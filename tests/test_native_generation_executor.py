from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_native_generation_input_artifacts import _generation_context


class NativeGenerationSubprocessTests(unittest.TestCase):
    def test_ephemeral_native_runtime_sets_private_environment(self) -> None:
        from scheduler.executor import run_scheme_subprocess
        from shared.input_artifacts import EPHEMERAL_NATIVE_INPUT_ROOT_ENV
        from shared.liwei_0616_cache_contract import (
            CACHE_MUTATION_POLICY_ENV,
            CACHE_MUTATION_POLICY_PRIVATE_BUILD,
        )

        captured: dict[str, str] = {}

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            captured.update(env)
            return CompletedProcess(cmd, 0, "[]", "")

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ):
            root = Path(tmpdir).resolve()
            run_scheme_subprocess(
                "daily_demo",
                "2026-07-24",
                ephemeral_native_runtime_root=root,
            )

        self.assertEqual(
            captured[EPHEMERAL_NATIVE_INPUT_ROOT_ENV],
            str(root / "inputs"),
        )
        self.assertEqual(
            captured["LIWEI_0616_PHASE_A_CACHE_ROOT"],
            str(root / "phase-a-cache"),
        )
        self.assertEqual(
            captured[CACHE_MUTATION_POLICY_ENV],
            CACHE_MUTATION_POLICY_PRIVATE_BUILD,
        )

    def test_ephemeral_native_runtime_rejects_invalid_combinations(self) -> None:
        from scheduler.executor import run_configured_scheme

        native = SimpleNamespace(
            runtime_type="native_adapter",
            scheme_id="daily_demo",
        )
        blackbox = SimpleNamespace(
            runtime_type="blackbox_v2",
            input_source="data_bridge_current",
            scheme_id="blackbox_demo",
        )
        common = {
            "engine": object(),
            "algo_env": "forecast_env",
            "timeout_sec": 600,
        }
        with self.assertRaisesRegex(ValueError, "absolute"):
            run_configured_scheme(
                native,
                "2026-07-24",
                ephemeral_native_runtime_root="relative/root",
                **common,
            )
        with self.assertRaisesRegex(ValueError, "native_generation"):
            run_configured_scheme(
                native,
                "2026-07-24",
                native_generation=_generation_context(),
                ephemeral_native_runtime_root=Path("/private/native-gap"),
                **common,
            )
        with self.assertRaisesRegex(ValueError, "native_adapter"):
            run_configured_scheme(
                blackbox,
                "2026-07-24",
                ephemeral_native_runtime_root=Path("/private/native-gap"),
                **common,
            )

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
            patch.object(data_service, "_load_root_db_config") as load_config,
            self.assertRaisesRegex(
                RuntimeError,
                "forbids live database access",
            ),
        ):
            data_service.create_sqlalchemy_engine()

        load_config.assert_not_called()

    def test_v2_policy_timeout_is_a_hard_upper_bound(self) -> None:
        from scheduler.executor import _effective_timeout_sec

        cfg = SimpleNamespace(
            scheme_id="daily_v2",
            runtime_type="blackbox_v2",
            schedule=SimpleNamespace(timeout_sec=3600),
        )
        self.assertEqual(_effective_timeout_sec(cfg, 120), 120)

    def test_native_subprocess_environment_is_allowlisted(self) -> None:
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
            "BOND_DB_PASSWORD": "secret",
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
        self.assertEqual(
            captured["LIWEI_0616_PHASE_A_CACHE_ROOT"],
            "/tmp/cache",
        )
        self.assertNotIn("BOND_DB_PASSWORD", captured)

    def test_native_subprocess_receives_exact_generation_environment(
        self,
    ) -> None:
        from scheduler.executor import run_scheme_subprocess
        from shared import input_artifacts

        context = _generation_context()
        captured: dict[str, object] = {}

        def fake_run(cmd, *, cwd, env, timeout):
            from subprocess import CompletedProcess

            captured["env"] = env
            return CompletedProcess(cmd, 0, "[]", "")

        with patch(
            "scheduler.executor._run_process_group",
            side_effect=fake_run,
        ):
            records = run_scheme_subprocess(
                "daily_demo",
                "2026-07-24",
                native_generation=context,
                execution_token="attempt-7",
            )

        self.assertEqual(records, [])
        environment = captured["env"]
        self.assertEqual(
            environment[input_artifacts.NATIVE_GENERATION_ID_ENV],
            context.generation_id,
        )
        self.assertEqual(
            environment[input_artifacts.NATIVE_MANIFEST_SHA256_ENV],
            context.manifest_sha256,
        )
        self.assertEqual(
            environment[input_artifacts.SCHEDULE_EXECUTION_TOKEN_ENV],
            "attempt-7",
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
                input_artifacts.NATIVE_GENERATION_ID_ENV,
                input_artifacts.EPHEMERAL_NATIVE_INPUT_ROOT_ENV,
            ):
                captured[name] = env.get(name, "<missing>")
            return CompletedProcess(cmd, 0, "[]", "")

        inherited = {
            input_artifacts.NATIVE_INPUT_MODE_ENV: "inherited",
            input_artifacts.NATIVE_GENERATION_ID_ENV: "inherited",
            input_artifacts.EPHEMERAL_NATIVE_INPUT_ROOT_ENV: "/inherited",
        }
        with (
            patch.dict(os.environ, inherited, clear=False),
            patch(
                "scheduler.executor._run_process_group",
                side_effect=fake_run,
            ),
        ):
            run_scheme_subprocess("daily_demo", "2026-07-24")

        self.assertEqual(set(captured.values()), {"<missing>"})


if __name__ == "__main__":
    unittest.main()
