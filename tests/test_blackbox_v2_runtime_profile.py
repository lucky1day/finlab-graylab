from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = PROJECT_ROOT / "deploy" / "blackbox_v2" / "runtime_profile_v1.json"


class BlackboxV2RuntimeProfileTests(unittest.TestCase):
    def test_profile_matches_runner_defaults(self) -> None:
        from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE

        profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(profile["profile_name"], DEFAULT_RUNTIME_PROFILE.name)
        self.assertEqual(profile["conda_env"], DEFAULT_RUNTIME_PROFILE.conda_env)
        for field in (
            "data_schema_version",
            "contract_version",
            "cpu_threads",
            "memory_limit_bytes",
            "predict_timeout_sec",
            "backtest_timeout_sec",
            "max_batch_requests",
            "max_output_bytes",
            "max_log_bytes",
            "max_run_dir_bytes",
            "sandbox_enabled",
            "network_access",
            "database_access",
        ):
            self.assertEqual(profile[field], getattr(DEFAULT_RUNTIME_PROFILE, field))
        self.assertEqual(
            tuple(
                (item["path"], item["resolved_boundary"])
                for item in profile["read_roots"]
            ),
            tuple(
                (item.path, item.resolved_boundary)
                for item in DEFAULT_RUNTIME_PROFILE.read_roots
            ),
        )
        self.assertEqual(
            tuple(profile["environment_allowlist"]),
            DEFAULT_RUNTIME_PROFILE.environment_allowlist,
        )
        self.assertEqual(
            tuple(profile["environment_defaults"].items()),
            DEFAULT_RUNTIME_PROFILE.environment_defaults,
        )

    def test_profile_read_roots_are_narrow(self) -> None:
        from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE

        forbidden = {"/", "/Users", "/etc", str(PROJECT_ROOT), str(Path.home())}
        self.assertTrue(
            forbidden.isdisjoint(item.path for item in DEFAULT_RUNTIME_PROFILE.read_roots)
        )
        self.assertEqual(
            tuple(
                (item.path, item.resolved_boundary)
                for item in DEFAULT_RUNTIME_PROFILE.read_roots
            ),
            (("/opt/homebrew/opt/libomp/lib", "/opt/homebrew/Cellar/libomp"),),
        )

    def test_profile_loader_rejects_malformed_and_unsafe_profiles(self) -> None:
        from scheduler.blackbox_v2_runner import _load_runtime_profile

        base = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        cases: list[tuple[str, dict]] = []

        profile = copy.deepcopy(base)
        profile["unknown_field"] = True
        cases.append(("unknown", profile))

        profile = copy.deepcopy(base)
        profile.pop("cpu_threads")
        cases.append(("missing", profile))

        profile = copy.deepcopy(base)
        profile["cpu_threads"] = True
        cases.append(("wrong type", profile))

        profile = copy.deepcopy(base)
        profile["max_run_dir_bytes"] = 0
        cases.append(("positive", profile))

        profile = copy.deepcopy(base)
        profile["max_output_bytes"] = profile["max_run_dir_bytes"] + 1
        cases.append(("max_output_bytes", profile))

        profile = copy.deepcopy(base)
        profile["read_roots"] = [{"path": "/", "resolved_boundary": "/"}]
        cases.append(("unsafe", profile))

        with tempfile.TemporaryDirectory() as tmpdir:
            for index, (message, payload) in enumerate(cases):
                path = Path(tmpdir) / f"profile-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(case=message):
                    with self.assertRaisesRegex(ValueError, message):
                        _load_runtime_profile(path)

    def test_profile_file_changes_runtime_thread_and_quota_behavior(self) -> None:
        from scheduler.blackbox_v2_runner import _load_runtime_profile, _runtime_environment

        payload = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        payload["cpu_threads"] = 3
        payload["max_run_dir_bytes"] = payload["max_output_bytes"] + 12345
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "profile.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            profile = _load_runtime_profile(path)
            environment = _runtime_environment(
                profile,
                root,
                python_executable=sys.executable,
            )

        self.assertEqual(profile.max_run_dir_bytes, payload["max_run_dir_bytes"])
        self.assertEqual(environment["OMP_NUM_THREADS"], "3")

    def test_read_root_missing_and_boundary_escape_are_clear_errors(self) -> None:
        from scheduler.blackbox_v2_runner import (
            _load_runtime_profile,
            _resolve_runtime_read_roots,
        )

        base = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            boundary = root / "approved" / "libomp"
            boundary.mkdir(parents=True)

            missing = copy.deepcopy(base)
            missing["read_roots"] = [
                {
                    "path": str(boundary / "missing"),
                    "resolved_boundary": str(boundary),
                }
            ]
            missing_path = root / "missing.json"
            missing_path.write_text(json.dumps(missing), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not exist"):
                _resolve_runtime_read_roots(_load_runtime_profile(missing_path))

            outside = root / "outside"
            outside.mkdir()
            alias = boundary / "escaped"
            alias.symlink_to(outside, target_is_directory=True)
            escaped = copy.deepcopy(base)
            escaped["read_roots"] = [
                {
                    "path": str(alias),
                    "resolved_boundary": str(boundary),
                }
            ]
            escaped_path = root / "escaped.json"
            escaped_path.write_text(json.dumps(escaped), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes approved boundary"):
                _resolve_runtime_read_roots(_load_runtime_profile(escaped_path))

    def test_python_runtime_rejects_malicious_prefixes(self) -> None:
        from scheduler.blackbox_v2_runner import _validate_python_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            arbitrary_prefix = root / "arbitrary"
            arbitrary_bin = arbitrary_prefix / "bin"
            arbitrary_bin.mkdir(parents=True)
            arbitrary_python = arbitrary_bin / "python"
            arbitrary_python.write_text("not-python", encoding="utf-8")
            arbitrary_python.chmod(0o700)

            env_prefix = root / "conda" / "envs" / "expected"
            env_bin = env_prefix / "bin"
            env_bin.mkdir(parents=True)
            outside_python = root / "outside-python"
            outside_python.write_text("not-python", encoding="utf-8")
            outside_python.chmod(0o700)

            cases = (
                (Path("/usr/bin/python3"), Path("/usr"), "expected"),
                (arbitrary_python, arbitrary_prefix, "expected"),
                (outside_python, env_prefix, "expected"),
                (env_bin / "python", env_prefix, "different"),
            )
            (env_bin / "python").write_text("not-python", encoding="utf-8")
            (env_bin / "python").chmod(0o700)
            for executable, prefix, env_name in cases:
                with self.subTest(executable=str(executable), prefix=str(prefix)):
                    with self.assertRaisesRegex(ValueError, "Python runtime"):
                        _validate_python_runtime(executable, prefix, env_name)

    def test_environment_manifest_fingerprint_is_self_consistent(self) -> None:
        manifest = json.loads(
            (PROJECT_ROOT / "deploy" / "blackbox_v2" / "environment_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        canonical = json.dumps(
            manifest["explicit_packages"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(
            manifest["environment_fingerprint"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
