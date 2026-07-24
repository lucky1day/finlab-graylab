from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scheduler.capacity_candidate_runtime import (
    CapacityCandidateRuntimeError,
)
from scheduler.discovery import SchemeConfig, discover_schemes


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "deploy" / "daily_scheduler_policy_v1.json"
SERVER_UUID = "11111111-2222-3333-4444-555555555555"


def _active_daily() -> tuple[SchemeConfig, ...]:
    return tuple(
        config
        for config in discover_schemes(strict=True)
        if config.status == "active" and config.frequency == "daily"
    )


def _registry_version_rows(
    configs: tuple[SchemeConfig, ...],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for config in configs:
        for tenor in config.tenors:
            rows.append(
                {
                    "registry_scheme_id":
                        f"{config.scheme_id}__h{config.horizon}__{tenor}",
                    "base_scheme_id": config.scheme_id,
                    "registry_runtime_type": config.runtime_type,
                    "frequency": config.frequency,
                    "task_type": config.task_type,
                    "target_tenor": tenor,
                    "horizon": config.horizon,
                    "registry_status": config.status,
                    "scheme_version": config.scheme_version,
                    "version_runtime_type": config.runtime_type,
                    "code_hash": config.code_hash,
                    "config_hash": config.config_hash,
                    "manifest_hash": config.manifest_hash,
                    "version_status": "active",
                    "algorithm_version": config.algorithm_version,
                    "contract_version": config.contract_version,
                    "runtime_profile": config.runtime_profile,
                    "environment_fingerprint":
                        config.environment_fingerprint,
                    "data_snapshot_id": config.data_snapshot_id,
                }
            )
    return sorted(rows, key=lambda row: str(row["registry_scheme_id"]))


_REQUIRED_COLUMNS = {
    "t_input_generations": {
        "generation_id",
        "state",
        "manifest_sha256",
    },
    "t_schedule_occurrences": {
        "occurrence_id",
        "policy_sha256",
        "registry_digest",
        "sla_outcome",
    },
    "t_schedule_items": {
        "item_id",
        "current_run_id",
        "attempt_no",
        "state",
        "input_generation_id",
    },
    "t_schedule_item_targets": {
        "target_id",
        "registry_scheme_id",
        "accepted_run_id",
        "status",
    },
    "t_scheme_runs": {
        "run_id",
        "schedule_item_id",
        "attempt_no",
        "status",
        "failure_code",
    },
}

_REQUIRED_INDEXES = {
    ("t_schedule_occurrences", "uk_schedule_occurrence"),
    ("t_schedule_items", "uk_schedule_item"),
    ("t_schedule_item_targets", "uk_schedule_target_registry"),
    ("t_schedule_item_targets", "uk_schedule_target_item"),
    ("t_scheme_runs", "uk_scheme_runs_schedule_attempt"),
    ("t_scheme_runs", "uk_scheme_runs_execution_token"),
}

_REQUIRED_FOREIGN_KEYS = {
    ("t_schedule_items", "fk_schedule_item_occurrence"),
    ("t_schedule_item_targets", "fk_schedule_target_item"),
    ("t_scheme_runs", "fk_scheme_run_schedule_item"),
}

_REQUIRED_CHECKS = {
    ("t_schedule_occurrences", "ck_schedule_occurrence_feature_date"),
    ("t_schedule_occurrences", "ck_schedule_occurrence_failure_code"),
    ("t_schedule_items", "ck_schedule_item_failure_code"),
    ("t_scheme_runs", "ck_scheme_run_schedule_failure_code"),
}


def _schema_rows() -> dict[str, list[dict[str, object]]]:
    columns: list[dict[str, object]] = []
    for table_name, names in sorted(_REQUIRED_COLUMNS.items()):
        for position, column_name in enumerate(sorted(names), start=1):
            columns.append(
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "ordinal_position": position,
                    "column_type": "varchar(128)",
                    "is_nullable": "NO",
                    "column_default": None,
                    "extra": "",
                }
            )
    indexes = [
        {
            "table_name": table_name,
            "index_name": index_name,
            "non_unique": 0,
            "seq_in_index": 1,
            "column_name": "identity",
            "collation": "A",
            "index_type": "BTREE",
        }
        for table_name, index_name in sorted(_REQUIRED_INDEXES)
    ]
    foreign_keys = [
        {
            "table_name": table_name,
            "constraint_name": constraint_name,
            "column_name": "identity",
            "referenced_table_name": "parent",
            "referenced_column_name": "identity",
            "ordinal_position": 1,
        }
        for table_name, constraint_name in sorted(_REQUIRED_FOREIGN_KEYS)
    ]
    checks = [
        {
            "table_name": table_name,
            "constraint_name": constraint_name,
            "check_clause": "identity IS NOT NULL",
        }
        for table_name, constraint_name in sorted(_REQUIRED_CHECKS)
    ]
    return {
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "checks": checks,
    }


class _MappingsResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, object]]:
        return copy.deepcopy(self._rows)

    def one(self) -> dict[str, object]:
        if len(self._rows) != 1:
            raise RuntimeError("expected exactly one row")
        return copy.deepcopy(self._rows[0])


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return _MappingsResult(self._rows)


class _FakeConnection:
    def __init__(
        self,
        registry_rows: list[dict[str, object]],
        schema: dict[str, list[dict[str, object]]],
    ) -> None:
        self.registry_rows = registry_rows
        self.schema = schema

    def execute(self, statement: object) -> _Result:
        sql = str(statement).lower()
        if "from t_scheme_registry" in sql:
            return _Result(self.registry_rows)
        if "@@server_uuid" in sql:
            return _Result(
                [
                    {
                        "server_uuid": SERVER_UUID,
                        "database_schema": "bond_db",
                        "server_version": "8.0.42",
                        "version_comment": "MySQL Community Server - GPL",
                    }
                ]
            )
        if "information_schema.columns" in sql:
            return _Result(self.schema["columns"])
        if "information_schema.statistics" in sql:
            return _Result(self.schema["indexes"])
        if "information_schema.key_column_usage" in sql:
            return _Result(self.schema["foreign_keys"])
        if "information_schema.check_constraints" in sql:
            return _Result(self.schema["checks"])
        raise AssertionError(f"unexpected SQL: {statement}")


class _Begin:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, *args: object) -> None:
        return None


class _FakeEngine:
    def __init__(
        self,
        registry_rows: list[dict[str, object]],
        *,
        schema: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(name="mysql")
        self.connection = _FakeConnection(
            registry_rows,
            schema or _schema_rows(),
        )

    def begin(self) -> _Begin:
        return _Begin(self.connection)


class _CommandRunner:
    def __init__(
        self,
        *,
        service_marker: str = "service-v1",
        service_pip_version: str = "1.0.0",
    ) -> None:
        self.service_marker = service_marker
        self.service_pip_version = service_pip_version
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, command: tuple[str, ...]) -> bytes:
        normalized = tuple(command)
        self.calls.append(normalized)
        executable = Path(normalized[0]).name
        if executable == "ioreg":
            return (
                b'    "IOPlatformUUID" = '
                b'"AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"\n'
            )
        if executable == "sysctl" and normalized[-1] == "hw.model":
            return b"Mac14,13\n"
        if executable == "sysctl" and normalized[-1] == "hw.memsize":
            return b"137438953472\n"
        if executable == "sw_vers":
            return b"25G88\n"
        if executable == "conda":
            if "--prefix" in normalized:
                marker = self.service_marker
                pip_version = self.service_pip_version
            elif normalized[-1] == "forecast_env":
                marker = "native-v1"
                pip_version = "2.0.0"
            elif normalized[-1] == "forecast_env_blackbox_v1":
                marker = "blackbox-v1"
                pip_version = "3.0.0"
            else:
                raise AssertionError(f"unexpected conda command: {normalized}")
            if "--json" in normalized:
                return json.dumps(
                    [
                        {
                            "base_url": "https://conda.anaconda.org/pypi",
                            "build_number": 0,
                            "build_string": "pypi_0",
                            "channel": "pypi",
                            "dist_name":
                                f"runtime-package-{pip_version}-pypi_0",
                            "name": "runtime-package",
                            "platform": "pypi",
                            "version": pip_version,
                        },
                        {
                            "base_url": "https://packages.example",
                            "build_number": 0,
                            "build_string": marker,
                            "channel": "runtime",
                            "dist_name": marker,
                            "name": "runtime-marker",
                            "platform": "osx-arm64",
                            "version": "1",
                        },
                    ]
                ).encode("utf-8")
            return (
                f"# platform: osx-arm64\n@EXPLICIT\n"
                f"https://packages.example/{marker}.conda\n"
            ).encode("utf-8")
        raise AssertionError(f"unexpected command: {normalized}")


class CurrentCapacityCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.discovered = _active_daily()
        if len(cls.discovered) != 21:
            raise AssertionError("test fixture requires current 21-item policy")

    def _build(
        self,
        *,
        rows: list[dict[str, object]] | None = None,
        schema: dict[str, list[dict[str, object]]] | None = None,
        runner: _CommandRunner | None = None,
    ):
        from scheduler.capacity_candidate_runtime import (
            build_current_capacity_candidate,
        )

        engine = _FakeEngine(
            rows or _registry_version_rows(self.discovered),
            schema=schema,
        )
        with patch(
            "scheduler.capacity_candidate_runtime._resolve_conda_executable",
            return_value=Path("/opt/conda/bin/conda"),
        ), patch(
            "scheduler.capacity_candidate_runtime.sys.prefix",
            "/opt/conda/envs/bond_factor_lab_service",
        ):
            return build_current_capacity_candidate(
                engine,
                project_root=PROJECT_ROOT,
                policy_path=POLICY_PATH,
                discovered=self.discovered,
                command_runner=runner or _CommandRunner(),
            )

    def test_builds_exact_live_21_item_25_target_candidate(self) -> None:
        candidate = self._build()

        self.assertEqual(candidate.payload["expected_item_count"], 21)
        self.assertEqual(candidate.payload["expected_target_count"], 25)
        self.assertEqual(candidate.payload["memory_bytes"], 137438953472)
        targets = candidate.payload["target_manifest"]
        self.assertEqual(len(targets), 25)
        self.assertEqual(
            [row["registry_scheme_id"] for row in targets],
            sorted(row["registry_scheme_id"] for row in targets),
        )
        self.assertEqual(
            {
                row["registry_scheme_id"]
                for row in targets
                if row["runtime_type"] == "blackbox_v2"
            },
            {
                f"{config.scheme_id}__h{config.horizon}__{config.tenors[0]}"
                for config in self.discovered
                if config.runtime_type == "blackbox_v2"
            },
        )

    def test_db_active_version_single_field_drift_fails_closed(self) -> None:
        rows = _registry_version_rows(self.discovered)
        rows[0]["code_hash"] = "0" * 64

        with self.assertRaisesRegex(
            CapacityCandidateRuntimeError,
            "active version|code_hash",
        ):
            self._build(rows=rows)

    def test_registry_target_single_field_drift_fails_closed(self) -> None:
        rows = _registry_version_rows(self.discovered)
        rows[0]["task_type"] = "weekly_point"

        with self.assertRaisesRegex(
            CapacityCandidateRuntimeError,
            "Registry|task_type",
        ):
            self._build(rows=rows)

    def test_environment_command_drift_changes_current_candidate(self) -> None:
        from scheduler.capacity_attestation import (
            CapacityAttestationError,
            verify_current_candidate,
        )

        signed = self._build(runner=_CommandRunner(service_marker="v1"))
        current = self._build(runner=_CommandRunner(service_marker="v2"))

        changed_fields = {
            field
            for field in signed.payload
            if signed.payload[field] != current.payload[field]
        }
        self.assertEqual(
            changed_fields,
            {"service_environment_sha256"},
        )
        with self.assertRaisesRegex(
            CapacityAttestationError,
            "differs from signed candidate",
        ):
            verify_current_candidate(signed.payload, current.payload)

    def test_control_plane_identity_drift_changes_current_candidate(
        self,
    ) -> None:
        baseline_artifacts = {
            "cutover/marker-template.json": "1" * 64,
            "cutover/marker-path.txt": "2" * 64,
            "runtime/root.txt": "3" * 64,
            "service/uid.txt": "4" * 64,
        }
        drifted_artifacts = {
            **baseline_artifacts,
            "runtime/root.txt": "5" * 64,
        }
        with patch(
            "scheduler.capacity_candidate_runtime."
            "daily_control_plane_artifacts",
            return_value=baseline_artifacts,
            create=True,
        ):
            baseline = self._build()
        with patch(
            "scheduler.capacity_candidate_runtime."
            "daily_control_plane_artifacts",
            return_value=drifted_artifacts,
            create=True,
        ):
            drifted = self._build()

        changed_fields = {
            field
            for field in baseline.payload
            if baseline.payload[field] != drifted.payload[field]
        }
        self.assertEqual(
            changed_fields,
            {"control_plane_identity_sha256"},
        )

    def test_pip_package_version_drift_changes_current_candidate(self) -> None:
        baseline = self._build(
            runner=_CommandRunner(service_pip_version="1.0.0")
        )
        drifted = self._build(
            runner=_CommandRunner(service_pip_version="1.0.1")
        )

        changed_fields = {
            field
            for field in baseline.payload
            if baseline.payload[field] != drifted.payload[field]
        }
        self.assertEqual(
            changed_fields,
            {"service_environment_sha256"},
        )

    def test_release_file_single_field_drift_changes_candidate(self) -> None:
        from scheduler import capacity_candidate_runtime

        baseline = self._build()
        original = capacity_candidate_runtime._read_source_file

        def drifted(path: Path, label: str) -> bytes:
            payload = original(path, label)
            if path == PROJECT_ROOT / "scheduler" / "alerts.py":
                return payload + b"\n# drift-injected-by-test\n"
            return payload

        with patch(
            "scheduler.capacity_candidate_runtime._read_source_file",
            side_effect=drifted,
        ):
            current = self._build()

        changed_fields = {
            field
            for field in baseline.payload
            if baseline.payload[field] != current.payload[field]
        }
        self.assertEqual(changed_fields, {"scheduler_release_sha256"})

    def test_native_inference_drift_changes_actual_scheme_candidate(
        self,
    ) -> None:
        from scheduler import capacity_candidate_runtime

        native = next(
            config
            for config in self.discovered
            if (
                config.runtime_type == "native_adapter"
                and (config.path / "inference.py").is_file()
            )
        )
        inference_path = native.path / "inference.py"
        baseline = self._build()
        original = capacity_candidate_runtime._read_source_file

        def drifted(path: Path, label: str) -> bytes:
            payload = original(path, label)
            if path == inference_path:
                return payload + b"\n# drift-injected-by-test\n"
            return payload

        with patch(
            "scheduler.capacity_candidate_runtime._read_source_file",
            side_effect=drifted,
        ):
            current = self._build()

        changed_fields = {
            field
            for field in baseline.payload
            if baseline.payload[field] != current.payload[field]
        }
        self.assertEqual(
            changed_fields,
            {
                "registry_manifest_sha256",
                "scheme_bundle_sha256",
                "target_manifest",
            },
        )

    def test_environment_self_reported_hash_is_not_accepted(self) -> None:
        class InvalidRunner(_CommandRunner):
            def __call__(self, command: tuple[str, ...]) -> bytes:
                if Path(command[0]).name == "conda":
                    return b"sha256=pretend-this-is-enough\n"
                return super().__call__(command)

        with self.assertRaisesRegex(
            CapacityCandidateRuntimeError,
            "EXPLICIT",
        ):
            self._build(runner=InvalidRunner())

    def test_missing_critical_ledger_column_fails_closed(self) -> None:
        schema = _schema_rows()
        schema["columns"] = [
            row
            for row in schema["columns"]
            if not (
                row["table_name"] == "t_schedule_items"
                and row["column_name"] == "current_run_id"
            )
        ]

        with self.assertRaisesRegex(
            CapacityCandidateRuntimeError,
            "ledger schema|current_run_id",
        ):
            self._build(schema=schema)


if __name__ == "__main__":
    unittest.main()
