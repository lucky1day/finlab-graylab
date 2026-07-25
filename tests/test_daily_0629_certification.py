from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shared.data_contract import (
    NativeInputReadiness,
    SourceCommitEvidence,
)
from shared.daily_0629_predict_adapter import (
    DAILY_0629_INTERNAL_FIELDS,
)
from shared.daily_0629_source_evidence import (
    DAILY_0629_SOURCE_ROLE,
)
from shared.models import PredictionRecord


SCHEME_ID = "daily_1y_xgb_1y13_0629"
PREDICT_DATE = "2026-07-27"
FEATURE_DATE = "2026-07-24"
TARGET_DATE = "2026-07-27"
PACKAGE_SHA = "a" * 64
SOURCE_TOKEN = "b" * 64


def _record(*, active_score: float = 0.0) -> PredictionRecord:
    extra = {
        field: None
        for field in DAILY_0629_INTERNAL_FIELDS
    }
    extra.update(
        {
            "source_role": DAILY_0629_SOURCE_ROLE,
            "source_model_id": "1y-model",
            "source_package_hash": PACKAGE_SHA,
            "source_output_date": PREDICT_DATE,
            "source_prediction_date": FEATURE_DATE,
            "source_rdate": TARGET_DATE,
            "input_cutoff_date": FEATURE_DATE,
            "feature_date": FEATURE_DATE,
            "target_date": TARGET_DATE,
            "input_artifact_path": "/private/runtime/daily.csv",
            "input_artifact_source": "shared_data_service_daily",
            "frequency": "D1Y",
            "final_select_id": "1Y13",
            "candidate_id": "1y-candidate",
            "target_col": "TB1YWI0C",
            "prediction_mode": "active_abstain",
            "candidate_pred": 0,
            "raw_direction": 0,
            "prob_up": 0.5,
            "active_score": active_score,
            "threshold": 0.1,
        }
    )
    return PredictionRecord(
        scheme_id=SCHEME_ID,
        target_tenor="1Y",
        horizon=1,
        predict_date=PREDICT_DATE,
        feature_date=FEATURE_DATE,
        target_date=TARGET_DATE,
        predicted_direction=0,
        confidence=0.5,
        model_version="1Y13",
        extra=extra,
    )


def _source_evidence(token: str = SOURCE_TOKEN) -> SourceCommitEvidence:
    return SourceCommitEvidence(
        feature_date=FEATURE_DATE,
        source_commit_token=token,
        tables=(),
    )


def _dependencies(
    *,
    records: list[PredictionRecord] | None = None,
) -> dict[str, object]:
    engine = SimpleNamespace(dispose=Mock())
    guard_engine = SimpleNamespace(dispose=Mock())
    record_list = list(records or [_record(), _record()])
    table_state = {
        "t_scheme_runs": {
            "exists": True,
            "row_count": 7,
            "schema_sha256": "2" * 64,
            "content_sha256": "3" * 64,
        },
        "t_scheme_predictions": {
            "exists": True,
            "row_count": 11,
            "schema_sha256": "4" * 64,
            "content_sha256": "5" * 64,
        },
    }
    table_snapshotter = Mock(return_value=table_state)
    return {
        "engine": engine,
        "guard_engine": guard_engine,
        "runner": Mock(
            side_effect=[[record_list[0]], [record_list[1]]]
        ),
        "database_config": SimpleNamespace(cache_identity="c" * 64),
        "preflight": SimpleNamespace(
            database="bond_db",
            authenticated_user="bfl_source_readonly",
            tables=("api_wind_daily",),
        ),
        "calendar": SimpleNamespace(
            is_trading_day=lambda value: value == PREDICT_DATE,
        ),
        "context": SimpleNamespace(
            feature_date=FEATURE_DATE,
            target_date=TARGET_DATE,
        ),
        "readiness": NativeInputReadiness(
            feature_date=FEATURE_DATE,
            ready=True,
            missing_requirements=(),
        ),
        "source_evidence": _source_evidence(),
        "table_state": table_state,
        "table_snapshotter": table_snapshotter,
        "candidate": SimpleNamespace(
            git_head="d" * 40,
            tree_sha256="e" * 64,
            file_count=100,
            worktree_clean=False,
            git_path=Path("/usr/bin/git"),
            git_sha256="6" * 64,
        ),
        "runtime": SimpleNamespace(
            algo_env="forecast_env",
            conda_path=Path("/private/conda"),
            conda_sha256="f" * 64,
            conda_runtime_tree_sha256="c" * 64,
            conda_runtime_tree_file_count=301,
            conda_runtime_tree_bytes=16384,
            conda_configuration_sha256="d" * 64,
            source_python_path=Path("/private/forecast-python"),
            source_python_sha256="1" * 64,
            conda_explicit_sha256="7" * 64,
            conda_explicit_package_count=31,
            python_packages_sha256="8" * 64,
            python_package_count=29,
            runtime_tree_sha256="9" * 64,
            runtime_tree_file_count=117,
            runtime_tree_bytes=4096,
            control_python_path=Path("/private/service-python"),
            control_python_sha256="a" * 64,
            control_runtime_tree_sha256="b" * 64,
            control_runtime_tree_file_count=211,
            control_runtime_tree_bytes=8192,
        ),
    }


class Daily0629CertificationTests(unittest.TestCase):
    def test_real_certification_runs_1y_twice_and_writes_private_report(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            certify_daily_0629_source_execution,
        )

        deps = _dependencies()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir).resolve() / "evidence"
            output_dir.mkdir(mode=0o700)
            report_path = output_dir / "report.json"
            with (
                patch.dict(
                    os.environ,
                    {
                        "DAILY_0629_SOURCE_CACHE_DISABLE": "inherited",
                        "DAILY_N_JOBS": "8",
                    },
                    clear=False,
                ),
                _patched_dependencies(deps),
            ):
                report = certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=report_path,
                    runner=deps["runner"],
                )
                restored_source_cache = os.environ[
                    "DAILY_0629_SOURCE_CACHE_DISABLE"
                ]
                restored_daily_jobs = os.environ["DAILY_N_JOBS"]

            runner = deps["runner"]
            self.assertEqual(runner.call_count, 2)
            for call in runner.call_args_list:
                self.assertEqual(
                    call.args[:2],
                    (SCHEME_ID, PREDICT_DATE),
                )
                self.assertEqual(call.kwargs["algo_env"], "forecast_env")
                self.assertEqual(call.kwargs["timeout_sec"], 600)
                self.assertIs(
                    call.kwargs["source_database_config"],
                    deps["database_config"],
                )
            self.assertEqual(report["status"], "PASSED")
            self.assertEqual(report["scheme_id"], SCHEME_ID)
            self.assertEqual(report["target_count"], 1)
            self.assertEqual(report["feature_date"], FEATURE_DATE)
            self.assertEqual(report["target_date"], TARGET_DATE)
            self.assertEqual(
                report["source_commit_token"],
                SOURCE_TOKEN,
            )
            self.assertTrue(report["full_record_deterministic"])
            self.assertTrue(report["source_package_unchanged"])
            self.assertTrue(report["business_tables_unchanged"])
            self.assertEqual(
                report["business_table_guard_scope"],
                "all_platform_write_tables_full_content",
            )
            self.assertFalse(
                report["scheduled_compatibility_fence_exercised"]
            )
            self.assertEqual(
                report["scope"],
                "live_source_no_persist_observed_watermark",
            )
            self.assertEqual(
                report["candidate"]["tree_sha256"],
                "e" * 64,
            )
            self.assertEqual(
                report["runtime"]["algo_env"],
                "forecast_env",
            )
            self.assertEqual(
                report["runtime"]["runtime_tree_sha256"],
                "9" * 64,
            )
            self.assertEqual(
                report["runtime"]["conda_runtime_tree_sha256"],
                "c" * 64,
            )
            self.assertEqual(
                report["runtime"]["control_runtime_tree_sha256"],
                "b" * 64,
            )
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8")),
                report,
            )
            self.assertEqual(
                stat.S_IMODE(report_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                restored_source_cache,
                "inherited",
            )
            self.assertEqual(restored_daily_jobs, "8")
            deps["engine"].dispose.assert_called_once()
            deps["guard_engine"].dispose.assert_called_once()
            self.assertEqual(
                deps["table_snapshotter"].call_count,
                2,
            )
            for call in deps["table_snapshotter"].call_args_list:
                self.assertIs(
                    call.args[0],
                    deps["guard_engine"],
                )

    def test_certification_rejects_scheme_not_yet_admitted_for_this_stage(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            self.assertRaises(Daily0629CertificationError) as raised,
        ):
            certify_daily_0629_source_execution(
                scheme_id="daily_5y_lgbm_5y10_0629",
                predict_date=PREDICT_DATE,
                report_path=Path(tmpdir) / "report.json",
                runner=Mock(),
            )
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_SCHEME_NOT_ADMITTED",
        )

    def test_certification_redacts_missing_source_database_injection(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        candidate = _dependencies()["candidate"]
        runtime = _dependencies()["runtime"]
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir).resolve() / "evidence"
            output_dir.mkdir(mode=0o700)
            with (
                patch(
                    "harness.daily_0629_certification."
                    "_freeze_candidate_identity",
                    return_value=candidate,
                ),
                patch(
                    "harness.daily_0629_certification."
                    "_freeze_runtime_identity",
                    return_value=runtime,
                ),
                patch(
                    "harness.daily_0629_certification."
                    "_require_control_process_isolation",
                ),
                patch(
                    "harness.daily_0629_certification."
                    "load_source_runtime_database_config",
                    side_effect=RuntimeError("sensitive-path"),
                ),
                self.assertRaises(
                    Daily0629CertificationError
                ) as raised,
            ):
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=output_dir / "report.json",
                    runner=Mock(),
                )
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_SOURCE_DATABASE_PREFLIGHT_FAILED",
        )

    def test_certification_rejects_source_watermark_drift(self) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        deps = _dependencies()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir).resolve() / "evidence"
            output_dir.mkdir(mode=0o700)
            with (
                _patched_dependencies(
                    deps,
                    source_evidence_side_effect=(
                        _source_evidence(),
                        _source_evidence("d" * 64),
                    ),
                ),
                self.assertRaises(
                    Daily0629CertificationError
                ) as raised,
            ):
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=output_dir / "report.json",
                    runner=deps["runner"],
                )
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_SOURCE_WATERMARK_DRIFT",
        )

    def test_certification_rejects_full_extra_drift(self) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        deps = _dependencies(
            records=[
                _record(),
                _record(active_score=0.25),
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir).resolve() / "evidence"
            output_dir.mkdir(mode=0o700)
            with (
                _patched_dependencies(deps),
                self.assertRaises(
                    Daily0629CertificationError
                ) as raised,
            ):
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=output_dir / "report.json",
                    runner=deps["runner"],
                )
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_NONDETERMINISTIC_OUTPUT",
        )

    def test_certification_requires_every_known_internal_extra_field(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        first = _record()
        second = _record()
        assert first.extra is not None
        assert second.extra is not None
        first.extra.pop("fit_n")
        second.extra.pop("fit_n")
        deps = _dependencies(records=[first, second])
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir).resolve() / "evidence"
            output_dir.mkdir(mode=0o700)
            with (
                _patched_dependencies(deps),
                self.assertRaises(
                    Daily0629CertificationError
                ) as raised,
            ):
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=output_dir / "report.json",
                    runner=deps["runner"],
                )
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_EXTRA_CONTRACT_INVALID",
        )

    def test_certification_rejects_same_count_business_table_update(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        deps = _dependencies()
        changed = {
            name: dict(value)
            for name, value in deps["table_state"].items()
        }
        changed["t_scheme_runs"]["content_sha256"] = "9" * 64
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir).resolve() / "evidence"
            output_dir.mkdir(mode=0o700)
            with (
                _patched_dependencies(
                    deps,
                    table_state_side_effect=(
                        deps["table_state"],
                        changed,
                    ),
                ),
                self.assertRaises(
                    Daily0629CertificationError
                ) as raised,
            ):
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=output_dir / "report.json",
                    runner=deps["runner"],
                )
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_BUSINESS_TABLE_CHANGED",
        )

    def test_postflight_checks_all_guards_after_candidate_drift(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        deps = _dependencies()
        candidate_verifier = Mock(
            side_effect=(
                None,
                Daily0629CertificationError(
                    "DAILY_0629_CERT_CANDIDATE_DRIFT"
                ),
                None,
            )
        )
        runtime_verifier = Mock()
        package_hasher = Mock(return_value=PACKAGE_SHA)
        source_snapshotter = Mock(
            return_value=deps["source_evidence"]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir).resolve() / "evidence"
            output_dir.mkdir(mode=0o700)
            with (
                _patched_dependencies(deps),
                patch(
                    "harness.daily_0629_certification."
                    "_verify_candidate_identity",
                    candidate_verifier,
                ),
                patch(
                    "harness.daily_0629_certification."
                    "_verify_runtime_identity",
                    runtime_verifier,
                ),
                patch(
                    "harness.daily_0629_certification."
                    "source_package_tree_sha256",
                    package_hasher,
                ),
                patch(
                    "harness.daily_0629_certification."
                    "capture_source_commit_evidence",
                    source_snapshotter,
                ),
                self.assertRaises(
                    Daily0629CertificationError
                ) as raised,
            ):
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=output_dir / "report.json",
                    runner=deps["runner"],
                )

        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_CANDIDATE_DRIFT",
        )
        self.assertEqual(package_hasher.call_count, 2)
        self.assertEqual(source_snapshotter.call_count, 2)
        self.assertEqual(deps["table_snapshotter"].call_count, 2)
        self.assertGreaterEqual(runtime_verifier.call_count, 2)

    def test_postflight_runs_when_control_environment_exit_fails(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        @contextmanager
        def failing_environment(_runtime):
            yield
            raise RuntimeError("cleanup-failed")

        deps = _dependencies()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir).resolve() / "evidence"
            output_dir.mkdir(mode=0o700)
            with (
                _patched_dependencies(deps),
                patch(
                    "harness.daily_0629_certification."
                    "_certification_environment",
                    failing_environment,
                ),
                self.assertRaises(
                    Daily0629CertificationError
                ) as raised,
            ):
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=output_dir / "report.json",
                    runner=deps["runner"],
                )

        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_CONTROL_ENVIRONMENT_FAILED",
        )
        self.assertEqual(deps["runner"].call_count, 2)
        self.assertEqual(deps["table_snapshotter"].call_count, 2)

    def test_runtime_verifier_rejects_dependency_inventory_drift(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            Daily0629RuntimeIdentity,
            _verify_runtime_identity,
        )

        expected = Daily0629RuntimeIdentity(
            algo_env="forecast_env",
            conda_path=Path("/private/conda"),
            conda_sha256="1" * 64,
            conda_runtime_tree_sha256="9" * 64,
            conda_runtime_tree_file_count=14,
            conda_runtime_tree_bytes=8192,
            conda_configuration_sha256="a" * 64,
            source_python_path=Path("/private/python"),
            source_python_sha256="2" * 64,
            conda_explicit_sha256="3" * 64,
            conda_explicit_package_count=10,
            python_packages_sha256="4" * 64,
            python_package_count=11,
            runtime_tree_sha256="6" * 64,
            runtime_tree_file_count=12,
            runtime_tree_bytes=2048,
            control_python_path=Path("/private/service-python"),
            control_python_sha256="7" * 64,
            control_runtime_tree_sha256="8" * 64,
            control_runtime_tree_file_count=13,
            control_runtime_tree_bytes=4096,
        )
        changed = Daily0629RuntimeIdentity(
            **{
                **expected.__dict__,
                "python_packages_sha256": "5" * 64,
            }
        )
        with (
            patch(
                "harness.daily_0629_certification."
                "_freeze_runtime_identity",
                return_value=changed,
            ),
            self.assertRaises(
                Daily0629CertificationError
            ) as raised,
        ):
            _verify_runtime_identity(expected)
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_RUNTIME_DRIFT",
        )

    def test_identity_guard_rejects_source_database_config_drift(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            _collect_identity_guard_failures,
        )

        deps = _dependencies()
        expected = SimpleNamespace(
            cache_identity="1" * 64,
            password="first",
        )
        changed = SimpleNamespace(
            cache_identity="1" * 64,
            password="second",
        )
        with (
            patch(
                "harness.daily_0629_certification."
                "_verify_candidate_identity",
            ),
            patch(
                "harness.daily_0629_certification."
                "_verify_runtime_identity",
            ),
            patch(
                "harness.daily_0629_certification."
                "load_source_runtime_database_config",
                return_value=changed,
            ),
        ):
            failures = _collect_identity_guard_failures(
                deps["candidate"],
                deps["runtime"],
                source_database_config=expected,
            )

        self.assertEqual(
            failures,
            ["DAILY_0629_CERT_SOURCE_DATABASE_CONFIG_DRIFT"],
        )

    def test_runtime_tree_identity_binds_actual_bytes_and_modes(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            _runtime_tree_identity,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve() / "forecast-env"
            package = root / "lib" / "python" / "site-packages"
            package.mkdir(parents=True)
            module = package / "model.py"
            module.write_text("left\n", encoding="utf-8")
            baseline = _runtime_tree_identity(root)
            module.write_text("rght\n", encoding="utf-8")
            bytes_changed = _runtime_tree_identity(root)
            module.chmod(0o755)
            mode_changed = _runtime_tree_identity(root)

        self.assertNotEqual(
            baseline[0],
            bytes_changed[0],
        )
        self.assertNotEqual(
            bytes_changed[0],
            mode_changed[0],
        )
        self.assertEqual(baseline[1], 1)
        self.assertEqual(baseline[2], 5)

    def test_conda_probe_environment_rejects_shell_internal_overrides(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            _conda_process_environment,
        )

        with patch.dict(
            os.environ,
            {
                "CONDA_EXE": "/private/untrusted-conda",
                "CONDA_PYTHON_EXE": "/private/untrusted-python",
                "_CE_CONDA": "bogus",
                "_CE_M": "bogus",
            },
            clear=False,
        ):
            environment = _conda_process_environment(
                Path("/private/fixed/conda")
            )

        for name in (
            "CONDA_EXE",
            "CONDA_PYTHON_EXE",
            "_CE_CONDA",
            "_CE_M",
        ):
            self.assertNotIn(name, environment)

    def test_official_script_bootstraps_control_pycache_before_import(
        self,
    ) -> None:
        project_root = Path(__file__).resolve().parents[1]
        script = (
            project_root
            / "scripts"
            / "certify_daily_0629_source.py"
        )
        environment = dict(os.environ)
        for name in (
            "BFL_DAILY_0629_CERT_CONTROL_ISOLATION",
            "PYTHONPYCACHEPREFIX",
            "PYTHONDONTWRITEBYTECODE",
        ):
            environment.pop(name, None)
        completed = subprocess.run(
            [os.sys.executable, "-B", str(script), "--help"],
            cwd=project_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--authorize-real-run", completed.stdout)

    def test_candidate_identity_binds_bytes_mode_and_fixed_git(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            _freeze_candidate_identity,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve() / "candidate"
            project_root.mkdir()
            subprocess.run(
                ["/usr/bin/git", "init", "-q", str(project_root)],
                check=True,
            )
            tracked = project_root / "tracked.py"
            tracked.write_text("left\n", encoding="utf-8")
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(project_root),
                    "add",
                    "tracked.py",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/git",
                    "-C",
                    str(project_root),
                    "-c",
                    "user.name=Certification Test",
                    "-c",
                    "user.email=certification@example.invalid",
                    "commit",
                    "-qm",
                    "baseline",
                ],
                check=True,
            )
            (project_root / "harness").mkdir()
            with patch(
                "harness.daily_0629_certification._PROJECT_ROOT",
                project_root,
            ):
                baseline = _freeze_candidate_identity()
                tracked.write_text("rght\n", encoding="utf-8")
                bytes_changed = _freeze_candidate_identity()
                tracked.chmod(0o755)
                mode_changed = _freeze_candidate_identity()
                exclude = project_root / ".git" / "info" / "exclude"
                exclude.write_text(
                    exclude.read_text(encoding="utf-8")
                    + "\nharness/shadow.py\n",
                    encoding="utf-8",
                )
                shadow = project_root / "harness" / "shadow.py"
                shadow.write_text("VALUE = 1\n", encoding="utf-8")
                ignored_code_added = _freeze_candidate_identity()
                root_shadow = project_root / "json.py"
                root_shadow.write_text(
                    "loads = None\n",
                    encoding="utf-8",
                )
                with exclude.open("a", encoding="utf-8") as handle:
                    handle.write("json.py\n")
                root_shadow_added = _freeze_candidate_identity()
                legacy_pyc = project_root / "harness" / "legacy.pyc"
                legacy_pyc.write_bytes(b"legacy-bytecode")
                with self.assertRaises(
                    Daily0629CertificationError
                ) as legacy_raised:
                    _freeze_candidate_identity()
                legacy_pyc.unlink()
                external_package = (
                    Path(tmpdir).resolve() / "external-package"
                )
                external_package.mkdir()
                (
                    external_package / "__init__.py"
                ).write_text("VALUE = 91\n", encoding="utf-8")
                package_link = project_root / "shadowpkg"
                package_link.symlink_to(
                    external_package,
                    target_is_directory=True,
                )
                with self.assertRaises(
                    Daily0629CertificationError
                ) as symlink_raised:
                    _freeze_candidate_identity()

        self.assertNotEqual(
            baseline.tree_sha256,
            bytes_changed.tree_sha256,
        )
        self.assertNotEqual(
            bytes_changed.tree_sha256,
            mode_changed.tree_sha256,
        )
        self.assertNotEqual(
            mode_changed.tree_sha256,
            ignored_code_added.tree_sha256,
        )
        self.assertEqual(
            ignored_code_added.file_count,
            mode_changed.file_count + 1,
        )
        self.assertFalse(ignored_code_added.worktree_clean)
        self.assertNotEqual(
            ignored_code_added.tree_sha256,
            root_shadow_added.tree_sha256,
        )
        self.assertEqual(
            root_shadow_added.file_count,
            ignored_code_added.file_count + 1,
        )
        self.assertFalse(root_shadow_added.worktree_clean)
        self.assertEqual(
            legacy_raised.exception.code,
            "DAILY_0629_CERT_CANDIDATE_FILE_UNSAFE",
        )
        self.assertEqual(
            symlink_raised.exception.code,
            "DAILY_0629_CERT_CANDIDATE_FILE_UNSAFE",
        )
        self.assertEqual(baseline.git_path, Path("/usr/bin/git"))

    def test_report_path_rejects_symlink_ancestor(self) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            physical = Path(tmpdir).resolve()
            real_parent = physical / "real" / "evidence"
            real_parent.mkdir(parents=True, mode=0o700)
            link = physical / "link"
            link.symlink_to(physical / "real", target_is_directory=True)
            with self.assertRaises(
                Daily0629CertificationError
            ) as raised:
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=link / "evidence" / "report.json",
                    runner=Mock(),
                )
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_REPORT_ROOT_UNSAFE",
        )

    def test_report_path_must_stay_outside_candidate_repository(
        self,
    ) -> None:
        from harness.daily_0629_certification import (
            Daily0629CertificationError,
            certify_daily_0629_source_execution,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve() / "candidate"
            output_dir = project_root / "ignored-evidence"
            output_dir.mkdir(parents=True, mode=0o700)
            with (
                patch(
                    "harness.daily_0629_certification._PROJECT_ROOT",
                    project_root,
                ),
                self.assertRaises(
                    Daily0629CertificationError
                ) as raised,
            ):
                certify_daily_0629_source_execution(
                    scheme_id=SCHEME_ID,
                    predict_date=PREDICT_DATE,
                    report_path=output_dir / "report.json",
                    runner=Mock(),
                )
        self.assertEqual(
            raised.exception.code,
            "DAILY_0629_CERT_REPORT_PATH_UNSAFE",
        )

    def test_cli_requires_double_opt_in(self) -> None:
        from harness.daily_0629_certification import main

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                main(
                    [
                        "--scheme-id",
                        SCHEME_ID,
                        "--predict-date",
                        PREDICT_DATE,
                        "--report-path",
                        "/tmp/not-created.json",
                        "--authorize-real-run",
                    ]
                ),
                2,
            )

    def test_cli_redacts_unexpected_runtime_failure(self) -> None:
        from harness.daily_0629_certification import main

        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {"BFL_DAILY_0629_CERTIFY_REAL": "1"},
                clear=True,
            ),
            patch(
                "harness.daily_0629_certification."
                "certify_daily_0629_source_execution",
                side_effect=RuntimeError("must-not-leak-secret"),
            ),
            redirect_stdout(output),
        ):
            result = main(
                [
                    "--scheme-id",
                    SCHEME_ID,
                    "--predict-date",
                    PREDICT_DATE,
                    "--report-path",
                    "/tmp/not-created.json",
                    "--authorize-real-run",
                ]
            )

        self.assertEqual(result, 1)
        rendered = output.getvalue()
        self.assertIn(
            "DAILY_0629_CERT_INTERNAL_ERROR",
            rendered,
        )
        self.assertNotIn("must-not-leak-secret", rendered)


def _patched_dependencies(
    deps: dict[str, object],
    *,
    source_evidence_side_effect=None,
    table_state_side_effect=None,
) -> ExitStack:
    stack = ExitStack()
    table_snapshotter = deps["table_snapshotter"]
    if table_state_side_effect is not None:
        table_snapshotter.side_effect = table_state_side_effect
    replacements = {
        "load_source_runtime_database_config":
            Mock(return_value=deps["database_config"]),
        "preflight_source_runtime_database_access":
            Mock(return_value=deps["preflight"]),
        "create_input_engine": Mock(return_value=deps["engine"]),
        "create_engine_from_env": Mock(
            return_value=deps["guard_engine"]
        ),
        "get_calendar": Mock(return_value=deps["calendar"]),
        "build_daily_live_context": Mock(
            return_value=deps["context"]
        ),
        "inspect_native_input_readiness": Mock(
            return_value=deps["readiness"]
        ),
        "capture_source_commit_evidence": Mock(
            side_effect=source_evidence_side_effect,
            return_value=deps["source_evidence"],
        ),
        "_snapshot_business_table_state": table_snapshotter,
        "require_daily_0629_source_evidence": Mock(
            return_value=SimpleNamespace(
                source_package_path=Path("/private/source"),
                source_package_hash=PACKAGE_SHA,
                target_tenor="1Y",
                final_select_id="1Y13",
                candidate_id="1y-candidate",
                model_id="1y-model",
            )
        ),
        "source_package_tree_sha256": Mock(
            return_value=PACKAGE_SHA
        ),
        "_freeze_candidate_identity": Mock(
            return_value=deps["candidate"]
        ),
        "_require_control_process_isolation": Mock(),
        "_verify_candidate_identity": Mock(),
        "_freeze_runtime_identity": Mock(
            return_value=deps["runtime"]
        ),
        "_verify_runtime_identity": Mock(),
    }
    for name, replacement in replacements.items():
        stack.enter_context(
            patch(
                f"harness.daily_0629_certification.{name}",
                replacement,
            )
        )
    return stack
