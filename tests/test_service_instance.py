from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class _Scalar:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value


class _Connection:
    def __init__(self, schema: str) -> None:
        self._schema = schema

    def execute(self, _sql):
        return _Scalar(self._schema)


class _Engine:
    def __init__(self, *, schema: str, host: str = "127.0.0.1") -> None:
        self._schema = schema
        self.url = SimpleNamespace(
            get_backend_name=lambda: "mysql",
            host=host,
            port=3306,
            database=schema,
            username="secret-user",
            password="secret-password",
        )

    @contextmanager
    def connect(self):
        yield _Connection(self._schema)


class ServiceInstanceIdentityTests(unittest.TestCase):
    def test_fingerprint_binds_database_commit_profile_and_nonce_without_leaking_raw_values(self) -> None:
        from shared.service_instance import build_service_instance_identity

        root = Path("/tmp/test-project")
        with patch("shared.service_instance._git_commit", return_value="a" * 40):
            baseline = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=root,
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-one",
            )
            wrong_db = build_service_instance_identity(
                _Engine(schema="bbv2_cert_two"),
                project_root=root,
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-one",
            )
            wrong_profile = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=root,
                runtime_profile="other-profile",
                instance_nonce="instance-secret-one",
            )
            wrong_nonce = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=root,
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-two",
            )
        with patch("shared.service_instance._git_commit", return_value="b" * 40):
            wrong_commit = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=root,
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-one",
            )

        fingerprints = {
            item["fingerprint"]
            for item in (baseline, wrong_db, wrong_profile, wrong_nonce, wrong_commit)
        }
        self.assertEqual(len(fingerprints), 5)
        serialized = json.dumps(baseline, sort_keys=True)
        for secret in (
            "bbv2_cert_one",
            "instance-secret-one",
            "secret-user",
            "secret-password",
        ):
            self.assertNotIn(secret, serialized)

    def test_health_returns_safe_service_identity(self) -> None:
        from backend import main

        identity = {
            "fingerprint_version": "1",
            "fingerprint": "f" * 64,
            "database_identity_sha256": "d" * 64,
            "code_commit": "a" * 40,
            "runtime_profile": "blackbox-v2-v1",
            "instance_nonce_sha256": "n" * 64,
        }
        with (
            patch.object(main, "get_engine", return_value="engine"),
            patch.object(main, "build_service_instance_identity", return_value=identity) as build,
            patch.dict(
                "os.environ",
                {
                    "BOND_FACTOR_LAB_RUNTIME_PROFILE": "blackbox-v2-v1",
                    "BOND_FACTOR_LAB_INSTANCE_NONCE": "explicit-instance",
                },
            ),
        ):
            result = main.health()

        self.assertEqual(result, {"status": "ok", "service_instance": identity})
        self.assertEqual(build.call_args.kwargs["instance_nonce"], "explicit-instance")
