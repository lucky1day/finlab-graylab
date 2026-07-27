from __future__ import annotations

import json
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class GenerationNativeDailyCertificationTests(unittest.TestCase):
    def test_generation_calendar_requires_consecutive_trading_days(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _require_daily_generation_calendar,
        )

        generation = self._generation()
        calendar = mock.Mock()
        calendar.is_trading_day.side_effect = (
            lambda value: value != generation.business_date
        )

        with (
            mock.patch(
                "harness.native_daily_certification.get_calendar",
                return_value=calendar,
            ),
            self.assertRaises(NativeDailyCertificationError) as caught,
        ):
            _require_daily_generation_calendar(generation)

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_GENERATION_CALENDAR_INVALID",
        )

    def test_policy_matrix_is_exactly_fourteen_items_and_eighteen_targets(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            load_generation_native_matrix,
        )

        matrix = load_generation_native_matrix()

        self.assertEqual(len(matrix), 14)
        self.assertEqual(
            sum(len(item.target_tenors) for item in matrix),
            18,
        )
        self.assertEqual(
            {item.scheme_id for item in matrix},
            {
                "daily_5y_2_v28",
                "daily_7y_1_v28",
                "liwei_0616_10y01_cons_say_k3_div_k10",
                "liwei_0616_10y01_full_oos_k3_div_k10",
                "liwei_0616_10y02_cons_say_k3_div_k5",
                "liwei_0616_5y01_full_oos_k3_div_k10",
                "liwei_0616_5y_auc_static_all_k3_div_k10",
                "liwei_0616_5y_auc_yearly_all_k3_div_k10",
                "liwei_0616_5y_ic_yearly_all_k3_div_k10",
                "liwei_0616_7y01_cons_say_k3_div_k10",
                "liwei_0616_7y03_cons_all_k3_div_k8",
                "liwei_0616_cons_sda_k3_div_k10",
                "t1_daily",
                "t5_daily",
            },
        )
        self.assertTrue(
            all(item.input_compatibility == "generation_v1" for item in matrix)
        )
        self.assertTrue(
            all(item.required_internal_fields for item in matrix)
        )

    def test_no_persist_certification_compares_independent_real_run_modes(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            certify_generation_native_daily,
            load_generation_native_matrix,
        )

        generation = self._generation()
        matrix = load_generation_native_matrix()
        calls: list[tuple[str, str]] = []

        def worker(item, mode, **_kwargs):
            calls.append((item.scheme_id, mode))
            return self._worker_result(
                generation,
                item,
                mode=mode,
                output_root=_kwargs["output_root"],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir).resolve() / "certification"
            output_root.mkdir(mode=0o700)
            report = certify_generation_native_daily(
                manifest_path=generation.manifest_path,
                output_root=output_root,
                worker=worker,
                generation_opener=lambda *_args, **_kwargs: generation,
            )
            report_path = output_root / "certification.json"

            self.assertTrue(report_path.is_file())
            self.assertEqual(
                stat.S_IMODE(report_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8")),
                report,
            )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["persistence"], "none")
        self.assertEqual(report["network_policy"], "test_double")
        self.assertEqual(report["evidence_scope"], "test_double")
        self.assertEqual(report["expected_scheme_count"], 14)
        self.assertEqual(report["expected_target_count"], 18)
        self.assertEqual(report["completed_scheme_count"], 14)
        self.assertEqual(report["completed_target_count"], 18)
        self.assertEqual(
            report["generation"]["generation_id"],
            generation.generation_id,
        )
        self.assertEqual(
            report["generation"]["manifest_sha256"],
            generation.manifest_sha256,
        )
        for item in matrix:
            expected_modes = (
                ("cold", "warm_build", "warm_hit")
                if item.is_liwei
                else ("first", "second")
            )
            self.assertEqual(
                tuple(
                    mode
                    for scheme_id, mode in calls
                    if scheme_id == item.scheme_id
                ),
                expected_modes,
            )
        self.assertTrue(
            all(
                scheme["cache_lineage"] is not None
                for scheme in report["schemes"]
                if scheme["scheme_id"].startswith("liwei_0616_")
            )
        )

    def test_internal_field_drift_fails_closed_without_passing_report(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            certify_generation_native_daily,
        )

        generation = self._generation()
        call_count: dict[str, int] = {}

        def worker(item, mode, **_kwargs):
            result = self._worker_result(
                generation,
                item,
                mode=mode,
                output_root=_kwargs["output_root"],
            )
            call_count[item.scheme_id] = (
                call_count.get(item.scheme_id, 0) + 1
            )
            if (
                item.scheme_id == "daily_5y_2_v28"
                and mode == "second"
            ):
                result["records"][0]["extra"]["vote_score"] = 0.99
            return result

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir).resolve() / "certification"
            output_root.mkdir(mode=0o700)
            with self.assertRaises(
                NativeDailyCertificationError
            ) as caught:
                certify_generation_native_daily(
                    manifest_path=generation.manifest_path,
                    output_root=output_root,
                    worker=worker,
                    generation_opener=lambda *_args, **_kwargs: generation,
                )

            self.assertFalse(
                (output_root / "certification.json").exists()
            )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_COMPARE_MISMATCH",
        )
        self.assertEqual(
            caught.exception.scheme_id,
            "daily_5y_2_v28",
        )

    def test_network_or_generation_drift_is_rejected(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            certify_generation_native_daily,
        )

        generation = self._generation()
        cases = (
            ("network_attempts", 1, "NATIVE_CERT_NETWORK_ATTEMPT"),
            (
                "manifest_sha256",
                "f" * 64,
                "NATIVE_CERT_GENERATION_DRIFT",
            ),
        )
        for field, value, expected_code in cases:
            with self.subTest(field=field):
                first = True

                def worker(item, mode, **_kwargs):
                    nonlocal first
                    result = self._worker_result(
                        generation,
                        item,
                        mode=mode,
                        output_root=_kwargs["output_root"],
                    )
                    if first:
                        result[field] = value
                        first = False
                    return result

                with tempfile.TemporaryDirectory() as tmpdir:
                    output_root = (
                        Path(tmpdir).resolve() / "certification"
                    )
                    output_root.mkdir(mode=0o700)
                    with self.assertRaises(
                        NativeDailyCertificationError
                    ) as caught:
                        certify_generation_native_daily(
                            manifest_path=generation.manifest_path,
                            output_root=output_root,
                            worker=worker,
                            generation_opener=(
                                lambda *_args, **_kwargs: generation
                            ),
                        )
                self.assertEqual(
                    caught.exception.code,
                    expected_code,
                )

    def test_warm_cache_native_generation_drift_is_rejected(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            certify_generation_native_daily,
        )

        generation = self._generation()

        def worker(item, mode, **_kwargs):
            result = self._worker_result(
                generation,
                item,
                mode=mode,
                output_root=_kwargs["output_root"],
            )
            if item.is_liwei and mode == "warm_build":
                result["records"][0]["extra"]["phase_a_cache"][
                    "generation_acceptance"
                ]["native_generation"]["generation_id"] = (
                    "native-wrong-generation"
                )
            return result

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir).resolve() / "certification"
            output_root.mkdir(mode=0o700)
            with self.assertRaises(
                NativeDailyCertificationError
            ) as caught:
                certify_generation_native_daily(
                    manifest_path=generation.manifest_path,
                    output_root=output_root,
                    worker=worker,
                    generation_opener=lambda *_args, **_kwargs: generation,
                )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_GENERATION_DRIFT",
        )

    def test_runtime_cache_path_is_ignored_but_cache_family_is_compared(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            _algorithm_projection,
        )

        first = [
            {
                "target_tenor": "5Y",
                "horizon": 5,
                "extra": {
                    "vote_score": -1,
                    "phase_a_cache_root": "/isolated/first",
                    "phase_a_cache_family": "family-a",
                },
            }
        ]
        second = [
            {
                "target_tenor": "5Y",
                "horizon": 5,
                "extra": {
                    "vote_score": -1,
                    "phase_a_cache_root": "/isolated/second",
                    "phase_a_cache_family": "family-a",
                },
            }
        ]

        self.assertEqual(
            _algorithm_projection(first),
            _algorithm_projection(second),
        )
        second[0]["extra"]["phase_a_cache_family"] = "family-b"
        self.assertNotEqual(
            _algorithm_projection(first),
            _algorithm_projection(second),
        )

    def test_empty_internal_mapping_is_rejected(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _validate_worker_result,
            load_generation_native_matrix,
        )

        generation = self._generation()
        item = next(
            candidate
            for candidate in load_generation_native_matrix()
            if candidate.scheme_id == "t5_daily"
        )
        result = self._worker_result(
            generation,
            item,
            mode="first",
        )
        result["records"][0]["extra"]["vote_signals"] = {}

        with self.assertRaises(
            NativeDailyCertificationError
        ) as caught:
            _validate_worker_result(
                result,
                item=item,
                mode="first",
                expected_generation={
                    "generation_id": generation.generation_id,
                    "manifest_sha256": generation.manifest_sha256,
                    "dataset_content_id": generation.dataset_content_id,
                    "business_date": generation.business_date,
                    "feature_date": generation.feature_date,
                    "schema_version": generation.schema_version,
                    "exporter_version": generation.exporter_version,
                },
                business_date=generation.business_date,
                feature_date=generation.feature_date,
                output_root=Path("/isolated"),
            )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
        )

    def test_invalid_nested_internal_values_are_rejected(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _generation_binding,
            _validate_worker_result,
            load_generation_native_matrix,
        )

        generation = self._generation()
        matrix = load_generation_native_matrix()
        t5 = next(
            item for item in matrix if item.scheme_id == "t5_daily"
        )
        t1 = next(
            item for item in matrix if item.scheme_id == "t1_daily"
        )
        v28 = next(
            item
            for item in matrix
            if item.scheme_id == "daily_5y_2_v28"
        )
        liwei = next(item for item in matrix if item.is_liwei)
        cases = (
            (t5, "first", "vote_signals", {"STD": None}),
            (t5, "first", "vote_signals", {"STD": "bad"}),
            (t5, "first", "vote_signals", {"   ": -1}),
            (t5, "first", "model_pred", 99),
            (t1, "first", "base_pred", 99),
            (t1, "first", "threshold", 1.5),
            (v28, "first", "ens_prob", 1.5),
            (liwei, "cold", "baseline_signs", {"STD": 2}),
            (
                liwei,
                "cold",
                "baseline_scores",
                {"STD": float("inf")},
            ),
        )
        for item, mode, field, value in cases:
            with self.subTest(
                scheme_id=item.scheme_id,
                field=field,
                value=value,
            ):
                result = self._worker_result(
                    generation,
                    item,
                    mode=mode,
                )
                result["records"][0]["extra"][field] = value
                with self.assertRaises(
                    NativeDailyCertificationError
                ) as caught:
                    _validate_worker_result(
                        result,
                        item=item,
                        mode=mode,
                        expected_generation=(
                            _generation_binding(generation)
                        ),
                        business_date=generation.business_date,
                        feature_date=generation.feature_date,
                        output_root=Path("/isolated"),
                    )
                self.assertEqual(
                    caught.exception.code,
                    "NATIVE_CERT_INTERNAL_SCOPE_MISSING",
                )

    def test_t5_cold_fallback_allows_nullable_probability(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _generation_binding,
            _validate_worker_result,
            load_generation_native_matrix,
        )

        generation = self._generation()
        item = next(
            candidate
            for candidate in load_generation_native_matrix()
            if candidate.scheme_id == "t5_daily"
        )
        result = self._worker_result(
            generation,
            item,
            mode="first",
        )
        for record in result["records"]:
            record["confidence"] = None
            record["extra"]["threshold"] = None
            record["extra"]["decision"] = "cold_fallback"

        validated = _validate_worker_result(
            result,
            item=item,
            mode="first",
            expected_generation=_generation_binding(generation),
            business_date=generation.business_date,
            feature_date=generation.feature_date,
            output_root=Path("/isolated"),
        )

        self.assertEqual(len(validated), 4)
        result["records"][0]["confidence"] = 0.5
        with self.assertRaises(
            NativeDailyCertificationError
        ) as caught:
            _validate_worker_result(
                result,
                item=item,
                mode="first",
                expected_generation=_generation_binding(generation),
                business_date=generation.business_date,
                feature_date=generation.feature_date,
                output_root=Path("/isolated"),
            )
        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_RECORDS_INVALID",
        )

    def test_liwei_fallback_baseline_extends_score_scope(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _generation_binding,
            _validate_worker_result,
            load_generation_native_matrix,
        )

        generation = self._generation()
        item = next(
            candidate
            for candidate in load_generation_native_matrix()
            if candidate.is_liwei
        )
        valid = self._worker_result(
            generation,
            item,
            mode="cold",
        )
        extra = valid["records"][0]["extra"]
        extra["vote_baselines"] = ["STD", "ACCWT", "V55_7Y"]
        extra["fallback_baseline"] = "DIV"
        extra["baseline_signs"] = {
            "STD": -1,
            "ACCWT": 1,
            "V55_7Y": -1,
            "DIV": 1,
        }
        extra["baseline_scores"] = {
            "STD": -0.1,
            "ACCWT": 0.2,
            "V55_7Y": -0.3,
            "DIV": 0.4,
        }

        validated = _validate_worker_result(
            valid,
            item=item,
            mode="cold",
            expected_generation=_generation_binding(generation),
            business_date=generation.business_date,
            feature_date=generation.feature_date,
            output_root=Path("/isolated"),
        )

        self.assertEqual(len(validated), len(item.target_tenors))
        for invalid_keys in (
            {"STD", "ACCWT", "V55_7Y"},
            {"STD", "ACCWT", "V55_7Y", "DIV", "EXTRA"},
        ):
            invalid = self._worker_result(
                generation,
                item,
                mode="cold",
            )
            invalid_extra = invalid["records"][0]["extra"]
            invalid_extra["vote_baselines"] = [
                "STD",
                "ACCWT",
                "V55_7Y",
            ]
            invalid_extra["fallback_baseline"] = "DIV"
            invalid_extra["baseline_signs"] = {
                key: -1 for key in invalid_keys
            }
            invalid_extra["baseline_scores"] = {
                key: -0.1 for key in invalid_keys
            }
            with self.assertRaises(
                NativeDailyCertificationError
            ):
                _validate_worker_result(
                    invalid,
                    item=item,
                    mode="cold",
                    expected_generation=_generation_binding(generation),
                    business_date=generation.business_date,
                    feature_date=generation.feature_date,
                    output_root=Path("/isolated"),
                )

    def test_canonical_prediction_fields_are_required(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _generation_binding,
            _validate_worker_result,
            load_generation_native_matrix,
        )

        generation = self._generation()
        item = next(
            candidate
            for candidate in load_generation_native_matrix()
            if candidate.scheme_id == "t1_daily"
        )
        cases = (
            ("predicted_direction", 9),
            ("confidence", float("nan")),
            ("model_version", ""),
            ("target_date", ""),
        )
        for field, value in cases:
            with self.subTest(field=field):
                result = self._worker_result(
                    generation,
                    item,
                    mode="first",
                )
                result["records"][0][field] = value
                with self.assertRaises(
                    NativeDailyCertificationError
                ) as caught:
                    _validate_worker_result(
                        result,
                        item=item,
                        mode="first",
                        expected_generation=(
                            _generation_binding(generation)
                        ),
                        business_date=generation.business_date,
                        feature_date=generation.feature_date,
                        output_root=Path("/isolated"),
                    )
                self.assertEqual(
                    caught.exception.code,
                    "NATIVE_CERT_RECORDS_INVALID",
                )

    def test_target_date_must_match_frozen_calendar_horizon(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _generation_binding,
            _validate_worker_result,
            load_generation_native_matrix,
        )

        generation = self._generation()
        item = next(
            candidate
            for candidate in load_generation_native_matrix()
            if candidate.scheme_id == "t1_daily"
        )
        for wrong_target_date in (
            "2030-01-01",
            "2026-08-01",
            "2026-08-03",
        ):
            with self.subTest(target_date=wrong_target_date):
                result = self._worker_result(
                    generation,
                    item,
                    mode="first",
                )
                result["records"][0]["target_date"] = (
                    wrong_target_date
                )
                with self.assertRaises(
                    NativeDailyCertificationError
                ) as caught:
                    _validate_worker_result(
                        result,
                        item=item,
                        mode="first",
                        expected_generation=(
                            _generation_binding(generation)
                        ),
                        business_date=generation.business_date,
                        feature_date=generation.feature_date,
                        expected_target_date="2026-07-31",
                        output_root=Path("/isolated"),
                    )
                self.assertEqual(
                    caught.exception.code,
                    "NATIVE_CERT_RECORDS_INVALID",
                )

    def test_warm_cache_family_and_shared_lineage_are_enforced(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            certify_generation_native_daily,
        )

        generation = self._generation()

        def worker(item, mode, **_kwargs):
            result = self._worker_result(
                generation,
                item,
                mode=mode,
                output_root=_kwargs["output_root"],
            )
            if (
                item.scheme_id
                == "liwei_0616_10y01_cons_say_k3_div_k10"
                and mode == "warm_build"
            ):
                result["records"][0]["extra"]["phase_a_cache"][
                    "cache_family"
                ] = "wrong-family"
            return result

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir).resolve() / "certification"
            output_root.mkdir(mode=0o700)
            with self.assertRaises(
                NativeDailyCertificationError
            ) as caught:
                certify_generation_native_daily(
                    manifest_path=generation.manifest_path,
                    output_root=output_root,
                    worker=worker,
                    generation_opener=lambda *_args, **_kwargs: generation,
                )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_CACHE_LINEAGE_INVALID",
        )

    def test_worker_environment_removes_database_credentials(
        self,
    ) -> None:
        from harness.native_daily_certification_worker import (
            _worker_environment,
        )

        generation = self._generation()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.dict(
                os.environ,
                {
                    "BOND_DB_PASSWORD": "must-not-cross",
                    "BOND_DB_HOST": "production-host",
                    "BFL_SOURCE_DB_CONFIG_PATH":
                        "/private/source.json",
                    "BFL_SOURCE_DB_CONFIG_ROOT": "/private",
                    "BFL_SOURCE_RUNNER_MYSQL": "1",
                    "MYSQL_PWD": "must-not-cross",
                    "DATABASE_URL": "mysql://must-not-cross",
                    "SQLALCHEMY_DATABASE_URI":
                        "mysql://must-not-cross",
                    "ARBITRARY_SECRET": "must-not-cross",
                    "LANG": "zh_CN.UTF-8",
                },
                clear=True,
            ),
        ):
            root = Path(tmpdir).resolve()
            environment = _worker_environment(
                generation=generation,
                cache_root=root / "cache",
                environment_root=root / "environment",
            )

        self.assertFalse(
            any(name.startswith("BOND_DB_") for name in environment)
        )
        self.assertNotIn("BFL_SOURCE_DB_CONFIG_PATH", environment)
        self.assertNotIn("BFL_SOURCE_DB_CONFIG_ROOT", environment)
        self.assertNotIn("BFL_SOURCE_RUNNER_MYSQL", environment)
        self.assertNotIn("MYSQL_PWD", environment)
        self.assertNotIn("DATABASE_URL", environment)
        self.assertNotIn("SQLALCHEMY_DATABASE_URI", environment)
        self.assertNotIn("ARBITRARY_SECRET", environment)
        self.assertEqual(environment["LANG"], "zh_CN.UTF-8")
        self.assertEqual(
            environment["BOND_NATIVE_INPUT_MODE"],
            "native_generation_v1",
        )
        self.assertEqual(
            environment["BOND_NATIVE_GENERATION_ID"],
            generation.generation_id,
        )
        self.assertEqual(
            environment["HOME"],
            str(root / "environment" / "home"),
        )

    def test_worker_command_uses_os_sandbox_with_read_only_inputs(
        self,
    ) -> None:
        from harness.native_daily_certification_worker import (
            _native_certification_sandbox_command,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            runtime = root / "runtime"
            project = root / "candidate"
            generation = root / "generation"
            output = root / "output"
            for path in (runtime, project, generation, output):
                path.mkdir(mode=0o700)
            executable = runtime / "python"
            executable.write_bytes(b"python")
            executable.chmod(0o700)

            command = _native_certification_sandbox_command(
                [str(executable), "-m", "worker"],
                output_root=output,
                project_root=project,
                generation_root=generation,
                python_executable=executable,
                python_prefix=runtime,
                sandbox_available=lambda: True,
            )

        self.assertEqual(command[0], str(executable))
        self.assertIn("-I", command)
        policy = command[command.index("-c") + 2]
        self.assertIn("(deny network*)", policy)
        self.assertIn(f'(subpath "{output}")', policy)
        self.assertIn("(allow file-write*", policy)
        self.assertIn("(deny file-write*", policy)
        self.assertIn("(allow process-fork)", policy)
        self.assertIn("(allow ipc-posix-sem)", policy)
        self.assertIn("(allow process-exec", policy)
        self.assertIn(f'(literal "{executable}")', policy)
        self.assertIn(f'(subpath "{project}")', policy)
        self.assertIn(f'(subpath "{generation}")', policy)

    @unittest.skipUnless(
        sys.platform == "darwin" and shutil.which("sandbox-exec"),
        "requires macOS sandbox",
    )
    def test_sandbox_allows_python_children_but_keeps_denies(
        self,
    ) -> None:
        from harness.native_daily_certification_worker import (
            _native_certification_sandbox_command,
        )

        bootstrap = r'''
import ctypes
import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

policy, output_root, forbidden = sys.argv[1:]
error = ctypes.c_char_p()
library = ctypes.CDLL("/usr/lib/libsandbox.1.dylib")
library.sandbox_init.argtypes = [
    ctypes.c_char_p,
    ctypes.c_uint64,
    ctypes.POINTER(ctypes.c_char_p),
]
library.sandbox_init.restype = ctypes.c_int
if library.sandbox_init(policy.encode(), 0, ctypes.byref(error)) != 0:
    raise RuntimeError(error.value)

def main():
    mp.set_start_method("spawn", force=True)
    process = mp.get_context("spawn").Process(
        target=abs,
        args=(-7,),
    )
    process.start()
    process.join(5)
    if process.exitcode != 0:
        raise RuntimeError(
            f"spawn child failed: exitcode={process.exitcode}"
        )
    with mp.get_context("spawn").Pool(1) as pool:
        pool_result = pool.map(abs, [-7])
    pid = os.fork()
    if pid == 0:
        import socket
        Path(output_root, "child.txt").write_text("ok")
        try:
            Path(forbidden).write_text("forbidden")
        except OSError:
            write_denied = True
        else:
            write_denied = False
        sock = socket.socket()
        try:
            sock.connect(("127.0.0.1", 9))
        except OSError as exc:
            network_denied = exc.errno in {1, 13}
        else:
            network_denied = False
        finally:
            sock.close()
        Path(output_root, "child-result.json").write_text(
            json.dumps({
                "network_denied": network_denied,
                "write_denied": write_denied,
            })
        )
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        raise RuntimeError("fork child failed")
    payload = json.loads(
        Path(output_root, "child-result.json").read_text()
    )
    payload["pool_result"] = pool_result
    print(json.dumps(payload))

if __name__ == "__main__":
    main()
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            output = root / "output"
            generation = root / "generation"
            output.mkdir(mode=0o700)
            generation.mkdir(mode=0o700)
            command = _native_certification_sandbox_command(
                [sys.executable, "-m", "worker"],
                output_root=output,
                project_root=Path(__file__).resolve().parents[1],
                generation_root=generation,
            )
            policy = command[command.index("-c") + 2]
            forbidden = root / "forbidden.txt"
            completed = subprocess.run(
                [
                    command[0],
                    "-I",
                    "-c",
                    bootstrap,
                    policy,
                    str(output),
                    str(forbidden),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
            )
            payload = json.loads(
                completed.stdout.strip().splitlines()[-1]
            )

            self.assertEqual(
                payload,
                {
                    "network_denied": True,
                    "pool_result": [7],
                    "write_denied": True,
                },
            )
            self.assertEqual(
                (output / "child.txt").read_text(),
                "ok",
            )
            self.assertEqual(
                (output / "child-result.json").is_file(),
                True,
            )
            self.assertFalse(forbidden.exists())

    def test_clean_worktree_check_rejects_ignored_files(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _require_isolated_clean_worktree,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            project = root / "candidate"
            output = root / "output"
            project.mkdir(mode=0o700)
            output.mkdir(mode=0o700)
            (project / ".git").write_text(
                "gitdir: /isolated/git",
                encoding="utf-8",
            )
            symbolic = subprocess.CompletedProcess(
                ["git"],
                1,
                "",
                "",
            )
            status = subprocess.CompletedProcess(
                ["git"],
                0,
                "!! .env\n",
                "",
            )
            with (
                mock.patch(
                    "harness.native_daily_certification."
                    "subprocess.run",
                    side_effect=(symbolic, status),
                ),
                self.assertRaises(
                    NativeDailyCertificationError
                ) as caught,
            ):
                _require_isolated_clean_worktree(
                    project,
                    output_root=output,
                )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_ISOLATED_WORKTREE_REQUIRED",
        )

    def test_normal_worker_exit_with_descendants_fails_closed(
        self,
    ) -> None:
        from harness.native_daily_certification_worker import (
            NativeDailyCertificationError,
            _run_certification_process_group,
        )
        from scheduler.process_control import (
            ProcessGroupTerminationResult,
        )

        process = mock.Mock(pid=81234, returncode=0)
        process.communicate.return_value = ("ok", "")
        termination = ProcessGroupTerminationResult(
            process_id=81234,
            process_group_id=81234,
            term_sent=True,
            kill_sent=False,
            confirmed_gone=True,
        )
        with (
            mock.patch(
                "harness.native_daily_certification_worker."
                "subprocess.Popen",
                return_value=process,
            ),
            mock.patch(
                "harness.native_daily_certification_worker."
                "capture_new_session_process_group",
                return_value=81234,
            ),
            mock.patch(
                "harness.native_daily_certification_worker."
                "_process_group_exists",
                return_value=True,
            ),
            mock.patch(
                "harness.native_daily_certification_worker."
                "terminate_process_group",
                return_value=termination,
            ) as terminate,
            self.assertRaises(
                NativeDailyCertificationError
            ) as caught,
        ):
            _run_certification_process_group(
                ["worker"],
                cwd=Path("/candidate"),
                env={},
                timeout=1,
            )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_ORPHAN_PROCESS",
        )
        terminate.assert_called_once()

    def test_private_report_rejects_replaced_output_root(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _write_private_json,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir).resolve()
            output = parent / "output"
            output.mkdir(mode=0o700)
            original = output.stat()
            moved = parent / "moved"
            output.rename(moved)
            output.mkdir(mode=0o700)

            with self.assertRaises(
                NativeDailyCertificationError
            ) as caught:
                _write_private_json(
                    output / "certification.json",
                    {"status": "passed"},
                    expected_root=(
                        original.st_dev,
                        original.st_ino,
                    ),
                )

            self.assertFalse(
                (output / "certification.json").exists()
            )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_OUTPUT_ROOT_DRIFT",
        )

    def test_candidate_identity_is_rechecked_around_every_worker(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeCertificationCandidate,
            certify_generation_native_daily,
        )

        generation = self._generation()
        project_root = Path(__file__).resolve().parents[1]
        policy_path = (
            project_root
            / "deploy"
            / "daily_scheduler_policy_v1.json"
        )
        calls = 0

        def worker(item, mode, **kwargs):
            nonlocal calls
            calls += 1
            self.assertEqual(kwargs["policy_path"], policy_path)
            self.assertEqual(
                kwargs["policy_sha256"],
                hashlib.sha256(policy_path.read_bytes()).hexdigest(),
            )
            return self._worker_result(
                generation,
                item,
                mode=mode,
                output_root=kwargs["output_root"],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir).resolve() / "output"
            output.mkdir(mode=0o700)
            details = output.stat()
            candidate = NativeCertificationCandidate(
                project_root=project_root,
                commit="a" * 40,
                policy_path=policy_path,
                policy_sha256=hashlib.sha256(
                    policy_path.read_bytes()
                ).hexdigest(),
                output_root=output,
                output_device=details.st_dev,
                output_inode=details.st_ino,
            )
            with mock.patch(
                "harness.native_daily_certification."
                "_verify_candidate_identity",
            ) as verify:
                certify_generation_native_daily(
                    manifest_path=generation.manifest_path,
                    output_root=output,
                    worker=worker,
                    policy_path=policy_path,
                    generation_opener=(
                        lambda *_args, **_kwargs: generation
                    ),
                    candidate=candidate,
                )

        self.assertEqual(verify.call_count, calls * 2 + 2)

    def test_child_rejects_parent_policy_digest_drift(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
        )
        from harness.native_daily_certification_worker import (
            execute_native_certification_worker,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            policy = root / "policy.json"
            policy.write_text("{}", encoding="utf-8")
            with self.assertRaises(
                NativeDailyCertificationError
            ) as caught:
                execute_native_certification_worker(
                    scheme_id="t1_daily",
                    mode="first",
                    manifest_path=root / "manifest.json",
                    artifact_root=root / "artifacts",
                    result_path=root / "result.json",
                    policy_path=policy,
                    policy_sha256="f" * 64,
                )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_POLICY_DRIFT",
        )

    def test_cold_worker_uses_real_predict_entry_with_cache_disabled(
        self,
    ) -> None:
        from harness.native_daily_certification import (
            NativeCertificationItem,
        )
        from harness.native_daily_certification_worker import (
            _run_real_predict_entry,
        )
        from shared.models import PredictionRecord

        calls: dict[str, object] = {}
        module = SimpleNamespace()

        def build_daily(**kwargs):
            calls["output_root"] = kwargs["output_root"]
            return object()

        def inference(**kwargs):
            calls["use_incremental_cache"] = kwargs[
                "use_incremental_cache"
            ]
            calls["cache_root"] = kwargs["cache_root"]
            return object()

        def run(predict_date):
            module.build_daily_input_artifact(
                scheme_id="demo",
                predict_date=predict_date,
                start_date="2026-01-01",
                end_date="2026-07-24",
            )
            module.run_10y01_for_feature_date(
                use_incremental_cache=True,
                cache_root="/must-not-be-used",
            )
            return [
                PredictionRecord(
                    scheme_id=(
                        "liwei_0616_10y01_cons_say_k3_div_k10"
                    ),
                    target_tenor="10Y",
                    horizon=5,
                    predict_date=predict_date,
                    feature_date="2026-07-24",
                    target_date="2026-07-31",
                    predicted_direction=-1,
                    confidence=0.4,
                    model_version="real",
                    extra={},
                )
            ]

        module.build_daily_input_artifact = build_daily
        module.run_10y01_for_feature_date = inference
        module.run = run
        item = NativeCertificationItem(
            scheme_id="liwei_0616_10y01_cons_say_k3_div_k10",
            horizon=5,
            target_tenors=("10Y",),
            input_compatibility="generation_v1",
            cache_group="liwei:test",
            cache_prerequisite=False,
            timeout_sec=600,
            required_internal_fields=(
                "vote_score",
                "baseline_signs",
                "baseline_scores",
            ),
        )

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch(
                "harness.native_daily_certification_worker."
                "importlib.import_module",
                return_value=module,
            ),
        ):
            artifact_root = Path(tmpdir).resolve()
            records = _run_real_predict_entry(
                item,
                mode="cold",
                predict_date="2026-07-25",
                artifact_root=artifact_root,
            )

        self.assertEqual(calls["output_root"], artifact_root)
        self.assertIs(calls["use_incremental_cache"], False)
        self.assertIsNone(calls["cache_root"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["target_tenor"], "10Y")

    def test_private_tree_rejects_unsafe_existing_parent(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
        )
        from harness.native_daily_certification_worker import (
            _make_private_tree,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o755)
            child = unsafe / "child"

            with self.assertRaises(
                NativeDailyCertificationError
            ):
                _make_private_tree(child)

            self.assertFalse(child.exists())

    def test_real_worker_rejects_non_forecast_environment(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            load_generation_native_matrix,
        )
        from harness.native_daily_certification_worker import (
            run_real_native_worker,
        )

        generation = self._generation()
        item = load_generation_native_matrix()[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir).resolve()
            with (
                mock.patch(
                    "harness.native_daily_certification_worker."
                    "sys.prefix",
                    "/isolated/bond_factor_lab_service",
                ),
                self.assertRaises(
                    NativeDailyCertificationError
                ) as caught,
            ):
                run_real_native_worker(
                    item,
                    "first",
                    generation=generation,
                    output_root=output_root,
                    policy_path=(
                        Path(__file__).resolve().parents[1]
                        / "deploy"
                        / "daily_scheduler_policy_v1.json"
                    ),
                    policy_sha256="a" * 64,
                    project_root=Path(__file__).resolve().parents[1],
                )

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_RUNTIME_ENV_INVALID",
        )

    def test_cli_requires_explicit_no_persist(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            main,
        )

        with self.assertRaises(
            NativeDailyCertificationError
        ) as caught:
            main(["--manifest", "/tmp/manifest.json"])

        self.assertEqual(
            caught.exception.code,
            "NATIVE_CERT_NO_PERSIST_REQUIRED",
        )

    @unittest.skipUnless(
        Path("/tmp").is_symlink(),
        "requires macOS /tmp symlink",
    )
    def test_tmp_output_root_requires_physical_path(self) -> None:
        from harness.native_daily_certification import (
            NativeDailyCertificationError,
            _require_empty_private_output_root,
        )

        with tempfile.TemporaryDirectory(
            dir="/tmp",
            prefix="bfl-native-cert.",
        ) as raw:
            raw_path = Path(raw)
            physical_path = raw_path.resolve(strict=True)
            with self.assertRaises(NativeDailyCertificationError):
                _require_empty_private_output_root(raw_path)
            self.assertEqual(
                _require_empty_private_output_root(physical_path),
                physical_path,
            )

    def test_direct_script_entry_can_load_harness(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                str(
                    project_root
                    / "scripts"
                    / "certify_generation_native_daily.py"
                ),
                "--help",
            ],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--no-persist", completed.stdout)

    def _generation(self) -> SimpleNamespace:
        return SimpleNamespace(
            generation_id="native-0123456789abcdef01234567",
            manifest_sha256="a" * 64,
            dataset_content_id="b" * 64,
            business_date="2026-07-25",
            feature_date="2026-07-24",
            schema_version="native-generation-v1",
            exporter_version="native-generation-exporter-v1",
            manifest_path=Path(
                "/tmp/native-certification-generation/manifest.json"
            ),
        )

    def _worker_result(
        self,
        generation,
        item,
        *,
        mode: str,
        output_root: Path = Path("/isolated"),
    ) -> dict[str, object]:
        records = []
        for tenor in item.target_tenors:
            extra = {
                field: self._internal_value(field)
                for field in item.required_internal_fields
            }
            extra.update(
                {
                    "input_artifact_source":
                        "shared_data_service_daily",
                    "input_artifact_path":
                        f"/isolated/{item.scheme_id}/{mode}.csv",
                }
            )
            if item.is_liwei:
                cache_family, _cache_tenor = (
                    item.cache_group.rsplit(":", 1)
                )
                extra["phase_a_cache_family"] = cache_family
                extra["phase_a_cache_root"] = (
                    None
                    if mode == "cold"
                    else f"/isolated/runtime/{mode}"
                )
            if item.is_liwei and mode != "cold":
                cache_family, cache_tenor = item.cache_group.rsplit(
                    ":",
                    1,
                )
                extra.update(
                    {
                        "phase_a_cache": {
                            "status": "hit",
                            "cache_family": cache_family,
                            "tenor": cache_tenor,
                            "family_root": (
                                str(
                                    output_root
                                    / "cache"
                                    / cache_family
                                    / cache_tenor.lower()
                                )
                            ),
                            "generation_id": (
                                "cache-"
                                + cache_family.replace("_", "-")
                            ),
                            "generation_manifest_sha256": "c" * 64,
                            "generation_acceptance": {
                                "native_generation": {
                                    "generation_id":
                                        generation.generation_id,
                                    "manifest_sha256":
                                        generation.manifest_sha256,
                                    "dataset_content_id":
                                        generation.dataset_content_id,
                                    "business_date":
                                        generation.business_date,
                                    "feature_date":
                                        generation.feature_date,
                                    "schema_version":
                                        generation.schema_version,
                                    "exporter_version":
                                        generation.exporter_version,
                                },
                            },
                        },
                        "phase_a_cache_status": "hit",
                    }
                )
            records.append(
                {
                    "scheme_id": item.scheme_id,
                    "target_tenor": tenor,
                    "horizon": item.horizon,
                    "predict_date": generation.business_date,
                    "feature_date": generation.feature_date,
                    "target_date": "2026-07-31",
                    "predicted_direction": -1,
                    "confidence": 0.4,
                    "model_version": "real-model",
                    "extra": extra,
                }
            )
        return {
            "scheme_id": item.scheme_id,
            "mode": mode,
            "records": records,
            "generation_id": generation.generation_id,
            "manifest_sha256": generation.manifest_sha256,
            "network_attempts": 0,
            "persistence": "none",
            "cache_status": (
                "hit"
                if item.is_liwei and mode != "cold"
                else None
            ),
        }

    @staticmethod
    def _internal_value(field: str) -> object:
        if field == "vote_baselines":
            return ["STD"]
        if field == "fallback_baseline":
            return "STD"
        if field in {"baseline_signs", "baseline_scores", "vote_signals"}:
            return {"STD": -1}
        if field in {"decision", "base_decision"}:
            return "model"
        if field in {"threshold", "ens_prob"}:
            return 0.5
        return -1


if __name__ == "__main__":
    unittest.main()
